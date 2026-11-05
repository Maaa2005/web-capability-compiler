"""Asking a site for a page the way it asked to be asked.

Two duties, both decided before a top-level document is requested:

**``robots.txt``.** Each origin's file is fetched once, cached, and consulted for
the product token ``wcc`` (:mod:`wcc.policy.robots`). WCC opens pages because a
model asked it to, which is the case the reference MCP ``fetch`` server treats
as autonomous and checks; it skips the check only for a page a person asked for
through an MCP prompt, and WCC has no such path.

**Pacing.** Requests to one host start at least ``min_interval_seconds`` apart,
further apart when the site's ``Crawl-delay`` says so, and not before a 429 or
503's ``Retry-After`` has passed. Fetching ``robots.txt`` is a request like any
other: it takes a slot, and it can answer 429 or 503 too. So is a redirect hop,
to the same host or another. A wait the runtime is not willing to hold a navigation
open for is refused with the time to retry instead, because a navigation parked
for a minute inside a tool call looks to the agent like a hang.

How the answers were chosen, and what was left out, is in ``docs/progress.md``
("The runtime asks before it fetches"). The parts that matter when reading code:

- *Unreachable* (5xx, 429, no answer, a refused redirect hop, a body cut off before
  the end it declared) disallows, as RFC 9309 section 2.3.1.4 says. A copy cached
  from an earlier success is used in its place for as long as the site stays
  unreachable (section 2.4).
- *Unavailable* (other 4xx, or more than five redirects) restricts nothing, as
  section 2.3.1.3 says.
- 401 and 403 disallow everything. RFC 9309 counts them as unavailable; this
  follows the reference MCP ``fetch`` server and Python's own reader instead,
  reading a refusal to show the rules as a refusal.
- Every hop of the ``robots.txt`` fetch passes the same URL policy as a page, and
  connects to the addresses the policy judged rather than resolving the name again
  (:func:`http_get`), so neither a redirect nor a DNS answer that changes after the
  check can make this module request an address the browser could not.
- A refusal says what kind of answer the file got, not which status it was. The
  text reaches the agent, and it should not learn more about a host from asking
  for its ``robots.txt`` than the rules themselves.
- The fetch has one deadline and one size limit, kept by the thread doing the
  reading. It runs on a daemon thread, because the default executor is joined when
  the event loop shuts down and one slow site must not hold the process open.
- ``Retry-After`` is kept however long it is, because the promise is to honour it.
  Nothing sleeps through a long one: it is refused, with the time to retry.
"""

from __future__ import annotations

import asyncio
import contextlib
import functools
import io
import logging
import socket
import ssl
import threading
import time
from collections.abc import Awaitable, Callable, Sequence
from dataclasses import dataclass, field
from datetime import UTC
from email.utils import parsedate_to_datetime
from enum import StrEnum
from http.client import HTTPConnection, HTTPResponse, IncompleteRead
from urllib.parse import urljoin, urlsplit

from wcc.compiler.route import origin_of
from wcc.driver.base import (
    NavigationBlockedError,
    NavigationGuard,
    RateLimitedError,
    RobotsDisallowedError,
)
from wcc.policy.robots import MAX_ROBOTS_BYTES, PRODUCT_TOKEN, RobotsFile, parse_robots
from wcc.policy.url_policy import Resolver, UrlPolicy, resolve_addresses

logger = logging.getLogger(__name__)

#: RFC 9309 section 2.3.1.2 asks a crawler to follow at least five consecutive
#: redirects when fetching ``robots.txt``, and lets it treat more as unavailable.
MAX_ROBOTS_REDIRECTS = 5

#: How much longer than its own deadline the event loop waits for the worker. Only a
#: lookup can overrun, since ``getaddrinfo`` takes no timeout; the reads cannot.
_ABANDON_GRACE_SECONDS = 1.0

#: Where an absurd ``Retry-After`` stops counting. Longer than any process holding
#: this state lives, so it shortens nothing a site could notice. It exists because
#: ``float`` reads four hundred digits as infinity, and the wait is shown to the agent.
_LONGEST_RETRY_AFTER_SECONDS = 100.0 * 365 * 24 * 60 * 60


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
    """For the whole fetch of one origin's file: every redirect hop and every byte. Not
    per read, or a site sending one byte at a time could hold the fetch open for as
    long as it liked. The waits for each hop's slot are not in it: they are this
    runtime's own pacing, not the site being slow, and five hops on one host at one per
    second would otherwise use it all up. ``max_wait_seconds`` limits each of those."""

    robots_ttl_seconds: float = 24 * 60 * 60
    """RFC 9309 section 2.4: a cached copy should not be used for more than 24
    hours unless the file is unreachable."""

    unreachable_ttl_seconds: float = 5 * 60
    """How long a failed fetch is remembered before it is tried again, so a host
    that does not answer costs one timeout per five minutes rather than one per
    navigation."""

    backoff_seconds: float = 30.0
    """How long a 429 or 503 without ``Retry-After`` holds the host back."""


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
    retry_after: str | None = None


Fetch = Callable[[str, float, str], FetchedResponse]
"""``(url, timeout_seconds, user_agent)`` to one response, without following
redirects. Raises on a network failure. Blocking; it runs in a worker thread."""


def _remaining(deadline: float) -> float:
    """Seconds left before ``deadline``; raises once there are none."""
    left = deadline - time.monotonic()
    if left <= 0:
        raise TimeoutError("the deadline passed")
    return left


class _DeadlineReader(io.RawIOBase):
    """Reads from a socket, with one deadline shared by every read.

    A socket timeout restarts with each ``recv``, so a peer sending one byte per
    timeout would never trip it, whether in the status line, the headers or the body.
    """

    def __init__(self, sock: socket.socket, deadline: float) -> None:
        super().__init__()
        self._sock = sock
        self._deadline = deadline

    def readable(self) -> bool:
        return True

    def readinto(self, buffer: bytearray | memoryview) -> int:  # type: ignore[override]
        self._sock.settimeout(_remaining(self._deadline))
        return self._sock.recv_into(buffer)


class _DeadlineSocket:
    """What :mod:`http.client` is given in place of a socket.

    ``close`` does nothing because ``http.client`` closes its connection as soon as
    it has a response that ends with the connection, relying on the reference count
    a real ``makefile`` keeps. :func:`http_get` owns the socket and closes it.
    """

    def __init__(self, sock: socket.socket, deadline: float) -> None:
        self._sock = sock
        self._deadline = deadline

    def sendall(self, data: bytes) -> None:
        self._sock.settimeout(_remaining(self._deadline))
        self._sock.sendall(data)

    def makefile(self, mode: str = "rb") -> io.BufferedReader:
        del mode
        return io.BufferedReader(_DeadlineReader(self._sock, self._deadline))

    def close(self) -> None:
        pass


def _connect(addresses: Sequence[str], port: int, deadline: float) -> socket.socket:
    """Connect to the first address that answers, sharing the time between them."""
    last: OSError | None = None
    for index, address in enumerate(addresses):
        share = _remaining(deadline) / (len(addresses) - index)
        try:
            return socket.create_connection((address, port), timeout=share)
        except OSError as failed:
            last = failed
    raise last if last is not None else OSError("no address to connect to")


def _read_capped(response: HTTPResponse) -> bytes:
    """At most :data:`MAX_ROBOTS_BYTES` of the body, one network read at a time.

    Raises :class:`IncompleteRead` on a body that ends before its ``Content-Length``,
    which ``read1`` reports only as an empty read. What is missing may be the rule that
    refuses the page. A chunked body raises by itself, and a body that declares no
    length ends where the connection does, so nothing can be said about those.
    """
    chunks: list[bytes] = []
    left = MAX_ROBOTS_BYTES
    while left > 0:
        chunk = response.read1(left)
        if not chunk:
            if response.length:
                raise IncompleteRead(b"".join(chunks), response.length)
            break
        chunks.append(chunk)
        left -= len(chunk)
    return b"".join(chunks)


def http_get(
    url: str, timeout: float, user_agent: str, addresses: Sequence[str]
) -> FetchedResponse:
    """One GET to one of ``addresses``, without following a redirect.

    ``addresses`` are what the URL policy already judged. The name in ``url`` is used
    only for the ``Host`` header and for checking the certificate, and is never looked
    up, so there is no moment between the judgement and the connection for a changed
    DNS answer to use. :mod:`wcc.driver.egress_proxy` holds the browser to the same
    rule.

    ``timeout`` covers everything from connecting to the last byte, and at most
    :data:`MAX_ROBOTS_BYTES` of the body is kept.
    """
    deadline = time.monotonic() + timeout
    parts = urlsplit(url)
    host = parts.hostname or ""
    secure = parts.scheme.lower() == "https"
    port = parts.port or (443 if secure else 80)
    sock = _connect(addresses, port, deadline)
    try:
        if secure:
            sock.settimeout(_remaining(deadline))
            # Takes the socket over, and closes it if the handshake fails.
            sock = ssl.create_default_context().wrap_socket(sock, server_hostname=host)
        connection = HTTPConnection(host, port)
        # A connection that already has a socket never opens one of its own.
        connection.sock = _DeadlineSocket(sock, deadline)  # type: ignore[assignment]
        target = (parts.path or "/") + (f"?{parts.query}" if parts.query else "")
        connection.request(
            "GET",
            target,
            headers={"Host": parts.netloc, "User-Agent": user_agent, "Connection": "close"},
        )
        with connection.getresponse() as response:
            body = _read_capped(response) if 200 <= response.status < 300 else b""
            return FetchedResponse(
                response.status,
                response.getheader("Location"),
                body,
                response.getheader("Retry-After"),
            )
    finally:
        sock.close()


async def _on_worker_thread[T](work: Callable[[], T]) -> T:
    """Run blocking ``work`` on a daemon thread of its own.

    Not :func:`asyncio.to_thread`: its pool is joined when the event loop shuts down,
    so a worker stuck in ``getaddrinfo``, which no timeout reaches, would hold up the
    exit of a process that had already stopped waiting for it.
    """
    loop = asyncio.get_running_loop()
    future: asyncio.Future[T] = loop.create_future()

    def deliver(result: T | None, failure: BaseException | None) -> None:
        if future.done():
            return  # The caller gave up on this worker.
        if failure is not None:
            future.set_exception(failure)
        else:
            future.set_result(result)  # type: ignore[arg-type]

    def run() -> None:
        result: T | None = None
        failure: BaseException | None = None
        try:
            result = work()
        except BaseException as failed:  # handed to the awaiting coroutine
            failure = failed
        # The loop may be gone by the time an abandoned worker finishes.
        with contextlib.suppress(RuntimeError):
            loop.call_soon_threadsafe(deliver, result, failure)

    threading.Thread(target=run, name="wcc-robots", daemon=True).start()
    return await future


@dataclass
class _Cached:
    answer: RobotsAnswer
    expires_at: float


@dataclass
class PoliteAccess:
    """The :class:`~wcc.driver.base.DocumentAdmission` the runtime uses.

    One instance per process, shared by every session, so two sessions on the
    same host share its pace. ``clock``, ``wall_clock``, ``sleep`` and ``fetch``
    are injectable for tests; nothing else should replace them. Leaving ``fetch``
    out is what connects to the addresses the policy judged.
    """

    guard: NavigationGuard
    settings: PolitenessSettings = field(default_factory=PolitenessSettings)
    fetch: Fetch | None = None
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
        try:
            await self._take_slot(url, crawl_delay)
        except RateLimitedError as refused:
            if redirected_from is None:
                raise
            # Asking for the first URL again would spend the slot on it, be redirected
            # here again and be refused again, however long the agent waited.
            raise RateLimitedError(
                f"{refused.message}, asking for {url}, where the redirect was going",
                retry_after_seconds=refused.retry_after_seconds,
            ) from refused

    def observe(self, url: str, status: int, retry_after: str | None) -> None:
        if status not in (429, 503):
            return
        delay = _retry_after_seconds(retry_after, self.wall_clock())
        if delay is None:
            delay = self.settings.backoff_seconds
        delay = min(delay, _LONGEST_RETRY_AFTER_SECONDS)
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
        deadline = self.clock() + timeout
        url = robots_url
        for _ in range(MAX_ROBOTS_REDIRECTS + 1):
            before_the_wait = self.clock()
            # Outside the ``try``: a host holding requests back refuses this
            # navigation with the time to retry, and is not "unreachable".
            await self._take_slot(url, None)
            deadline += self.clock() - before_the_wait
            left = deadline - self.clock()
            if left <= 0:
                return _unreachable(robots_url, f"no answer within {timeout:g} s")
            try:
                response = await asyncio.wait_for(
                    _on_worker_thread(
                        functools.partial(self._fetch_hop, url, left, allow_private_network)
                    ),
                    left + _ABANDON_GRACE_SECONDS,
                )
            except NavigationBlockedError as refused:
                return _unreachable(robots_url, f"{url} is refused by the URL policy: {refused}")
            except TimeoutError:
                return _unreachable(robots_url, f"no answer within {timeout:g} s")
            except Exception as failed:  # network, TLS, and malformed-response errors
                logger.info("fetching %s failed: %s", url, failed)
                return _unreachable(robots_url, f"the fetch failed ({type(failed).__name__})")

            status = response.status
            self.observe(url, status, response.retry_after)
            if 300 <= status < 400 and response.location:
                url = urljoin(url, response.location)
                continue
            logger.info("%s answered %d", url, status)
            if 200 <= status < 300:
                return RobotsAnswer(RobotsStatus.PARSED, robots_url, parse_robots(response.body))
            if status in (401, 403):
                return RobotsAnswer(
                    RobotsStatus.RESTRICTED, robots_url, reason="the site refused to show it"
                )
            if 400 <= status < 500 and status != 429:
                return RobotsAnswer(
                    RobotsStatus.UNAVAILABLE, robots_url, reason="the site has none to show"
                )
            # 5xx, 429 (which Google also reads as a server error), and anything
            # else that is neither rules nor a clear "there are none".
            return _unreachable(robots_url, "the site answered with an error")
        return RobotsAnswer(
            RobotsStatus.UNAVAILABLE,
            robots_url,
            reason=f"more than {MAX_ROBOTS_REDIRECTS} redirects",
        )

    def _fetch_hop(self, url: str, timeout: float, allow_private_network: bool) -> FetchedResponse:
        """Judge ``url`` and ask it. Blocking; runs on the worker thread.

        Judged here rather than before the wait for the slot, so the answer that is
        connected to is the newest one. The time a lookup takes comes out of
        ``timeout``.
        """
        deadline = time.monotonic() + timeout
        self.guard.check(url, allow_private_network=allow_private_network)
        user_agent = self.settings.user_agent
        if self.fetch is not None:
            return self.fetch(url, timeout, user_agent)
        addresses = self._judged_addresses(url, allow_private_network)
        return http_get(url, _remaining(deadline), user_agent, addresses)

    def _judged_addresses(self, url: str, allow_private_network: bool) -> list[str]:
        """Every address the name in ``url`` has now, all of them admitted by the guard.

        What ``guard.check(url)`` looked at cannot be reused: the protocol answers
        yes or no, and does not say which addresses it meant. So this looks the name
        up once more and judges each address as itself, which is how the egress proxy
        does it, and those literals are all that gets connected to.
        """
        host = _host(url)
        resolver: Resolver = (
            self.guard.resolver if isinstance(self.guard, UrlPolicy) else resolve_addresses
        )
        addresses = list(dict.fromkeys(resolver(host)))
        if not addresses:
            raise NavigationBlockedError(f"{host!r} resolved to no addresses")
        for address in addresses:
            if "%" in address:
                raise NavigationBlockedError(f"{host!r} resolved to the scoped address {address}")
            literal = f"[{address}]" if ":" in address else address
            self.guard.check(f"http://{literal}/", allow_private_network=allow_private_network)
        return addresses

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

    async def _take_slot(self, url: str, crawl_delay: float | None) -> None:
        """Reserve the host's next slot and wait for it, or refuse if it is too far.

        Reserved before sleeping and without a lock: nothing between reading the
        last start and writing the new one awaits, so two navigations to the same
        host queue one interval apart instead of both taking the same slot.

        A redirect hop comes through here like anything else. It is a request the site
        receives, and the site's ``Crawl-delay`` makes no exception for it.

        Another session's page can answer 429 during the sleep, so the sleep ends by
        looking again. A ``Retry-After`` reaching past the slot sends this back to the
        queue, one interval behind the slot it reserved, which is not given back: the
        ones behind it stay spaced, and all of them hold off when the site's time comes.
        ``max_wait_seconds`` limits the whole stay, however many times that happens.
        """
        host = _host(url)
        interval = max(self.settings.min_interval_seconds, crawl_delay or 0.0)
        hold_until = self.clock() + self.settings.max_wait_seconds
        while True:
            now = self.clock()
            earliest = self._not_before.get(host, now)
            if host in self._last_start:
                earliest = max(earliest, self._last_start[host] + interval)
            wait = max(0.0, earliest - now)
            if wait > 0 and now + wait > hold_until:  # the clock has moved on since hold_until
                raise RateLimitedError(
                    f"the next request to {host} may start in {wait:.1f} s; retry after that",
                    retry_after_seconds=wait,
                )
            slot = now + wait
            self._last_start[host] = slot
            if wait <= 0:
                return
            await self.sleep(wait)
            if self._not_before.get(host, slot) <= slot:
                return


def _unreachable(robots_url: str, reason: str) -> RobotsAnswer:
    return RobotsAnswer(RobotsStatus.UNREACHABLE, robots_url, reason=reason)


def _host(url: str) -> str:
    return (urlsplit(url).hostname or "").lower()


def _retry_after_seconds(value: str | None, now: float) -> float | None:
    """Read ``Retry-After`` in either form RFC 9110 section 10.2.3 allows."""
    if value is None:
        return None
    value = value.strip()
    # ``isdigit`` alone also accepts characters such as a superscript two, which
    # ``float`` then rejects.
    if value.isascii() and value.isdigit():
        return float(value)
    try:
        when = parsedate_to_datetime(value)
    except (TypeError, ValueError, IndexError):
        return None
    if when.tzinfo is None:
        when = when.replace(tzinfo=UTC)
    return max(0.0, when.timestamp() - now)
