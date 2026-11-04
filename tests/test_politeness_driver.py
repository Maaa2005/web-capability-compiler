"""``robots.txt`` and pacing, enforced by the real driver in a real browser.

A loopback site with its own ``robots.txt`` stands in for the web. Each test
checks the refusal an agent would actually receive: a typed error from
``open_session``, or a failed step with the refusal's code from an action.
"""

from __future__ import annotations

import functools
import http.server
import threading
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Any, ClassVar

import pytest

from wcc.config import USER_AGENT_PRODUCT
from wcc.driver.base import (
    DocumentAdmission,
    DriverSettings,
    NavigationBlockedError,
    RateLimitedError,
    RobotsDisallowedError,
)
from wcc.driver.playwright_driver import PlaywrightDriver
from wcc.mcp.server import build_server, build_service
from wcc.models.action import PlannedAction
from wcc.models.common import ActionType
from wcc.models.session import OpenSessionRequest
from wcc.policy.politeness import PoliteAccess, PolitenessSettings
from wcc.policy.url_policy import UrlPolicy
from wcc.store.store import Store

pytestmark = pytest.mark.asyncio

_PAGE = "<!doctype html><html><head><title>{title}</title></head><body>{body}</body></html>"


class _Site(http.server.BaseHTTPRequestHandler):
    robots: ClassVar[str] = "User-agent: *\nDisallow: /private\n"
    agents: ClassVar[list[tuple[str, str]]] = []

    def do_GET(self) -> None:  # noqa: N802 - the standard library's name
        type(self).agents.append((self.path, self.headers.get("User-Agent", "")))
        if self.path == "/robots.txt":
            self._send(200, type(self).robots, "text/plain")
        elif self.path == "/busy.html":
            self._send(429, _PAGE.format(title="Busy", body="slow down"), retry_after="120")
        elif self.path == "/index.html":
            links = '<a href="/private/page.html">private</a> <a href="/public.html">public</a>'
            self._send(200, _PAGE.format(title="Index", body=links))
        elif self.path in ("/public.html", "/private/page.html"):
            self._send(200, _PAGE.format(title=self.path, body="<p>a page</p>"))
        else:
            self._send(404, _PAGE.format(title="Missing", body=""))

    def _send(
        self, status: int, body: str, kind: str = "text/html", retry_after: str | None = None
    ) -> None:
        payload = body.encode()
        self.send_response(status)
        self.send_header("Content-Type", f"{kind}; charset=utf-8")
        self.send_header("Content-Length", str(len(payload)))
        if retry_after is not None:
            self.send_header("Retry-After", retry_after)
        self.end_headers()
        self.wfile.write(payload)

    def log_message(self, format: str, *args: object) -> None:  # noqa: A002
        del format, args


@pytest.fixture
def site() -> Iterator[str]:
    _Site.agents = []
    _Site.robots = "User-agent: *\nDisallow: /private\n"
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), functools.partial(_Site))
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()


def _driver(admission: DocumentAdmission | None, suffix: str = "") -> PlaywrightDriver:
    return PlaywrightDriver(
        UrlPolicy(),
        DriverSettings(allow_private_network=True, user_agent_suffix=suffix),
        admission,
    )


def _polite(**settings: float) -> PoliteAccess:
    chosen: dict[str, float] = {"min_interval_seconds": 0.0} | settings
    return PoliteAccess(
        UrlPolicy(),
        PolitenessSettings(user_agent="wcc/test", **chosen),  # type: ignore[arg-type]
    )


@pytest.fixture
async def polite_driver() -> AsyncIterator[PlaywrightDriver]:
    driver = _driver(_polite())
    try:
        yield driver
    finally:
        await driver.shutdown()


async def test_a_page_robots_txt_disallows_never_becomes_a_session(
    polite_driver: PlaywrightDriver, site: str
) -> None:
    with pytest.raises(RobotsDisallowedError) as refused:
        await polite_driver.open_session(OpenSessionRequest(url=f"{site}/private/page.html"))
    assert refused.value.code == "robots_disallowed"
    assert "/private/page.html" in refused.value.message
    assert all(path != "/private/page.html" for path, _ in _Site.agents)


async def test_an_allowed_page_opens(polite_driver: PlaywrightDriver, site: str) -> None:
    session = await polite_driver.open_session(OpenSessionRequest(url=f"{site}/index.html"))
    assert session.session_id
    assert [path for path, _ in _Site.agents][:2] == ["/robots.txt", "/index.html"]


async def test_following_a_link_into_a_disallowed_path_fails_the_step_with_the_code(
    polite_driver: PlaywrightDriver, site: str
) -> None:
    session = await polite_driver.open_session(OpenSessionRequest(url=f"{site}/index.html"))
    elements = await polite_driver.extract_elements(session.session_id)
    link = next(e for e in elements if e.resolved_label == "private")

    observation = await polite_driver.execute_action(
        session.session_id, PlannedAction(action=ActionType.ACTIVATE, element_id=link.element_id)
    )
    assert not observation.succeeded
    assert observation.error_code == "robots_disallowed"
    assert not observation.document_changed
    assert any(
        event.blocked and (event.reason or "").startswith("robots_disallowed")
        for event in observation.navigation_events
    )
    assert all(path != "/private/page.html" for path, _ in _Site.agents)
    # The refusal costs the agent the step, not the session: the page it was on
    # is still there to act on.
    snapshot = await polite_driver.snapshot(session.session_id)
    assert snapshot.url.endswith("/index.html")


async def test_an_unreadable_robots_txt_refuses_the_page(
    polite_driver: PlaywrightDriver, site: str
) -> None:
    del site
    # Nothing listens on this port, so robots.txt is unreachable, not missing.
    with pytest.raises(RobotsDisallowedError, match="could not be read"):
        await polite_driver.open_session(OpenSessionRequest(url="http://127.0.0.1:9/index.html"))


async def test_a_second_page_too_soon_is_refused_with_the_wait(site: str) -> None:
    driver = _driver(_polite(min_interval_seconds=60, max_wait_seconds=0))
    try:
        await driver.open_session(OpenSessionRequest(url=f"{site}/index.html"))
        with pytest.raises(RateLimitedError) as refused:
            await driver.open_session(OpenSessionRequest(url=f"{site}/public.html"))
        assert refused.value.code == "rate_limited"
        assert 55 < refused.value.retry_after_seconds <= 60
    finally:
        await driver.shutdown()


async def test_a_429_from_the_page_holds_the_host_back(
    polite_driver: PlaywrightDriver, site: str
) -> None:
    await polite_driver.open_session(OpenSessionRequest(url=f"{site}/busy.html"))
    with pytest.raises(RateLimitedError) as refused:
        await polite_driver.open_session(OpenSessionRequest(url=f"{site}/public.html"))
    assert 115 < refused.value.retry_after_seconds <= 120


async def test_the_browser_names_itself_with_the_product_token(site: str) -> None:
    driver = _driver(_polite(), suffix="wcc/test (+https://example.invalid)")
    try:
        await driver.open_session(OpenSessionRequest(url=f"{site}/index.html"))
    finally:
        await driver.shutdown()
    agents = dict(_Site.agents)
    assert agents["/robots.txt"] == "wcc/test"
    page_agent = agents["/index.html"]
    assert page_agent.endswith(" wcc/test (+https://example.invalid)")
    assert "Chrome" in page_agent


async def test_without_an_admission_the_driver_behaves_as_before(site: str) -> None:
    """The benchmarks build their drivers this way and measured them this way."""
    driver = _driver(None)
    try:
        await driver.open_session(OpenSessionRequest(url=f"{site}/private/page.html"))
    finally:
        await driver.shutdown()
    paths = [path for path, _ in _Site.agents]
    assert "/robots.txt" not in paths
    assert "/private/page.html" in paths
    assert "wcc" not in dict(_Site.agents)["/private/page.html"]


async def test_the_runtime_always_runs_with_the_polite_default(tmp_path: Path) -> None:
    """``build_service`` is the only way the MCP server builds its driver, and it
    has no way to leave the admission out."""
    service = build_service(Store(tmp_path / "wcc.db"))
    try:
        admission = service.driver.admission
        assert isinstance(admission, PoliteAccess)
        assert admission.settings.user_agent == USER_AGENT_PRODUCT
        assert admission.settings.min_interval_seconds == 1.0
        assert admission.settings.product_token == "wcc"
        assert service.driver._settings.user_agent_suffix == USER_AGENT_PRODUCT
        assert USER_AGENT_PRODUCT.startswith("wcc/")
    finally:
        await service.driver.shutdown()
        service.store.close()


async def test_the_mcp_tool_reports_a_refusal_by_its_code(
    site: str, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    monkeypatch.setenv("WCC_ALLOW_PRIVATE_NETWORK", "1")
    service = build_service(Store(tmp_path / "wcc.db"), admission=_polite())
    server: Any = build_server(service)
    try:
        with pytest.raises(Exception, match="robots_disallowed: .* does not allow 'wcc'"):
            await server.call_tool("open_session", {"url": f"{site}/private/page.html"})

        opened = await server.call_tool("open_session", {"url": f"{site}/public.html"})
        assert not opened.is_error, opened.content
    finally:
        await service.driver.shutdown()
        service.store.close()


async def test_an_admission_that_breaks_refuses_rather_than_hangs(site: str) -> None:
    class Broken:
        async def admit(
            self, url: str, *, allow_private_network: bool, redirected_from: str | None = None
        ) -> None:
            del url, allow_private_network, redirected_from
            raise ValueError("no idea")

        def observe(self, url: str, status: int, retry_after: str | None) -> None:
            del url, status, retry_after

    driver = _driver(Broken())
    try:
        with pytest.raises(NavigationBlockedError, match="could not decide.*no idea"):
            await driver.open_session(OpenSessionRequest(url=f"{site}/index.html"))
    finally:
        await driver.shutdown()
