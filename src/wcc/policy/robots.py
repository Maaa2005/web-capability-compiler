"""``robots.txt``, parsed and matched as RFC 9309 describes.

The runtime asks one question of this module: may the product token ``wcc``
fetch this URL? Answering it takes a parser and a matcher, and both are written
here rather than borrowed, for two reasons found before writing them.

**The standard library answers differently by Python version.** On 3.12,
``urllib.robotparser`` takes the first rule in file order and treats ``*`` and
``$`` as literal characters. From 3.13.14 and 3.14.5 it takes the longest match
and supports both, but scores a wildcard rule by how much of the URL it matched
rather than by the rule's length. This project declares Python 3.12 as its
minimum and runs on 3.13, so the same file could allow a page on one machine and
refuse it on another.

**Protego, the parser Scrapy uses, adds Google's extensions** (``Allow`` of an
``index.html`` also allowing its directory, the ``Host`` and ``Visit-time``
lines). They are reasonable for a crawler that wants to behave like Googlebot;
here they would be behaviour nobody asked for, in exchange for a new runtime
dependency.

What is implemented, with the section of RFC 9309 each rule comes from:

- Records are ``key: value`` lines; keys are case-insensitive; ``#`` starts a
  comment. A group is one or more ``user-agent`` lines followed by rules; a
  ``user-agent`` line after a rule starts a new group. Rules before any
  ``user-agent`` line belong to no group (2.1, 2.2).
- A group applies when its ``user-agent`` value equals the product token,
  ignoring case. Every matching group is combined. Without one, the ``*`` groups
  apply; without those, nothing does (2.2.1).
- The most specific matching rule decides, measured by the length of its path
  pattern; an ``allow`` and a ``disallow`` of equal length resolve to ``allow``
  (2.2.2). Google's own library scores by pattern length too.
- ``*`` matches any run of characters and a trailing ``$`` anchors the end
  (2.2.3). A ``$`` anywhere else is an ordinary character.
- Paths are compared after the same percent-encoding normalisation on both
  sides: non-ASCII is encoded as UTF-8, and an encoded unreserved character is
  decoded (2.2.2).
- ``/robots.txt`` itself is always allowed (2.2.2).
- At most :data:`MAX_ROBOTS_BYTES` are parsed; the RFC asks for at least
  500 KiB (2.5).

``Crawl-delay`` is not in RFC 9309. It is read anyway, because the runtime
uses it to space requests further apart and never to permit anything.

The section numbers are cited from the existing notes in this repository and
from the parsers above; the RFC's own text could not be fetched from the
environment this was written in, which ``docs/progress.md`` records.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from urllib.parse import urlsplit

#: The name a site uses to address WCC in its ``robots.txt``. RFC 9309 limits a
#: product token to letters, ``_`` and ``-``, and asks that it appear in the
#: User-Agent the crawler sends, which :mod:`wcc.config` arranges.
PRODUCT_TOKEN = "wcc"

#: How much of a ``robots.txt`` is read. RFC 9309 section 2.5 requires at least
#: 500 KiB; anything after this is ignored rather than refused.
MAX_ROBOTS_BYTES = 500 * 1024

_TOKEN = re.compile(r"[A-Za-z_-]+")
_UNRESERVED = frozenset("ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz0123456789-._~")
_HEX = frozenset("0123456789abcdefABCDEF")


@dataclass(frozen=True)
class Rule:
    """One ``allow`` or ``disallow`` line, with its path already normalised."""

    allow: bool
    pattern: str

    def matches(self, path: str) -> bool:
        return _matches(self.pattern, path)


@dataclass(frozen=True)
class Group:
    """The rules addressed to one or more user agents."""

    agents: tuple[str, ...]
    rules: tuple[Rule, ...]
    crawl_delay: float | None = None


@dataclass(frozen=True)
class RobotsFile:
    """A parsed ``robots.txt``."""

    groups: tuple[Group, ...]

    def applicable(self, token: str = PRODUCT_TOKEN) -> Group | None:
        """The combined group ``token`` must obey, or ``None`` if none applies."""
        wanted = token.lower()
        named = [g for g in self.groups if any(a.lower() == wanted for a in g.agents)]
        chosen = named or [g for g in self.groups if "*" in g.agents]
        if not chosen:
            return None
        delays = [g.crawl_delay for g in chosen if g.crawl_delay is not None]
        return Group(
            agents=tuple(a for g in chosen for a in g.agents),
            rules=tuple(r for g in chosen for r in g.rules),
            crawl_delay=max(delays) if delays else None,
        )

    def allows(self, url: str, token: str = PRODUCT_TOKEN) -> bool:
        """Whether ``token`` may fetch ``url`` (a full URL or a path)."""
        path = _target(url)
        if path == "/robots.txt":
            return True
        group = self.applicable(token)
        if group is None:
            return True
        best: Rule | None = None
        for rule in group.rules:
            if not rule.matches(path):
                continue
            if (
                best is None
                or len(rule.pattern) > len(best.pattern)
                or (len(rule.pattern) == len(best.pattern) and rule.allow)
            ):
                best = rule
        return best is None or best.allow

    def crawl_delay(self, token: str = PRODUCT_TOKEN) -> float | None:
        group = self.applicable(token)
        return None if group is None else group.crawl_delay


def parse_robots(content: bytes | str) -> RobotsFile:
    """Parse a ``robots.txt`` body. Never raises: an unreadable line is skipped."""
    if isinstance(content, bytes):
        text = content[:MAX_ROBOTS_BYTES].decode("utf-8", errors="replace")
    else:
        text = content
    text = text.removeprefix("﻿")

    groups: list[Group] = []
    agents: list[str] = []
    rules: list[Rule] = []
    delay: float | None = None
    in_rules = False

    def close() -> None:
        if agents:
            groups.append(Group(tuple(agents), tuple(rules), delay))

    for raw in text.splitlines():
        line = raw.split("#", 1)[0].strip()
        if ":" not in line:
            continue
        key, value = (part.strip() for part in line.split(":", 1))
        key = key.lower()

        if key == "user-agent":
            if in_rules:
                close()
                agents, rules, delay, in_rules = [], [], None, False
            agent = _agent(value)
            if agent:
                agents.append(agent)
        elif key in ("allow", "disallow"):
            in_rules = True
            # An empty value matches nothing (RFC 9309 2.2.2's grammar allows it,
            # and "Disallow:" has meant "nothing is disallowed" since 1994).
            if value:
                rules.append(Rule(allow=key == "allow", pattern=_normalise(value)))
        elif key == "crawl-delay":
            in_rules = True
            parsed = _seconds(value)
            if parsed is not None:
                delay = parsed if delay is None else max(delay, parsed)
        # Anything else (sitemap, host, ...) is not a group member and is ignored,
        # without ending the group it appears in.
    close()
    return RobotsFile(tuple(groups))


def _agent(value: str) -> str:
    """The product token a ``user-agent`` line names, or ``*``.

    ``Googlebot/2.1`` names ``googlebot``: everything after the token's own
    characters is a version or a comment, not part of the name.
    """
    if value.startswith("*"):
        return "*"
    match = _TOKEN.match(value)
    return match.group(0) if match else ""


def _seconds(value: str) -> float | None:
    try:
        seconds = float(value)
    except ValueError:
        return None
    if seconds != seconds or seconds < 0 or seconds == float("inf"):
        return None
    return seconds


def _target(url: str) -> str:
    """The part of ``url`` a rule is matched against: path and query, normalised."""
    parts = urlsplit(url)
    path = parts.path or "/"
    if parts.query:
        path = f"{path}?{parts.query}"
    return _normalise(path)


def _normalise(path: str) -> str:
    """Percent-encode non-ASCII as UTF-8 and decode encoded unreserved characters.

    Applied to both sides, so ``/a/%62`` and ``/a/b`` compare equal while an
    encoded reserved character such as ``%2F`` stays distinct from ``/``.
    """
    out: list[str] = []
    i = 0
    while i < len(path):
        char = path[i]
        if char == "%" and _is_hex_pair(path, i + 1):
            decoded = chr(int(path[i + 1 : i + 3], 16))
            out.append(decoded if decoded in _UNRESERVED else path[i : i + 3].upper())
            i += 3
            continue
        if ord(char) > 127:
            out.extend(f"%{byte:02X}" for byte in char.encode("utf-8"))
        else:
            out.append(char)
        i += 1
    return "".join(out)


def _is_hex_pair(text: str, start: int) -> bool:
    return start + 1 < len(text) and text[start] in _HEX and text[start + 1] in _HEX


def _matches(pattern: str, path: str) -> bool:
    """Match ``*`` and a trailing ``$`` without regular expressions.

    A regular expression built from a hostile ``robots.txt`` is a denial of
    service waiting to happen (Protego fixed one, CVE-2026-55520). Pieces between
    stars are found left to right, each at its earliest position, which is
    enough: taking a later position for a piece can only leave less room for the
    pieces after it.
    """
    anchored = pattern.endswith("$")
    if anchored:
        pattern = pattern[:-1]
    pieces = pattern.split("*")
    first, rest = pieces[0], pieces[1:]
    if not path.startswith(first):
        return False
    if not rest:
        return not anchored or len(path) == len(first)
    position = len(first)
    for piece in rest[:-1]:
        found = path.find(piece, position)
        if found < 0:
            return False
        position = found + len(piece)
    last = rest[-1]
    if anchored:
        return len(path) - len(last) >= position and path.endswith(last)
    return path.find(last, position) >= 0
