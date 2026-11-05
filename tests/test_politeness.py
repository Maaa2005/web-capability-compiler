"""Reading ``robots.txt`` and pacing requests, without a browser.

The fetch, the clocks and the sleep are replaced so each answer a site can give
is produced on demand and no test waits in real time. The tests at the end run
the real fetch against loopback servers, which is where its deadline, its size
limit and the address it connects to can be seen to hold.
"""

from __future__ import annotations

import asyncio
import functools
import http.server
import ipaddress
import math
import socket
import ssl
import threading
import time
from collections.abc import Iterator
from datetime import UTC, datetime, timedelta
from email.utils import format_datetime
from http.client import IncompleteRead
from pathlib import Path
from urllib.error import URLError

import pytest

from wcc.driver.base import RateLimitedError, RobotsDisallowedError
from wcc.policy.politeness import (
    MAX_ROBOTS_REDIRECTS,
    FetchedResponse,
    PoliteAccess,
    PolitenessSettings,
    RobotsStatus,
    http_get,
)
from wcc.policy.robots import MAX_ROBOTS_BYTES
from wcc.policy.url_policy import UrlPolicy

PUBLIC = "93.184.216.34"


def resolver(host: str) -> list[str]:
    try:
        return [str(ipaddress.ip_address(host))]
    except ValueError:
        return {"private.test": ["10.0.0.1"]}.get(host, [PUBLIC])


class Clock:
    def __init__(self) -> None:
        self.now = 1_000.0
        self.slept: list[float] = []
        self.woke: list[float] = []

    def __call__(self) -> float:
        return self.now

    async def sleep(self, seconds: float) -> None:
        self.slept.append(round(seconds, 6))
        self.woke.append(round(self.now + seconds, 6))
        self.now += seconds


class ParkedClock(Clock):
    """A sleep that really suspends, as ``asyncio.sleep`` does, until the test moves time.

    ``Clock.sleep`` returns without letting anything else run, so nothing can happen
    during a wait and a test using it cannot see what a wait fails to notice.
    """

    def __init__(self) -> None:
        super().__init__()
        self.parked: list[tuple[float, asyncio.Future[None]]] = []

    async def sleep(self, seconds: float) -> None:
        future: asyncio.Future[None] = asyncio.get_running_loop().create_future()
        self.parked.append((self.now + seconds, future))
        await future

    async def advance(self, seconds: float) -> None:
        self.now += seconds
        due = [entry for entry in self.parked if entry[0] <= self.now + 1e-9]
        self.parked = [entry for entry in self.parked if entry not in due]
        for _, future in due:
            future.set_result(None)
        for _ in range(10):
            await asyncio.sleep(0)


class Site:
    """Answers by URL, and counts how often it was asked."""

    def __init__(self, answers: dict[str, FetchedResponse | Exception]) -> None:
        self.answers = answers
        self.calls: list[tuple[str, str]] = []

    def __call__(self, url: str, timeout: float, user_agent: str) -> FetchedResponse:
        del timeout
        self.calls.append((url, user_agent))
        answer = self.answers.get(url, FetchedResponse(404, None, b""))
        if isinstance(answer, Exception):
            raise answer
        return answer


def ok(body: str) -> FetchedResponse:
    return FetchedResponse(200, None, body.encode())


def access(site: Site, clock: Clock, **settings: float) -> PoliteAccess:
    return PoliteAccess(
        UrlPolicy(resolver=resolver),
        PolitenessSettings(user_agent="wcc/test", **settings),  # type: ignore[arg-type]
        fetch=site,
        clock=clock,
        wall_clock=lambda: clock.now,
        sleep=clock.sleep,
    )


async def admit(polite: PoliteAccess, url: str, previous: str | None = None) -> None:
    await polite.admit(url, allow_private_network=False, redirected_from=previous)


# -- robots.txt ------------------------------------------------------------


async def test_a_disallowed_path_is_refused_and_an_allowed_one_admitted() -> None:
    site = Site({"https://a.test/robots.txt": ok("User-agent: *\nDisallow: /private\n")})
    polite = access(site, Clock())
    await admit(polite, "https://a.test/public")
    with pytest.raises(RobotsDisallowedError) as refused:
        await admit(polite, "https://a.test/private/page")
    assert refused.value.code == "robots_disallowed"
    assert "/private/page" in refused.value.message
    assert site.calls == [("https://a.test/robots.txt", "wcc/test")]


async def test_a_missing_robots_txt_restricts_nothing() -> None:
    """RFC 9309 2.3.1.3: an unavailable file (4xx) means no rules."""
    for status in (404, 410, 400):
        site = Site({"https://a.test/robots.txt": FetchedResponse(status, None, b"")})
        polite = access(site, Clock())
        await admit(polite, "https://a.test/anything")
        answer = await polite.robots("https://a.test/", allow_private_network=False)
        assert answer.status is RobotsStatus.UNAVAILABLE


async def test_401_and_403_disallow_everything() -> None:
    """Stricter than RFC 9309, which counts them as unavailable; the reference MCP
    fetch server and Python's own reader refuse, and so does this."""
    for status in (401, 403):
        site = Site({"https://a.test/robots.txt": FetchedResponse(status, None, b"")})
        with pytest.raises(RobotsDisallowedError, match="refused to show it"):
            await admit(access(site, Clock()), "https://a.test/")


async def test_a_refusal_does_not_say_what_the_host_answered() -> None:
    """The text reaches the agent. Which status a host gave, or how a connection to it
    failed, is a way of learning about a host one was not allowed to read."""
    answers: list[FetchedResponse | Exception] = [
        FetchedResponse(401, None, b""),
        FetchedResponse(403, None, b""),
        FetchedResponse(429, None, b""),
        FetchedResponse(500, None, b""),
        FetchedResponse(503, None, b""),
        FetchedResponse(418, None, b""),
        FetchedResponse(304, None, b""),
        OSError("connecting to 10.1.2.3 was refused"),
    ]
    for answer in answers:
        site = Site({"https://a.test/robots.txt": answer})
        polite = access(site, Clock())
        verdict = await polite.robots("https://a.test/", allow_private_network=False)
        if isinstance(answer, FetchedResponse):
            assert str(answer.status) not in verdict.reason
        assert "10.1.2.3" not in verdict.reason


async def test_an_unreachable_robots_txt_disallows_everything() -> None:
    """RFC 9309 2.3.1.4: server errors, 429 and network failures mean complete
    disallow. The benchmark once read a network failure as permission."""
    failures: list[FetchedResponse | Exception] = [
        FetchedResponse(500, None, b""),
        FetchedResponse(503, None, b""),
        FetchedResponse(429, None, b""),
        URLError("connection refused"),
        TimeoutError("timed out"),
        OSError("network unreachable"),
    ]
    for failure in failures:
        site = Site({"https://a.test/robots.txt": failure})
        with pytest.raises(RobotsDisallowedError, match="unreachable robots.txt disallows"):
            await admit(access(site, Clock()), "https://a.test/")


async def test_a_fetch_that_never_answers_is_abandoned() -> None:
    def hangs(url: str, timeout: float, user_agent: str) -> FetchedResponse:
        del url, timeout, user_agent
        threading.Event().wait(3)
        return ok("")

    polite = PoliteAccess(
        UrlPolicy(resolver=resolver),
        PolitenessSettings(robots_timeout_seconds=0.05),
        fetch=hangs,
    )
    with pytest.raises(RobotsDisallowedError, match="no answer within 0.05 s"):
        await admit(polite, "https://a.test/")


async def test_redirects_are_followed_and_the_rules_apply_to_the_first_origin() -> None:
    site = Site(
        {
            "https://a.test/robots.txt": FetchedResponse(301, "https://b.test/rules", b""),
            "https://b.test/rules": FetchedResponse(302, "/final.txt", b""),
            "https://b.test/final.txt": ok("User-agent: *\nDisallow: /x\n"),
        }
    )
    polite = access(site, Clock())
    with pytest.raises(RobotsDisallowedError):
        await admit(polite, "https://a.test/x")
    await admit(polite, "https://a.test/y")
    assert [url for url, _ in site.calls] == [
        "https://a.test/robots.txt",
        "https://b.test/rules",
        "https://b.test/final.txt",
    ]


async def test_more_than_five_redirects_count_as_no_file() -> None:
    hops = {
        f"https://a.test/r{i}" if i else "https://a.test/robots.txt": FetchedResponse(
            301, f"https://a.test/r{i + 1}", b""
        )
        for i in range(MAX_ROBOTS_REDIRECTS + 1)
    }
    polite = access(Site(dict(hops)), Clock())
    await admit(polite, "https://a.test/page")
    answer = await polite.robots("https://a.test/", allow_private_network=False)
    assert answer.status is RobotsStatus.UNAVAILABLE


async def test_a_redirect_to_a_refused_address_is_not_followed() -> None:
    """Every hop passes the URL policy, so a site cannot point this fetch at an
    address the browser itself could not reach."""
    site = Site(
        {
            "https://a.test/robots.txt": FetchedResponse(
                302, "http://169.254.169.254/latest/meta-data", b""
            ),
            "https://b.test/robots.txt": FetchedResponse(302, "https://private.test/", b""),
        }
    )
    polite = access(site, Clock())
    for url in ("https://a.test/", "https://b.test/"):
        with pytest.raises(RobotsDisallowedError, match="refused by the URL policy"):
            await admit(polite, url)
    assert all("169.254" not in url and "private.test" not in url for url, _ in site.calls)


async def test_robots_txt_is_fetched_once_per_origin_and_again_after_a_day() -> None:
    site = Site({"https://a.test/robots.txt": ok("User-agent: *\nAllow: /\n")})
    clock = Clock()
    polite = access(site, clock, min_interval_seconds=0)
    await admit(polite, "https://a.test/1")
    await admit(polite, "https://a.test/2")
    await admit(polite, "http://a.test/3")  # another scheme is another origin
    assert len(site.calls) == 2
    clock.now += 24 * 60 * 60 + 1
    await admit(polite, "https://a.test/4")
    assert len(site.calls) == 3


async def test_concurrent_navigations_share_one_fetch() -> None:
    site = Site({"https://a.test/robots.txt": ok("")})
    polite = access(site, Clock(), min_interval_seconds=0)
    await asyncio.gather(*(admit(polite, f"https://a.test/{i}") for i in range(5)))
    assert len(site.calls) == 1


async def test_while_unreachable_the_last_good_copy_is_used() -> None:
    """RFC 9309 2.4 allows a cached copy past 24 hours while the file is unreachable."""
    site = Site({"https://a.test/robots.txt": ok("User-agent: *\nDisallow: /x\n")})
    clock = Clock()
    polite = access(site, clock, min_interval_seconds=0)
    await admit(polite, "https://a.test/y")
    site.answers["https://a.test/robots.txt"] = FetchedResponse(500, None, b"")
    clock.now += 24 * 60 * 60 + 1
    await admit(polite, "https://a.test/y")
    with pytest.raises(RobotsDisallowedError, match="does not allow"):
        await admit(polite, "https://a.test/x")


async def test_an_unreachable_answer_is_retried_after_five_minutes_not_on_every_call() -> None:
    site = Site({"https://a.test/robots.txt": FetchedResponse(503, None, b"")})
    clock = Clock()
    polite = access(site, clock)
    for _ in range(3):
        with pytest.raises(RobotsDisallowedError):
            await admit(polite, "https://a.test/")
    assert len(site.calls) == 1
    site.answers["https://a.test/robots.txt"] = ok("")
    clock.now += 5 * 60 + 1
    await admit(polite, "https://a.test/")
    assert len(site.calls) == 2


# -- pacing ----------------------------------------------------------------


async def test_requests_to_one_host_start_at_least_a_second_apart() -> None:
    clock = Clock()
    polite = access(Site({}), clock)
    await admit(polite, "https://a.test/1")  # one second after its robots.txt
    await admit(polite, "https://a.test/2")
    await admit(polite, "https://b.test/1")  # after b.test's robots.txt, not a.test's
    assert clock.slept == [1.0, 1.0, 1.0]
    clock.now += 5
    await admit(polite, "https://a.test/3")
    assert clock.slept == [1.0, 1.0, 1.0]


async def test_fetching_robots_txt_counts_as_a_request_to_the_host() -> None:
    clock = Clock()
    started: list[float] = []
    site = Site({"https://a.test/robots.txt": ok("")})

    def timed(url: str, timeout: float, user_agent: str) -> FetchedResponse:
        started.append(clock.now)
        return site(url, timeout, user_agent)

    polite = access(site, clock)
    polite.fetch = timed
    await admit(polite, "https://a.test/page")
    assert started == [1_000.0]
    assert clock.woke == [1_001.0]


async def test_a_recent_request_makes_the_robots_txt_fetch_wait_its_turn() -> None:
    """The file expired while the host is being visited: the fetch queues like a page."""
    clock = Clock()
    site = Site({"https://a.test/robots.txt": ok("")})
    polite = access(site, clock, robots_ttl_seconds=0.5)
    await admit(polite, "https://a.test/1")
    assert clock.woke == [1_001.0]
    await admit(polite, "https://a.test/2")
    assert clock.woke == [1_001.0, 1_002.0, 1_003.0]
    assert len(site.calls) == 2


async def test_a_host_holding_requests_back_is_not_asked_for_its_robots_txt() -> None:
    clock = Clock()
    site = Site({"https://a.test/robots.txt": ok("")})
    polite = access(site, clock)
    polite.observe("https://a.test/", 429, "120")
    with pytest.raises(RateLimitedError) as refused:
        await admit(polite, "https://a.test/")
    assert refused.value.retry_after_seconds == pytest.approx(120)
    assert site.calls == []
    # Refused for now, not remembered as an answer.
    clock.now += 120
    await admit(polite, "https://a.test/")
    assert len(site.calls) == 1


@pytest.mark.parametrize("status", [429, 503])
async def test_retry_after_on_robots_txt_holds_the_host_back(status: int) -> None:
    clock = Clock()
    site = Site(
        {"https://a.test/robots.txt": FetchedResponse(status, None, b"", retry_after="600")}
    )
    polite = access(site, clock)
    with pytest.raises(RobotsDisallowedError):
        await admit(polite, "https://a.test/")
    # The five minutes a failed fetch is remembered have passed; the site's ten have not.
    clock.now += 5 * 60 + 1
    with pytest.raises(RateLimitedError) as refused:
        await admit(polite, "https://a.test/")
    assert refused.value.retry_after_seconds == pytest.approx(5 * 60 - 1)
    assert len(site.calls) == 1


async def test_a_busy_robots_txt_without_retry_after_backs_off_by_the_default() -> None:
    clock = Clock()
    site = Site({"https://a.test/robots.txt": ok("")})
    polite = access(site, clock)
    await admit(polite, "https://a.test/")
    site.answers["https://a.test/robots.txt"] = FetchedResponse(429, None, b"")
    clock.now += 24 * 60 * 60 + 1
    # The last good copy stands in for the file, and the host is still held back.
    with pytest.raises(RateLimitedError) as refused:
        await admit(polite, "https://a.test/")
    assert refused.value.retry_after_seconds == pytest.approx(30)


async def test_a_same_host_redirect_that_needed_a_robots_fetch_is_paced() -> None:
    """http to https is one host and two origins: the hop's robots.txt fetch and the
    hop itself are two requests, and each takes its own slot."""
    clock = Clock()
    polite = access(Site({}), clock)
    await admit(polite, "http://a.test/")
    clock.now += 5
    clock.woke.clear()
    await admit(polite, "https://a.test/", previous="http://a.test/")
    assert clock.woke == [clock.now]
    assert clock.slept == [1.0, 1.0]


async def test_a_robots_redirect_to_a_host_just_visited_is_paced() -> None:
    clock = Clock()
    site = Site(
        {
            "https://a.test/robots.txt": FetchedResponse(301, "https://b.test/rules", b""),
            "https://b.test/rules": ok(""),
        }
    )
    polite = access(site, clock)
    await admit(polite, "https://b.test/page")
    clock.woke.clear()
    await admit(polite, "https://a.test/page")
    # b.test's rules wait out the second after its page. By then a second has also
    # passed since a.test's own first fetch, so a.test's page needs no wait of its own.
    assert clock.woke == [1_002.0]


async def test_one_deadline_covers_every_hop_of_the_fetch() -> None:
    """Five seconds per hop would be thirty for six hops, inside a five-second settle."""
    clock = Clock()
    hops = {
        f"https://a.test/r{i}" if i else "https://a.test/robots.txt": FetchedResponse(
            301, f"https://a.test/r{i + 1}", b""
        )
        for i in range(MAX_ROBOTS_REDIRECTS + 1)
    }
    site = Site(dict(hops))

    def slow(url: str, timeout: float, user_agent: str) -> FetchedResponse:
        clock.now += 2
        return site(url, timeout, user_agent)

    polite = access(site, clock, min_interval_seconds=0)
    polite.fetch = slow
    with pytest.raises(RobotsDisallowedError, match="no answer within 5 s"):
        await admit(polite, "https://a.test/")
    assert len(site.calls) == 3


async def test_concurrent_requests_queue_one_interval_apart() -> None:
    clock = Clock()
    polite = access(Site({}), clock, max_wait_seconds=10)
    await asyncio.gather(*(admit(polite, f"https://a.test/{i}") for i in range(3)))
    # The fetch of robots.txt went first, at 1000.
    assert sorted(clock.woke) == [1_001.0, 1_002.0, 1_003.0]


async def test_crawl_delay_widens_the_interval() -> None:
    site = Site({"https://a.test/robots.txt": ok("User-agent: wcc\nCrawl-delay: 1.5\n")})
    clock = Clock()
    polite = access(site, clock)
    await admit(polite, "https://a.test/1")
    await admit(polite, "https://a.test/2")
    assert clock.slept == [1.5, 1.5]


async def test_a_wait_longer_than_the_limit_is_refused_with_the_time_to_retry() -> None:
    site = Site({"https://a.test/robots.txt": ok("User-agent: *\nCrawl-delay: 10\n")})
    clock = Clock()
    polite = access(site, clock)
    # Crawl-delay applies from the fetch of the file that states it.
    with pytest.raises(RateLimitedError) as first:
        await admit(polite, "https://a.test/1")
    assert first.value.retry_after_seconds == pytest.approx(10)
    clock.now += 10
    await admit(polite, "https://a.test/1")
    clock.now += 3
    with pytest.raises(RateLimitedError) as refused:
        await admit(polite, "https://a.test/2")
    assert refused.value.code == "rate_limited"
    assert refused.value.retry_after_seconds == pytest.approx(7)
    assert clock.slept == []
    clock.now += 7
    await admit(polite, "https://a.test/2")


async def test_a_redirect_on_the_same_host_is_paced_like_any_request() -> None:
    """A hop is a request the site receives. The site cannot tell it from any other, and
    its Crawl-delay does not make an exception for it."""
    clock = Clock()
    polite = access(Site({}), clock)
    # Every robots.txt read and its slot spent, so only the hops are left to pace.
    for url in ("http://a.test/", "https://a.test/", "https://b.test/"):
        await admit(polite, url)
    clock.now += 5
    clock.slept.clear()

    await admit(polite, "http://a.test/")
    assert clock.slept == []
    await admit(polite, "https://a.test/", previous="http://a.test/")
    await admit(polite, "https://a.test/en/", previous="https://a.test/")
    assert clock.slept == [1.0, 1.0]
    await admit(polite, "https://b.test/", previous="https://a.test/en/")
    assert clock.slept == [1.0, 1.0]
    await admit(polite, "https://b.test/x", previous="https://c.test/")
    assert clock.slept == [1.0, 1.0, 1.0]


async def test_a_redirect_on_the_same_host_waits_out_the_crawl_delay() -> None:
    site = Site({"https://a.test/robots.txt": ok("User-agent: wcc\nCrawl-delay: 1.5\n")})
    clock = Clock()
    polite = access(site, clock)
    await admit(polite, "https://a.test/1")
    assert clock.slept == [1.5]
    await admit(polite, "https://a.test/2", previous="https://a.test/1")
    assert clock.slept == [1.5, 1.5]


async def test_a_redirect_too_soon_is_refused_naming_where_it_was_going() -> None:
    """Asking for the first URL again would be redirected again and refused again, every
    time, however long the agent waited. The hop's own address is what can be retried."""
    site = Site({"https://a.test/robots.txt": ok("User-agent: *\nCrawl-delay: 10\n")})
    clock = Clock()
    polite = access(site, clock)
    with pytest.raises(RateLimitedError):
        await admit(polite, "https://a.test/1")
    clock.now += 10
    await admit(polite, "https://a.test/1")

    with pytest.raises(RateLimitedError) as refused:
        await admit(polite, "https://a.test/2", previous="https://a.test/1")
    assert refused.value.retry_after_seconds == pytest.approx(10)
    assert "https://a.test/2" in refused.value.message
    assert clock.slept == []

    clock.now += 10
    await admit(polite, "https://a.test/2")


async def test_a_robots_redirect_on_the_same_host_is_paced() -> None:
    clock = Clock()
    started: list[float] = []
    site = Site(
        {
            "https://a.test/robots.txt": FetchedResponse(301, "/moved.txt", b""),
            "https://a.test/moved.txt": ok(""),
        }
    )

    def timed(url: str, timeout: float, user_agent: str) -> FetchedResponse:
        started.append(clock.now)
        return site(url, timeout, user_agent)

    polite = access(site, clock)
    polite.fetch = timed
    await polite.robots("https://a.test/", allow_private_network=False)
    assert started == [1_000.0, 1_001.0]


async def test_the_fetchs_deadline_is_not_spent_on_this_runtimes_own_pacing() -> None:
    """RFC 9309 2.3.1.2 asks for five redirects to be followed. On one host, at one per
    second, those are five seconds of waiting, which would use up a five-second deadline
    before the last answer and read a patient site as an unreachable one."""
    clock = Clock()
    hops: dict[str, FetchedResponse | Exception] = {
        f"https://a.test/r{i}" if i else "https://a.test/robots.txt": FetchedResponse(
            301, f"https://a.test/r{i + 1}", b""
        )
        for i in range(MAX_ROBOTS_REDIRECTS)
    }
    hops[f"https://a.test/r{MAX_ROBOTS_REDIRECTS}"] = ok("User-agent: *\nDisallow: /x\n")
    polite = access(Site(hops), clock)
    answer = await polite.robots("https://a.test/", allow_private_network=False)
    assert answer.status is RobotsStatus.PARSED
    assert clock.slept == [1.0] * MAX_ROBOTS_REDIRECTS


async def test_a_429_holds_the_host_back_for_its_retry_after() -> None:
    clock = Clock()
    polite = access(Site({}), clock)
    await admit(polite, "https://a.test/")
    polite.observe("https://a.test/", 429, "30")
    with pytest.raises(RateLimitedError) as refused:
        await admit(polite, "https://a.test/next")
    assert refused.value.retry_after_seconds == pytest.approx(30)
    await admit(polite, "https://b.test/")
    clock.now += 30
    await admit(polite, "https://a.test/next")


async def test_retry_after_may_be_a_date() -> None:
    clock = Clock()
    clock.now = datetime(2026, 11, 4, 12, 0, tzinfo=UTC).timestamp()
    polite = access(Site({}), clock)
    later = format_datetime(datetime(2026, 11, 4, 12, 2, tzinfo=UTC), usegmt=True)
    polite.observe("https://a.test/", 503, later)
    with pytest.raises(RateLimitedError) as refused:
        await admit(polite, "https://a.test/")
    assert refused.value.retry_after_seconds == pytest.approx(120)


@pytest.mark.parametrize("days", [1, 2, 30])
async def test_a_retry_after_longer_than_a_day_is_kept_to_the_second(days: int) -> None:
    clock = Clock()
    polite = access(Site({}), clock)
    seconds = days * 24 * 60 * 60 + 60
    polite.observe("https://a.test/", 429, str(seconds))
    with pytest.raises(RateLimitedError) as refused:
        await admit(polite, "https://a.test/")
    assert refused.value.retry_after_seconds == pytest.approx(seconds)

    clock.now += seconds - 60
    with pytest.raises(RateLimitedError) as refused:
        await admit(polite, "https://a.test/")
    assert refused.value.retry_after_seconds == pytest.approx(60)

    clock.now += 60
    await admit(polite, "https://a.test/")


async def test_a_retry_after_date_days_away_is_kept_too() -> None:
    clock = Clock()
    clock.now = datetime(2026, 11, 4, 12, 0, tzinfo=UTC).timestamp()
    polite = access(Site({}), clock)
    later = format_datetime(datetime(2026, 11, 7, 12, 0, tzinfo=UTC), usegmt=True)
    polite.observe("https://a.test/", 503, later)
    clock.now += 2 * 24 * 60 * 60
    with pytest.raises(RateLimitedError) as refused:
        await admit(polite, "https://a.test/")
    assert refused.value.retry_after_seconds == pytest.approx(24 * 60 * 60)


@pytest.mark.parametrize("value", ["9" * 400, "9" * 40, "Fri, 31 Dec 9999 23:59:59 GMT"])
async def test_an_absurd_retry_after_refuses_with_a_number_that_can_be_shown(value: str) -> None:
    """``float`` reads four hundred digits as infinity, which would then be the number
    handed to the agent. The refusal holds; its text stays readable."""
    clock = Clock()
    polite = access(Site({}), clock)
    polite.observe("https://a.test/", 429, value)
    clock.now += 365 * 24 * 60 * 60
    with pytest.raises(RateLimitedError) as refused:
        await admit(polite, "https://a.test/")
    assert math.isfinite(refused.value.retry_after_seconds)
    assert len(refused.value.message) < 200


async def _with_robots_read(clock: ParkedClock) -> PoliteAccess:
    """A polite access that has read a.test's rules and just sent a page there."""
    polite = access(Site({}), clock)
    polite.sleep = functools.partial(Clock.sleep, clock)
    await admit(polite, "https://a.test/first")
    polite.sleep = clock.sleep
    return polite


async def test_a_retry_after_heard_during_the_wait_stops_the_request() -> None:
    """A request parked for its slot when another session's page answers 429."""
    clock = ParkedClock()
    polite = await _with_robots_read(clock)
    waiting = asyncio.create_task(admit(polite, "https://a.test/next"))
    await clock.advance(0)
    assert len(clock.parked) == 1

    polite.observe("https://a.test/other", 429, "120")
    await clock.advance(1)
    with pytest.raises(RateLimitedError) as refused:
        await waiting
    assert refused.value.retry_after_seconds == pytest.approx(119)


async def test_everyone_in_the_queue_hears_the_retry_after() -> None:
    clock = ParkedClock()
    polite = await _with_robots_read(clock)
    queue = [asyncio.create_task(admit(polite, f"https://a.test/{i}")) for i in range(2)]
    await clock.advance(0)
    assert len(clock.parked) == 2

    polite.observe("https://a.test/other", 503, "120")
    await clock.advance(2)
    answers = await asyncio.gather(*queue, return_exceptions=True)
    assert all(isinstance(answer, RateLimitedError) for answer in answers)


async def test_a_short_retry_after_heard_during_the_wait_is_waited_out() -> None:
    clock = ParkedClock()
    polite = await _with_robots_read(clock)
    start = clock.now
    waiting = asyncio.create_task(admit(polite, "https://a.test/next"))
    await clock.advance(0)
    await clock.advance(0.5)

    polite.observe("https://a.test/other", 429, "1")
    await clock.advance(0.5)  # the slot it was waiting for has come, the site's time has not
    assert not waiting.done()
    await clock.advance(0.5)  # the site's time has come; one interval after that slot has not
    assert not waiting.done()
    await clock.advance(0.5)
    assert waiting.done()
    await waiting
    assert clock.now == pytest.approx(start + 2)


async def test_the_limit_on_holding_a_navigation_covers_the_whole_wait() -> None:
    """Waited out, this Retry-After would make 2.9 s of a limit of two."""
    clock = ParkedClock()
    polite = await _with_robots_read(clock)
    waiting = asyncio.create_task(admit(polite, "https://a.test/next"))
    await clock.advance(0)
    await clock.advance(0.9)

    polite.observe("https://a.test/other", 429, "2")
    await clock.advance(0.1)
    with pytest.raises(RateLimitedError) as refused:
        await waiting
    assert refused.value.retry_after_seconds == pytest.approx(1.9)


async def test_a_retry_after_over_before_the_slot_comes_changes_nothing() -> None:
    clock = ParkedClock()
    polite = await _with_robots_read(clock)
    waiting = asyncio.create_task(admit(polite, "https://a.test/next"))
    await clock.advance(0)
    await clock.advance(0.1)

    polite.observe("https://a.test/other", 429, "0")
    await clock.advance(0.9)
    assert waiting.done()
    await waiting


async def test_another_hosts_retry_after_does_not_stop_the_wait() -> None:
    clock = ParkedClock()
    polite = await _with_robots_read(clock)
    waiting = asyncio.create_task(admit(polite, "https://a.test/next"))
    await clock.advance(0)
    polite.observe("https://b.test/", 429, "120")
    await clock.advance(1)
    assert waiting.done()
    await waiting


async def test_a_retry_after_during_a_real_wait_on_the_real_clock() -> None:
    polite = PoliteAccess(
        UrlPolicy(resolver=resolver),
        PolitenessSettings(user_agent="wcc/test", min_interval_seconds=0.2),
        fetch=Site({}),
    )
    await admit(polite, "https://a.test/first")
    waiting = asyncio.create_task(admit(polite, "https://a.test/next"))
    await asyncio.sleep(0.05)
    assert not waiting.done()
    polite.observe("https://a.test/other", 429, "120")
    with pytest.raises(RateLimitedError) as refused:
        await waiting
    assert 115 < refused.value.retry_after_seconds <= 120


async def test_a_429_without_retry_after_backs_off_by_the_default() -> None:
    clock = Clock()
    polite = access(Site({}), clock)
    polite.observe("https://a.test/", 429, None)
    polite.observe("https://b.test/", 429, "not a time")
    polite.observe("https://c.test/", 200, "600")
    for host in ("a.test", "b.test"):
        with pytest.raises(RateLimitedError) as refused:
            await admit(polite, f"https://{host}/")
        assert refused.value.retry_after_seconds == pytest.approx(30)
    await admit(polite, "https://c.test/")


async def test_a_short_backoff_is_waited_out_rather_than_refused() -> None:
    clock = Clock()
    polite = access(Site({}), clock)
    polite.observe("https://a.test/", 503, "1")
    await admit(polite, "https://a.test/")
    # Waited out by the robots.txt fetch, then the interval after that fetch.
    assert clock.slept == [1.0, 1.0]


@pytest.mark.parametrize("value", ["\u00b2", "\u0661\u0662", "\uff11\uff12", " \u00b23 "])
async def test_a_retry_after_of_digits_that_are_not_ascii_is_not_a_number(value: str) -> None:
    """``str.isdigit`` accepts these and ``float`` rejects some of them, and the error
    would surface inside Playwright's event handler, with no refusal to show for it."""
    clock = Clock()
    polite = access(Site({}), clock)
    polite.observe("https://a.test/", 429, value)
    with pytest.raises(RateLimitedError) as refused:
        await admit(polite, "https://a.test/")
    assert refused.value.retry_after_seconds == pytest.approx(30)


# -- the real fetch ----------------------------------------------------------


class _Handler(http.server.BaseHTTPRequestHandler):
    seen_agents: list[str] = []

    def do_GET(self) -> None:  # noqa: N802 - the standard library's name
        type(self).seen_agents.append(self.headers.get("User-Agent", ""))
        if self.path == "/robots.txt":
            self.send_response(301)
            self.send_header("Location", "/big.txt")
            self.end_headers()
            return
        body = b"User-agent: *\n" + b"#" * (MAX_ROBOTS_BYTES + 10_000)
        self.send_response(200)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002
        del format, args


@pytest.fixture
def loopback() -> Iterator[str]:
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), functools.partial(_Handler))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()


def test_the_real_fetch_does_not_follow_redirects_and_reads_at_most_500_kib(
    loopback: str,
) -> None:
    redirected = http_get(f"{loopback}/robots.txt", 5, "wcc/test", ["127.0.0.1"])
    assert (redirected.status, redirected.location) == (301, "/big.txt")
    body = http_get(f"{loopback}/big.txt", 5, "wcc/test", ["127.0.0.1"])
    assert body.status == 200
    assert len(body.body) == MAX_ROBOTS_BYTES
    assert _Handler.seen_agents[-2:] == ["wcc/test", "wcc/test"]


class _Scripted(http.server.BaseHTTPRequestHandler):
    """Answers from what a test put in ``answer``, and notes who asked."""

    answer: bytes = b""
    asked: list[tuple[str, str]] = []

    def do_GET(self) -> None:  # noqa: N802 - the standard library's name
        type(self).asked.append((self.path, self.headers.get("Host", "")))
        self.wfile.write(type(self).answer)
        self.close_connection = True

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002
        del format, args


@pytest.fixture
def scripted() -> Iterator[int]:
    _Scripted.answer = b"HTTP/1.1 200 OK\r\nContent-Length: 0\r\n\r\n"
    _Scripted.asked = []
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), functools.partial(_Scripted))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server.server_address[1]
    finally:
        server.shutdown()
        server.server_close()


def test_the_fetch_reports_retry_after(scripted: int) -> None:
    _Scripted.answer = b"HTTP/1.1 503 Busy\r\nRetry-After: 7\r\nContent-Length: 0\r\n\r\n"
    answer = http_get(f"http://127.0.0.1:{scripted}/robots.txt", 5, "wcc/test", ["127.0.0.1"])
    assert (answer.status, answer.retry_after, answer.body) == (503, "7", b"")


def test_the_fetch_caps_a_chunked_body_too(scripted: int) -> None:
    chunk = b"10000\r\n" + b"#" * 0x10000 + b"\r\n"
    _Scripted.answer = (
        b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n" + chunk * 12 + b"0\r\n\r\n"
    )
    answer = http_get(f"http://127.0.0.1:{scripted}/robots.txt", 5, "wcc/test", ["127.0.0.1"])
    assert len(answer.body) == MAX_ROBOTS_BYTES


_CUT_OFF = pytest.mark.parametrize(
    "answer",
    [
        pytest.param(
            b"HTTP/1.1 200 OK\r\nContent-Length: 100\r\n\r\nUser-agent: *\n",
            id="short-of-its-content-length",
        ),
        pytest.param(b"HTTP/1.1 200 OK\r\nContent-Length: 100\r\n\r\n", id="no-body-at-all"),
        pytest.param(
            b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n40\r\nUser-agent: *\n",
            id="inside-a-chunk",
        ),
        pytest.param(
            b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\ne\r\nUser-agent: *\n\r\n",
            id="between-chunks",
        ),
    ],
)


@_CUT_OFF
def test_the_fetch_raises_on_a_body_cut_off_before_its_end(scripted: int, answer: bytes) -> None:
    _Scripted.answer = answer
    with pytest.raises(IncompleteRead):
        http_get(f"http://127.0.0.1:{scripted}/robots.txt", 5, "wcc/test", ["127.0.0.1"])


@_CUT_OFF
async def test_a_robots_txt_cut_off_before_its_end_is_unreachable(
    scripted: int, answer: bytes
) -> None:
    """The rest of the file may be the rule that refuses this page. RFC 9309 2.3.1.4,
    and remembered as a failure rather than cached for a day as the site's rules."""
    _Scripted.answer = answer
    polite = PoliteAccess(UrlPolicy(), PolitenessSettings(min_interval_seconds=0.0))
    url = f"http://127.0.0.1:{scripted}/page"
    with pytest.raises(RobotsDisallowedError, match="could not be read"):
        await polite.admit(url, allow_private_network=True)
    answered = await polite.robots(url, allow_private_network=True)
    assert answered.status is RobotsStatus.UNREACHABLE
    assert len(_Scripted.asked) == 1

    # The next read, five minutes on, starts again rather than trusting anything.
    complete = b"User-agent: *\nDisallow: /page\n"
    head = b"HTTP/1.1 200 OK\r\nContent-Length: %d\r\n\r\n" % len(complete)
    _Scripted.answer = head + complete
    polite.clock = lambda: time.monotonic() + 5 * 60 + 1
    with pytest.raises(RobotsDisallowedError, match="does not allow"):
        await polite.admit(url, allow_private_network=True)


async def test_a_cut_off_file_leaves_the_last_good_copy_in_charge(scripted: int) -> None:
    complete = b"User-agent: *\nDisallow: /x\n"
    head = b"HTTP/1.1 200 OK\r\nContent-Length: %d\r\n\r\n" % len(complete)
    _Scripted.answer = head + complete
    polite = PoliteAccess(UrlPolicy(), PolitenessSettings(min_interval_seconds=0.0))
    url = f"http://127.0.0.1:{scripted}/"
    await polite.robots(url, allow_private_network=True)

    _Scripted.answer = head + complete[:14]
    polite.clock = lambda: time.monotonic() + 24 * 60 * 60 + 1
    answer = await polite.robots(url, allow_private_network=True)
    assert len(_Scripted.asked) == 2
    assert answer.status is RobotsStatus.PARSED
    assert answer.file is not None and not answer.file.allows("/x", "wcc")


@pytest.mark.parametrize(
    ("answer", "body"),
    [
        pytest.param(b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nok", b"ok", id="as-declared"),
        pytest.param(b"HTTP/1.1 200 OK\r\nContent-Length: 0\r\n\r\n", b"", id="declared-empty"),
        # Nothing was promised, so the end of the connection is the end of the body.
        pytest.param(b"HTTP/1.1 200 OK\r\n\r\nok", b"ok", id="no-length-at-all"),
        pytest.param(
            b"HTTP/1.1 200 OK\r\nTransfer-Encoding: chunked\r\n\r\n2\r\nok\r\n0\r\n\r\n",
            b"ok",
            id="chunked",
        ),
    ],
)
def test_a_body_that_arrived_whole_is_read(scripted: int, answer: bytes, body: bytes) -> None:
    _Scripted.answer = answer
    fetched = http_get(f"http://127.0.0.1:{scripted}/robots.txt", 5, "wcc/test", ["127.0.0.1"])
    assert (fetched.status, fetched.body) == (200, body)


# -- the address that is connected to is the address that was judged ---------


def _record_lookups(monkeypatch: pytest.MonkeyPatch) -> list[str]:
    looked_up: list[str] = []
    real = socket.getaddrinfo

    def spy(host: str, *args: object, **kwargs: object) -> object:
        looked_up.append(host)
        return real(host, *args, **kwargs)  # type: ignore[arg-type]

    monkeypatch.setattr(socket, "getaddrinfo", spy)
    return looked_up


def test_the_fetch_connects_to_the_given_address_and_never_looks_the_name_up(
    scripted: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    looked_up = _record_lookups(monkeypatch)
    url = f"http://rebind.invalid:{scripted}/robots.txt"
    answer = http_get(url, 5, "wcc/test", ["127.0.0.1"])
    assert answer.status == 200
    assert _Scripted.asked == [("/robots.txt", f"rebind.invalid:{scripted}")]
    assert "rebind.invalid" not in looked_up


async def test_a_name_that_changes_after_the_check_is_never_connected_to(scripted: int) -> None:
    """The name is public when the URL is checked and internal a moment later, which is
    the DNS-rebinding window. What it answers last is what gets judged and used."""
    answers = iter([[PUBLIC], ["127.0.0.1"]])

    def rebinding(host: str) -> list[str]:
        return next(answers) if host == "rebind.test" else [host]

    polite = PoliteAccess(UrlPolicy(resolver=rebinding), PolitenessSettings())
    answer = await polite.robots(f"http://rebind.test:{scripted}/x", allow_private_network=False)
    assert answer.status is RobotsStatus.UNREACHABLE
    assert "refused by the URL policy" in answer.reason
    assert _Scripted.asked == []


async def test_the_runtime_fetch_uses_the_policys_answer_not_a_new_lookup(
    scripted: int, monkeypatch: pytest.MonkeyPatch
) -> None:
    _Scripted.answer = b"HTTP/1.1 200 OK\r\nContent-Length: 26\r\n\r\nUser-agent: *\nDisallow: /x"
    looked_up = _record_lookups(monkeypatch)

    def answers(host: str) -> list[str]:
        return ["127.0.0.1"] if host == "named.invalid" else [host]

    polite = PoliteAccess(UrlPolicy(resolver=answers), PolitenessSettings(user_agent="wcc/test"))
    answer = await polite.robots(f"http://named.invalid:{scripted}/", allow_private_network=True)
    assert answer.status is RobotsStatus.PARSED
    assert answer.file is not None and not answer.file.allows("/x", "wcc")
    assert _Scripted.asked == [("/robots.txt", f"named.invalid:{scripted}")]
    assert "named.invalid" not in looked_up


async def test_a_scoped_or_absent_address_is_refused() -> None:
    for addresses in (["fe80::1%12"], []):
        calls = iter([[PUBLIC], addresses])

        def answering(host: str, calls: Iterator[list[str]] = calls) -> list[str]:
            del host
            return next(calls)

        polite = PoliteAccess(UrlPolicy(resolver=answering), PolitenessSettings())
        answer = await polite.robots("http://a.test/", allow_private_network=True)
        assert answer.status is RobotsStatus.UNREACHABLE
        assert "refused by the URL policy" in answer.reason


# -- a site cannot hold the fetch open ---------------------------------------


def _dripping_server(head: bytes) -> tuple[socket.socket, threading.Event]:
    """Sends ``head``, then one byte every 50 ms for as long as anyone listens."""
    listener = socket.create_server(("127.0.0.1", 0))
    stop = threading.Event()

    def drip(connection: socket.socket) -> None:
        with connection:
            try:
                connection.recv(4096)
                connection.sendall(head)
                while not stop.wait(0.05):
                    connection.sendall(b"#")
            except OSError:
                pass

    def accept() -> None:
        while not stop.is_set():
            try:
                connection, _ = listener.accept()
            except OSError:
                return
            threading.Thread(target=drip, args=(connection,), daemon=True).start()

    threading.Thread(target=accept, daemon=True).start()
    return listener, stop


@pytest.mark.parametrize(
    "head",
    [
        pytest.param(b"HTTP/1.1 200 OK\r\nContent-Length: 400000\r\n\r\n", id="in-the-body"),
        pytest.param(b"HTTP/1.1 200 OK\r\nX-Slow: ", id="in-the-headers"),
        pytest.param(b"", id="in-the-status-line"),
    ],
)
def test_a_body_sent_a_byte_at_a_time_cannot_outlast_the_deadline(head: bytes) -> None:
    listener, stop = _dripping_server(head)
    port = listener.getsockname()[1]
    started = time.monotonic()
    try:
        with pytest.raises(TimeoutError):
            http_get(f"http://127.0.0.1:{port}/robots.txt", 0.4, "wcc/test", ["127.0.0.1"])
        assert time.monotonic() - started < 2
    finally:
        stop.set()
        listener.close()


def _robots_workers() -> list[threading.Thread]:
    return [thread for thread in threading.enumerate() if thread.name == "wcc-robots"]


def test_a_slow_robots_txt_stops_its_worker_and_lets_the_process_exit() -> None:
    listener, stop = _dripping_server(b"HTTP/1.1 200 OK\r\nContent-Length: 400000\r\n\r\n")
    port = listener.getsockname()[1]
    polite = PoliteAccess(UrlPolicy(), PolitenessSettings(robots_timeout_seconds=0.4))
    earlier = _robots_workers()
    started = time.monotonic()
    try:
        answer = asyncio.run(polite.robots(f"http://127.0.0.1:{port}/", allow_private_network=True))
        assert time.monotonic() - started < 3
        assert answer.status is RobotsStatus.UNREACHABLE
        assert "no answer within 0.4 s" in answer.reason
        # The thread itself stopped reading: it has not merely been stopped being waited for.
        workers = [worker for worker in _robots_workers() if worker not in earlier]
        for worker in workers:
            worker.join(timeout=2)
        assert not any(worker.is_alive() for worker in workers)
    finally:
        stop.set()
        listener.close()


def test_a_worker_nothing_can_interrupt_does_not_hold_up_the_loops_exit() -> None:
    """``getaddrinfo`` has no timeout, so this is the shape a stuck lookup would take."""
    release = threading.Event()

    def stuck(url: str, timeout: float, user_agent: str) -> FetchedResponse:
        del url, timeout, user_agent
        release.wait(30)
        return ok("")

    polite = PoliteAccess(
        UrlPolicy(resolver=resolver), PolitenessSettings(robots_timeout_seconds=0.05), fetch=stuck
    )
    started = time.monotonic()
    try:
        answer = asyncio.run(polite.robots("https://a.test/", allow_private_network=False))
        assert time.monotonic() - started < 5
        assert answer.status is RobotsStatus.UNREACHABLE
    finally:
        release.set()


# -- TLS to a pinned address -------------------------------------------------


def _certificate_for(name: str, directory: Path) -> tuple[Path, Path]:
    x509 = pytest.importorskip("cryptography.x509")
    hashes = pytest.importorskip("cryptography.hazmat.primitives.hashes")
    ec = pytest.importorskip("cryptography.hazmat.primitives.asymmetric.ec")
    serialization = pytest.importorskip("cryptography.hazmat.primitives.serialization")

    key = ec.generate_private_key(ec.SECP256R1())
    subject = x509.Name([x509.NameAttribute(x509.NameOID.COMMON_NAME, name)])
    now = datetime.now(UTC)
    certificate = (
        x509.CertificateBuilder()
        .subject_name(subject)
        .issuer_name(subject)
        .public_key(key.public_key())
        .serial_number(x509.random_serial_number())
        .not_valid_before(now - timedelta(days=1))
        .not_valid_after(now + timedelta(days=1))
        .add_extension(x509.SubjectAlternativeName([x509.DNSName(name)]), critical=False)
        .add_extension(x509.BasicConstraints(ca=True, path_length=None), critical=True)
        .sign(key, hashes.SHA256())
    )
    cert_path = directory / "cert.pem"
    key_path = directory / "key.pem"
    cert_path.write_bytes(certificate.public_bytes(serialization.Encoding.PEM))
    key_path.write_bytes(
        key.private_bytes(
            serialization.Encoding.PEM,
            serialization.PrivateFormat.PKCS8,
            serialization.NoEncryption(),
        )
    )
    return cert_path, key_path


@pytest.fixture
def tls_server(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Iterator[int]:
    """A TLS server for ``pinned.invalid``, whose certificate the fetch is made to trust."""
    cert_path, key_path = _certificate_for("pinned.invalid", tmp_path)
    server_context = ssl.SSLContext(ssl.PROTOCOL_TLS_SERVER)
    server_context.load_cert_chain(cert_path, key_path)
    trusting = ssl.create_default_context(cafile=str(cert_path))
    monkeypatch.setattr(ssl, "create_default_context", lambda: trusting)

    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), functools.partial(_Scripted))
    server.socket = server_context.wrap_socket(server.socket, server_side=True)
    # A failed handshake is the test passing; it should not be printed as an error.
    server.handle_error = lambda request, client_address: None  # type: ignore[method-assign]
    _Scripted.asked = []
    _Scripted.answer = b"HTTP/1.1 200 OK\r\nContent-Length: 2\r\n\r\nok"
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield server.server_address[1]
    finally:
        server.shutdown()
        server.server_close()


def test_tls_to_a_pinned_address_checks_the_certificate_against_the_name(
    tls_server: int,
) -> None:
    url = f"https://pinned.invalid:{tls_server}/robots.txt"
    answer = http_get(url, 5, "wcc/test", ["127.0.0.1"])
    assert (answer.status, answer.body) == (200, b"ok")
    assert _Scripted.asked == [("/robots.txt", f"pinned.invalid:{tls_server}")]


def test_tls_refuses_a_certificate_for_another_name(tls_server: int) -> None:
    url = f"https://other.invalid:{tls_server}/robots.txt"
    with pytest.raises(ssl.SSLCertVerificationError):
        http_get(url, 5, "wcc/test", ["127.0.0.1"])
    assert _Scripted.asked == []
