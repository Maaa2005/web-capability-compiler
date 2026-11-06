"""Draw the held-out candidates from the CrUX frame, exactly as ``PLAN.md`` says.

Pure apart from reading the frame file, so the draw can be repeated by anyone
holding the same file and checked against the committed ``sample.json``.
"""

from __future__ import annotations

import csv
import functools
import gzip
import hashlib
import io
import random
from collections import Counter
from collections.abc import Mapping
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import NamedTuple
from urllib.parse import urlsplit

from benchmarks.heldout import plan

SAMPLE_PATH = Path(__file__).resolve().parent / "sample.json"
BLOCKLIST_PATH = Path(__file__).resolve().parent / "blocked_in_frame.txt"
SUFFIX_LIST_PATH = Path(__file__).resolve().parent / "public_suffix_list.dat"


@dataclass(frozen=True)
class Candidate:
    stratum: str
    position: int
    """1-based order within the stratum. Candidates are used strictly in this order."""

    origin: str
    rank_bucket: int

    @property
    def url(self) -> str:
        return f"{self.origin}/"

    @property
    def host(self) -> str:
        return urlsplit(self.origin).hostname or ""


@dataclass
class Draw:
    candidates: list[Candidate]
    excluded: Counter[str] = field(default_factory=Counter)
    """How many origins each rule E1 to E5 removed while walking the shuffles."""


class FrameMismatch(Exception):
    """The frame file is not the one the plan names."""


def read_frame(path: Path, expected_sha256: str = plan.FRAME_SHA256) -> list[tuple[str, int]]:
    """Read ``origin,rank`` rows, refusing a file whose hash is not the plan's."""
    raw = path.read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    if digest != expected_sha256:
        raise FrameMismatch(f"{path} has SHA-256 {digest}; the plan names {expected_sha256}")
    text = gzip.decompress(raw).decode("utf-8")
    rows = []
    for row in csv.DictReader(io.StringIO(text)):
        rows.append((row["origin"].strip(), int(row["rank"])))
    return rows


def on_development_domain(host: str) -> bool:
    return any(host == d or host.endswith(f".{d}") for d in plan.DEVELOPMENT_DOMAINS)


class SuffixListMismatch(Exception):
    """The Public Suffix List file is not the one the plan names."""


class BlocklistMismatch(Exception):
    """A blocklist file is not the one the plan names."""


def read_blocklist(path: Path, expected_sha256: str) -> frozenset[str]:
    """The domains in one blocklist file, refusing a file whose hash is not the plan's.

    One domain per line, or hosts-file lines (``0.0.0.0 domain``): the last
    whitespace-separated token of each line that is not blank or a ``#`` comment.
    """
    raw = path.read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    if digest != expected_sha256:
        raise BlocklistMismatch(f"{path} has SHA-256 {digest}; the plan names {expected_sha256}")
    domains: set[str] = set()
    for line in raw.decode("utf-8", errors="replace").splitlines():
        line = line.split("#", 1)[0].strip()
        if line:
            domains.add(line.split()[-1].lower().rstrip("."))
    return frozenset(domains)


def build_frame_blocklist(
    rows: list[tuple[str, int]], category_files: dict[str, Path]
) -> list[str]:
    """The frame's hosts that any pinned UT1 category file lists, themselves or by parent.

    Each category file is checked against the SHA-256 the plan names, then the
    frame's hosts are matched with :func:`blocked`. The result is what gets
    committed: small enough to read, and enough to repeat the draw without the
    4.6 million lines it was derived from.
    """
    domains: set[str] = set()
    for name, path in category_files.items():
        domains |= read_blocklist(path, plan.UT1_CATEGORIES[name])
    frozen = frozenset(domains)
    hosts = {urlsplit(origin).hostname or "" for origin, _ in rows}
    return sorted(host for host in hosts if host and blocked(host, frozen))


def blocked(host: str, domains: frozenset[str]) -> bool:
    """Whether ``host`` or any parent domain of it is in ``domains``."""
    labels = host.lower().rstrip(".").split(".")
    return any(".".join(labels[i:]) in domains for i in range(len(labels) - 1))


def exclusion(origin: str, blocklist: frozenset[str] = frozenset()) -> str | None:
    """The rule among E1 to E3 and E5 that excludes ``origin``, or ``None``."""
    parts = urlsplit(origin)
    if parts.scheme != "https":
        return "E1"
    host = (parts.hostname or "").lower()
    if on_development_domain(host):
        return "E2"
    if any(word in host for word in plan.ADULT_HOST_WORDS):
        return "E3"
    if blocked(host, blocklist):
        return "E5"
    return None


class SuffixRules(NamedTuple):
    normal: frozenset[str]
    wildcard: frozenset[str]
    exception: frozenset[str]


@functools.lru_cache(maxsize=1)
def suffix_rules() -> SuffixRules:
    """The pinned Public Suffix List, refusing a file whose hash is not the plan's."""
    raw = SUFFIX_LIST_PATH.read_bytes()
    digest = hashlib.sha256(raw).hexdigest()
    if digest != plan.SUFFIX_LIST_SHA256:
        raise SuffixListMismatch(
            f"{SUFFIX_LIST_PATH} has SHA-256 {digest}; the plan names {plan.SUFFIX_LIST_SHA256}"
        )
    normal: set[str] = set()
    wildcard: set[str] = set()
    exception: set[str] = set()
    for line in raw.decode("utf-8").splitlines():
        rule = line.strip()
        if not rule or rule.startswith("//"):
            continue
        rule = rule.split()[0].lower()
        if rule.startswith("!"):
            exception.add(_ascii_rule(rule[1:]))
        elif rule.startswith("*."):
            wildcard.add(_ascii_rule(rule[2:]))
        else:
            normal.add(_ascii_rule(rule))
    return SuffixRules(frozenset(normal), frozenset(wildcard), frozenset(exception))


def _ascii_rule(rule: str) -> str:
    """A rule in the same A-label form that hosts arrive in."""
    return rule.encode("idna").decode("ascii")


def site_key(host: str) -> str:
    """E4's notion of "the same site": the registrable domain, by the pinned list.

    The list's own algorithm: the longest matching rule wins, and an exception rule
    beats a wildcard. No rule means the last label is the suffix. A host that is
    itself a public suffix, or an address, is its own site.
    """
    host = host.lower().rstrip(".")
    labels = host.split(".")
    if ":" in host or labels[-1].isdigit():
        return host
    rules = suffix_rules()
    suffix_length = 1
    for start in range(len(labels)):
        name = ".".join(labels[start:])
        if name in rules.exception:
            suffix_length = len(labels) - start - 1
            break
        parent = ".".join(labels[start + 1 :])
        if name in rules.normal or parent in rules.wildcard:
            suffix_length = len(labels) - start
            break
    if suffix_length >= len(labels):
        return host
    return ".".join(labels[-(suffix_length + 1) :])


def landing_exclusion(final_url: str, kept: Mapping[str, str]) -> str | None:
    """E2 or E4 for the page a candidate ended on, which the draw could not see.

    The draw judged the origin. A redirect can still put a development site in the
    run, or a second page of a site already kept. ``kept`` maps each site so far to
    its page's key.
    """
    host = (urlsplit(final_url).hostname or "").lower()
    if not host:
        return None
    if on_development_domain(host):
        return f"E2: it landed on {host}, a development domain"
    earlier = kept.get(site_key(host))
    return None if earlier is None else f"E4: it landed on {host}, the site of {earlier}"


def draw(
    rows: list[tuple[str, int]],
    seed: int = plan.SEED,
    strata: tuple[plan.Stratum, ...] = plan.STRATA,
    per_stratum: int = plan.CANDIDATES_PER_STRATUM,
    blocklist: frozenset[str] = frozenset(),
) -> Draw:
    rng = random.Random(seed)
    result = Draw(candidates=[])
    kept_sites: set[str] = set()
    for stratum in strata:
        pool = sorted(origin for origin, rank in rows if rank in stratum.ranks)
        ranks = {origin: rank for origin, rank in rows if rank in stratum.ranks}
        rng.shuffle(pool)
        position = 0
        for origin in pool:
            if position == per_stratum:
                break
            rule = exclusion(origin, blocklist)
            if rule is None:
                key = site_key(urlsplit(origin).hostname or "")
                if key in kept_sites:
                    rule = "E4"
                else:
                    kept_sites.add(key)
            if rule is not None:
                result.excluded[rule] += 1
                continue
            position += 1
            result.candidates.append(Candidate(stratum.name, position, origin, ranks[origin]))
    return result


def sample_document(result: Draw) -> dict[str, object]:
    return {
        "plan": "benchmarks/heldout/PLAN.md",
        "frame": {
            "repository": plan.FRAME_REPOSITORY,
            "commit": plan.FRAME_COMMIT,
            "path": plan.FRAME_PATH,
            "sha256": plan.FRAME_SHA256,
        },
        "seed": plan.SEED,
        "strata": [asdict(s) for s in plan.STRATA],
        "candidates_per_stratum": plan.CANDIDATES_PER_STRATUM,
        "blocklists": [{"name": b.name, "url": b.url, "sha256": b.sha256} for b in plan.BLOCKLISTS],
        "excluded_while_drawing": dict(sorted(result.excluded.items())),
        "candidates": [asdict(c) for c in result.candidates],
    }


def load_sample(path: Path = SAMPLE_PATH) -> list[Candidate]:
    import json

    document = json.loads(path.read_text(encoding="utf-8"))
    return [Candidate(**entry) for entry in document["candidates"]]
