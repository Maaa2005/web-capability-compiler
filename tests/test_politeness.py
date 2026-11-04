"""Reading ``robots.txt`` and pacing requests, without a browser.

The fetch, the clocks and the sleep are replaced so each answer a site can give
is produced on demand and no test waits in real time. One test at the end runs
the real standard-library fetch against a loopback server.
"""

from __future__ import annotations

import asyncio
import functools
import http.server
import ipaddress
import threading
from collections.abc import Iterator
from datetime import UTC, datetime
from email.utils import format_datetime
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
        with pytest.raises(RobotsDisallowedError, match=f"answered {status}"):
            await admit(access(site, Clock()), "https://a.test/")


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
    await admit(polite, "https://a.test/1")
    await admit(polite, "https://a.test/2")
    await admit(polite, "https://b.test/1")
    clock.now += 5
    await admit(polite, "https://a.test/3")
    assert clock.slept == [1.0]


async def test_concurrent_requests_queue_one_interval_apart() -> None:
    clock = Clock()
    polite = access(Site({}), clock, max_wait_seconds=10)
    await asyncio.gather(*(admit(polite, f"https://a.test/{i}") for i in range(3)))
    assert sorted(clock.woke) == [1_001.0, 1_002.0]


async def test_crawl_delay_widens_the_interval() -> None:
    site = Site({"https://a.test/robots.txt": ok("User-agent: wcc\nCrawl-delay: 1.5\n")})
    clock = Clock()
    polite = access(site, clock)
    await admit(polite, "https://a.test/1")
    await admit(polite, "https://a.test/2")
    assert clock.slept == [1.5]


async def test_a_wait_longer_than_the_limit_is_refused_with_the_time_to_retry() -> None:
    site = Site({"https://a.test/robots.txt": ok("User-agent: *\nCrawl-delay: 10\n")})
    clock = Clock()
    polite = access(site, clock)
    await admit(polite, "https://a.test/1")
    clock.now += 3
    with pytest.raises(RateLimitedError) as refused:
        await admit(polite, "https://a.test/2")
    assert refused.value.code == "rate_limited"
    assert refused.value.retry_after_seconds == pytest.approx(7)
    assert clock.slept == []
    clock.now += 7
    await admit(polite, "https://a.test/2")


async def test_a_redirect_on_the_same_host_is_not_paced_again() -> None:
    clock = Clock()
    polite = access(Site({}), clock)
    await admit(polite, "http://a.test/")
    await admit(polite, "https://a.test/", previous="http://a.test/")
    await admit(polite, "https://a.test/en/", previous="https://a.test/")
    assert clock.slept == []
    await admit(polite, "https://b.test/", previous="https://a.test/en/")
    await admit(polite, "https://b.test/x", previous="https://c.test/")
    assert clock.slept == [1.0]


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
    assert clock.slept == [1.0]


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
    redirected = http_get(f"{loopback}/robots.txt", 5, "wcc/test")
    assert (redirected.status, redirected.location) == (301, "/big.txt")
    body = http_get(f"{loopback}/big.txt", 5, "wcc/test")
    assert body.status == 200
    assert len(body.body) == MAX_ROBOTS_BYTES
    assert _Handler.seen_agents[-2:] == ["wcc/test", "wcc/test"]
