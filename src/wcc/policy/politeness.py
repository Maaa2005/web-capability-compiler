"""Asking a site for a page the way it asked to be asked.

Two duties, both decided before a top-level document is requested:

**``robots.txt``.** Each origin's file is fetched once, cached, and consulted for
the product token ``wcc`` (:mod:`wcc.policy.robots`). WCC opens pages because a
model asked it to, which is the case the reference MCP ``fetch`` server treats
as autonomous and checks; it skips the check only for a page a person asked for
through an MCP prompt, and WCC has no such path.

**Pacing.** Requests to one host start at least ``min_interval_seconds`` apart,
further apart when the site's ``Crawl-delay`` says so, and not before a 429 or
503's ``Retry-After`` has passed. A wait the runtime is not willing to hold a
navigation open for is refused with the time to retry instead, because a
navigation parked for a minute inside a tool call looks to the agent like a hang.

How the answers were chosen, and what was left out, is in ``docs/progress.md``
("The runtime asks before it fetches"). The parts that matter when reading code:

- *Unreachable* (5xx, 429, no answer, a refused redirect hop) disallows, as RFC
  9309 section 2.3.1.4 says. A copy cached from an earlier success is used in its
  place for as long as the site stays unreachable (section 2.4).
- *Unavailable* (other 4xx, or more than five redirects) restricts nothing, as
  section 2.3.1.3 says.
- 401 and 403 disallow everything. RFC 9309 counts them as unavailable; this
  follows the reference MCP ``fetch`` server and Python's own reader instead,
  reading a refusal to show the rules as a refusal.
- Every hop of the ``robots.txt`` fetch passes the same URL policy as a page, so
  this module cannot be made to request an address the browser could not.
"""

from __future__ import annotations

import asyncio
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass, field
from datetime import UTC
from email.utils import parsedate_to_datetime
from enum import StrEnum
from http.client import HTTPMessage
from typing import IO
from urllib.error import HTTPError
from urllib.parse import urljoin, urlsplit
from urllib.request import HTTPRedirectHandler, OpenerDirector, Request, build_opener

from wcc.compiler.route import origin_of
from wcc.driver.base import (
    NavigationBlockedError,
    NavigationGuard,
    RateLimitedError,
    RobotsDisallowedError,
)
from wcc.policy.robots import MAX_ROBOTS_BYTES, PRODUCT_TOKEN, RobotsFile, parse_robots

#: RFC 9309 section 2.3.1.2 asks a crawler to follow at least five consecutive
#: redirects when fetching ``robots.txt``, and lets it treat more as unavailable.
MAX_ROBOTS_REDIRECTS = 5


@dataclass(frozen=True)
class PolitenessSettings:
    """How politely to ask. Set by whoever launches the process, never by an agent."""

    product_token: str = PRODUCT_TOKEN
    user_agent: str = PRODUCT_TOKEN
    """Sent when fetching ``robots.txt``. :mod:`wcc.config` sets the same string the
    browser appends to its own User-Agent."""

    min_interval_seconds: float = 1.0
    """Between the starts of two requests to one host. Scrapy's project template
    uses one second and one request at a time per domain."""

    max_wait_seconds: float = 2.0
    """The longest a navigation is held for its slot. Longer waits are refused with
    the time to retry. Kept short because an action's settle budget is five
    seconds, and a navigation still parked when it expires would be reported as an
    effect that did not happen rather than one that was postponed."""

    robots_timeout_seconds: float = 5.0
    robots_ttl_seconds: float = 24 * 60 * 60
    """RFC 9309 section 2.4: a cached copy should not be used for more than 24
    hours unless the file is unreachable."""

    unreachable_ttl_seconds: float = 5 * 60
    """How long a failed fetch is remembered before it is tried again, so a host
    that does not answer costs one timeout per five minutes rather than one per
    navigation."""

    backoff_seconds: float = 30.0
    """How long a 429 or 503 without ``Retry-After`` holds the host back."""

    max_backoff_seconds: float = 24 * 60 * 60


class RobotsStatus(StrEnum):
    PARSED = "parsed"
    UNAVAILABLE = "unavailable"
    UNREACHABLE = "unreachable"
    RESTRICTED = "restricted"


@dataclass(frozen=True)
class RobotsAnswer:
    """What fetching one origin's ``robots.txt`` established."""

    status: RobotsStatus
    robots_url: str
    file: RobotsFile | None = None
    reason: str = ""


@dataclass(frozen=True)
class FetchedResponse:
    status: int
    location: str | None
    body: bytes


Fetch = Callable[[str, float, str], FetchedResponse]
"""``(url, timeout_seconds, user_agent)`` to one response, without following
redirects. Raises on a network failure. Blocking; it runs in a worker thread."""


class _NoRedirect(HTTPRedirectHandler):
    """Hand a 3xx back to the caller, which checks the next hop before taking it."""

    def redirect_request(
        self,
        req: Request,
        fp: IO[bytes],
        code: int,
        msg: str,
        headers: HTTPMessage,
        newurl: str,
    ) -> Request | None:
        del req, fp, code, msg, headers, newurl
        return None


_OPENER: OpenerDirector = build_opener(_NoRedirect)


def http_get(url: str, timeout: float, user_agent: str) -> FetchedResponse:
    """One GET with the standard library, reading at most :data:`MAX_ROBOTS_BYTES`."""
    request = Request(url, headers={"User-Agent": user_agent})
    try:
        with _OPENER.open(request, timeout=timeout) as response:
            return FetchedResponse(response.status, None, response.read(MAX_ROBOTS_BYTES))
    except HTTPError as answered:
        location = answered.headers.get("Location") if answered.headers else None
        answered.close()
        return FetchedResponse(answered.code, location, b"")


@dataclass
class _Cached:
    answer: RobotsAnswer
    expires_at: float


@dataclass
class PoliteAccess:
    """The :class:`~wcc.driver.base.DocumentAdmission` the runtime uses.

    One instance per process, shared by every session, so two sessions on the
    same host share its pace. ``clock``, ``wall_clock``, ``sleep`` and ``fetch``
    are injectable for tests; nothing else should replace them.
    """

    guard: NavigationGuard
    settings: PolitenessSettings = field(default_factory=PolitenessSettings)
    fetch: Fetch = http_get
    clock: Callable[[], float] = time.monotonic
    wall_clock: Callable[[], float] = time.time
    sleep: Callable[[float], Awaitable[None]] = asyncio.sleep

    _robots: dict[tuple[str, bool], _Cached] = field(default_factory=dict, init=False)
    _last_good: dict[tuple[str, bool], RobotsAnswer] = field(default_factory=dict, init=False)
    _fetching: dict[tuple[str, bool], asyncio.Lock] = field(default_factory=dict, init=False)
    _last_start: dict[str, float] = field(default_factory=dict, init=False)
    _not_before: dict[str, float] = field(default_factory=dict, init=False)

    async def admit(
        self, url: str, *, allow_private_network: bool, redirected_from: str | None = None
    ) -> None:
        answer = await self.robots(url, allow_private_network=allow_private_network)
        self._check_robots(answer, url)
        crawl_delay = answer.file.crawl_delay(self.settings.product_token) if answer.file else None
        await self._take_slot(url, crawl_delay, redirected_from)

    def observe(self, url: str, status: int, retry_after: str | None) -> None:
        if status not in (429, 503):
            return
        delay = _retry_after_seconds(retry_after, self.wall_clock())
        if delay is None:
            delay = self.settings.backoff_seconds
        delay = min(delay, self.settings.max_backoff_seconds)
        host = _host(url)
        self._not_before[host] = max(self._not_before.get(host, 0.0), self.clock() + delay)

    # -- robots.txt --------------------------------------------------------

    async def robots(self, url: str, *, allow_private_network: bool) -> RobotsAnswer:
        """The answer for ``url``'s origin, from the cache when it is fresh."""
        key = (origin_of(url), allow_private_network)
        lock = self._fetching.setdefault(key, asyncio.Lock())
        async with lock:
            cached = self._robots.get(key)
            if cached is not None and cached.expires_at > self.clock():
                return cached.answer
            answer = await self._fetch_robots(key[0], allow_private_network)
            if answer.status is RobotsStatus.UNREACHABLE:
                ttl = self.settings.unreachable_ttl_seconds
                answer = self._last_good.get(key, answer)
            else:
                ttl = self.settings.robots_ttl_seconds
                self._last_good[key] = answer
            self._robots[key] = _Cached(answer, self.clock() + ttl)
            return answer

    async def _fetch_robots(self, origin: str, allow_private_network: bool) -> RobotsAnswer:
        robots_url = f"{origin}/robots.txt"
        timeout = self.settings.robots_timeout_seconds
        url = robots_url
        for _ in range(MAX_ROBOTS_REDIRECTS + 1):
            try:
                await asyncio.to_thread(
                    self.guard.check, url, allow_private_network=allow_private_network
                )
                response = await asyncio.wait_for(
                    asyncio.to_thread(self.fetch, url, timeout, self.settings.user_agent),
                    timeout + 1,
                )
            except NavigationBlockedError as refused:
                return _unreachable(robots_url, f"{url} is refused by the URL policy: {refused}")
            except TimeoutError:
                return _unreachable(robots_url, f"no answer within {timeout:g} s")
            except Exception as failed:  # network, TLS, and malformed-response errors
                return _unreachable(robots_url, f"{type(failed).__name__}: {failed}")

            status = response.status
            if 300 <= status < 400 and response.location:
                url = urljoin(url, response.location)
                continue
            if 200 <= status < 300:
                return RobotsAnswer(RobotsStatus.PARSED, robots_url, parse_robots(response.body))
            if status in (401, 403):
                return RobotsAnswer(
                    RobotsStatus.RESTRICTED, robots_url, reason=f"it answered {status}"
                )
            if 400 <= status < 500 and status != 429:
                return RobotsAnswer(
                    RobotsStatus.UNAVAILABLE, robots_url, reason=f"it answered {status}"
                )
            # 5xx, 429 (which Google also reads as a server error), and anything
            # else that is neither rules nor a clear "there are none".
            return _unreachable(robots_url, f"it answered {status}")
        return RobotsAnswer(
            RobotsStatus.UNAVAILABLE,
            robots_url,
            reason=f"more than {MAX_ROBOTS_REDIRECTS} redirects",
        )

    def _check_robots(self, answer: RobotsAnswer, url: str) -> None:
        token = self.settings.product_token
        if answer.status is RobotsStatus.UNREACHABLE:
            raise RobotsDisallowedError(
                f"{answer.robots_url} could not be read ({answer.reason}); an unreachable "
                "robots.txt disallows everything (RFC 9309 section 2.3.1.4)"
            )
        if answer.status is RobotsStatus.RESTRICTED:
            raise RobotsDisallowedError(
                f"{answer.robots_url} could not be read ({answer.reason}), which is taken "
                "as disallowing everything"
            )
        if answer.file is not None and not answer.file.allows(url, token):
            path = urlsplit(url).path or "/"
            raise RobotsDisallowedError(f"{answer.robots_url} does not allow {token!r} {path}")

    # -- pacing ------------------------------------------------------------

    async def _take_slot(
        self, url: str, crawl_delay: float | None, redirected_from: str | None
    ) -> None:
        """Reserve the host's next slot and wait for it, or refuse if it is too far.

        Reserved before sleeping and without a lock: nothing between reading the
        last start and writing the new one awaits, so two navigations to the same
        host queue one interval apart instead of both taking the same slot.
        """
        host = _host(url)
        now = self.clock()
        earliest = self._not_before.get(host, now)
        # A redirect on the same host is the server's own answer to the request
        # that was just paced; spacing it again would charge one request twice.
        # A redirect to another host is a first request there and is paced.
        same_host_hop = redirected_from is not None and _host(redirected_from) == host
        if not same_host_hop and host in self._last_start:
            interval = max(self.settings.min_interval_seconds, crawl_delay or 0.0)
            earliest = max(earliest, self._last_start[host] + interval)
        wait = max(0.0, earliest - now)
        if wait > self.settings.max_wait_seconds:
            raise RateLimitedError(
                f"the next request to {host} may start in {wait:.1f} s; retry after that",
                retry_after_seconds=wait,
            )
        self._last_start[host] = now + wait
        if wait > 0:
            await self.sleep(wait)


def _unreachable(robots_url: str, reason: str) -> RobotsAnswer:
    return RobotsAnswer(RobotsStatus.UNREACHABLE, robots_url, reason=reason)


def _host(url: str) -> str:
    return (urlsplit(url).hostname or "").lower()


def _retry_after_seconds(value: str | None, now: float) -> float | None:
    """Read ``Retry-After`` in either form RFC 9110 section 10.2.3 allows."""
    if value is None:
        return None
    value = value.strip()
    if value.isdigit():
        return float(value)
    try:
        when = parsedate_to_datetime(value)
    except (TypeError, ValueError, IndexError):
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    return max(0.0, when.timestamp() - now)
