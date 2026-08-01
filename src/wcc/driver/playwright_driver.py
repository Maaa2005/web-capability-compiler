"""The Playwright implementation of :class:`~wcc.driver.base.Driver` (v0.1).

This module is the only place in the runtime allowed to import Playwright
(specification 4.1, ``AGENTS.md`` rule 10); ``tests/test_driver_isolation.py``
enforces that with an AST walk.

Navigation is validated *before* it is committed. Playwright's routing layer sees
every document request, including each redirect hop, so the guard runs on the hop
itself and a refused hop is aborted rather than loaded and regretted. Downloads,
popups, and newly opened pages are recorded as :class:`NavigationEvent` values and
left for the runtime policy layer to judge; the driver never decides policy
(specification 4.2).
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import UTC, datetime

from playwright.async_api import (
    Browser,
    BrowserContext,
    Page,
    Playwright,
    Route,
    async_playwright,
)
from playwright.async_api import (
    Error as PlaywrightError,
)
from playwright.async_api import (
    TimeoutError as PlaywrightTimeoutError,
)

from wcc.compiler.route import normalize_route_pattern, origin_of
from wcc.driver.base import (
    DocumentIdentity,
    DriverError,
    DriverNotReadyError,
    DriverSettings,
    DriverTimeoutError,
    NavigationBlockedError,
    NavigationGuard,
    SessionLimitExceededError,
    SessionNotFoundError,
)
from wcc.identifiers import new_id
from wcc.models.action import ActionObservation, NavigationEvent, PlannedAction
from wcc.models.common import SessionStatus
from wcc.models.session import OpenSessionRequest, PageSnapshot, SessionInfo

_DOCUMENT_TOKEN = "__wccDocumentToken"

# Stamped into every new document before page scripts run. A same-document update
# such as ``history.pushState`` leaves the token in place, which is exactly the
# distinction specification 7.2 draws between a document and an observation. The
# property is non-writable and non-configurable so a hostile page cannot forge a
# stable identity for a document it replaced (specification 10.3).
_DOCUMENT_TOKEN_SCRIPT = f"""
(() => {{
  const token =
    Math.random().toString(36).slice(2) + '-' + Date.now().toString(36);
  Object.defineProperty(window, {_DOCUMENT_TOKEN!r}, {{
    value: token,
    writable: false,
    configurable: false,
    enumerable: false,
  }});
}})();
"""


@dataclass
class _Session:
    """Driver-side state for one browser context with one active page."""

    session_id: str
    context: BrowserContext
    page: Page
    initial_url: str
    created_at: datetime
    allow_private_network: bool
    events: list[NavigationEvent] = field(default_factory=list)
    document_token: str | None = None
    current_document_id: str | None = None
    current_snapshot_id: str | None = None


class PlaywrightDriver:
    """Drive Chromium through Playwright's async API.

    The navigation guard is a constructor requirement rather than an optional
    extra: a driver with no guard would silently navigate anywhere, and
    specification 4.2 keeps policy decisions out of the driver entirely.
    """

    def __init__(
        self,
        navigation_guard: NavigationGuard,
        settings: DriverSettings | None = None,
    ) -> None:
        self._guard = navigation_guard
        self._settings = settings or DriverSettings()
        self._playwright: Playwright | None = None
        self._browsers: dict[bool, Browser] = {}
        self._sessions: dict[str, _Session] = {}

    # -- lifecycle ---------------------------------------------------------

    async def open_session(self, request: OpenSessionRequest) -> SessionInfo:
        if len(self._sessions) >= self._settings.max_active_sessions:
            raise SessionLimitExceededError(
                f"{len(self._sessions)} sessions are already active; "
                f"the configured maximum is {self._settings.max_active_sessions}"
            )
        self._guard.check(request.url, allow_private_network=request.allow_private_network)

        browser = await self._browser(headless=request.headless)
        context = await browser.new_context(locale=request.locale)
        page = await context.new_page()
        session = _Session(
            session_id=new_id("sess"),
            context=context,
            page=page,
            initial_url=request.url,
            created_at=datetime.now(UTC),
            allow_private_network=request.allow_private_network,
        )
        await self._arm_observers(session)

        try:
            await page.goto(request.url, timeout=self._settings.page_load_timeout_ms)
        except PlaywrightTimeoutError as exc:
            await context.close()
            raise DriverTimeoutError(
                f"loading {request.url} exceeded {self._settings.page_load_timeout_ms} ms"
            ) from exc
        except PlaywrightError as exc:
            await context.close()
            blocked = next((e for e in session.events if e.blocked), None)
            if blocked is not None:
                raise NavigationBlockedError(
                    blocked.reason or f"navigation to {blocked.url} was refused"
                ) from exc
            raise

        self._sessions[session.session_id] = session
        await self.identify(session.session_id)
        return SessionInfo(
            session_id=session.session_id,
            created_at=session.created_at,
            initial_url=session.initial_url,
            current_document_id=session.current_document_id,
            current_snapshot_id=session.current_snapshot_id,
            status=SessionStatus.ACTIVE,
        )

    async def close_session(self, session_id: str) -> None:
        session = self._sessions.pop(session_id, None)
        if session is None:
            raise SessionNotFoundError(f"no active session {session_id}")
        await session.context.close()

    async def shutdown(self) -> None:
        """Close every session and release the browser processes."""
        for session_id in list(self._sessions):
            await self.close_session(session_id)
        for browser in self._browsers.values():
            await browser.close()
        self._browsers.clear()
        if self._playwright is not None:
            await self._playwright.stop()
            self._playwright = None

    # -- observation -------------------------------------------------------

    async def wait_settled(self, session_id: str, timeout_ms: int) -> None:
        """Wait for the page to stop changing, bounded by ``timeout_ms``.

        Task T8 replaces this with the full settle definition of the
        specification, which also accounts for in-flight XHR and DOM mutation
        quiescence. Waiting for the network to go idle is the honest subset of
        that behaviour available today.
        """
        session = self._session(session_id)
        budget = min(timeout_ms, self._settings.settle_timeout_ms)
        try:
            await session.page.wait_for_load_state("networkidle", timeout=budget)
        except PlaywrightTimeoutError as exc:
            raise DriverTimeoutError(f"page did not settle within {budget} ms") from exc

    async def identify(self, session_id: str) -> DocumentIdentity:
        """Mint a fresh snapshot identity, reusing the document identity if it holds.

        The document token is read from the page rather than inferred from
        navigation events: ``framenavigated`` fires for same-document history
        updates too, so trusting it would hand out a new ``document_id`` for a
        ``pushState`` that never replaced the document.
        """
        session = self._session(session_id)
        token = await session.page.evaluate(f"() => window.{_DOCUMENT_TOKEN} ?? null")
        if not isinstance(token, str):
            raise DriverError(
                "the current document carries no WCC token; it was created before the "
                "session was armed or the page is not a document this driver opened"
            )
        if token != session.document_token:
            session.document_token = token
            session.current_document_id = new_id("doc")

        snapshot_id = new_id("snap")
        session.current_snapshot_id = snapshot_id
        url = session.page.url
        language = await session.page.evaluate("() => document.documentElement.lang || null")
        document_id = session.current_document_id
        assert document_id is not None  # noqa: S101 - set immediately above
        return DocumentIdentity(
            session_id=session.session_id,
            document_id=document_id,
            snapshot_id=snapshot_id,
            captured_at=datetime.now(UTC),
            url=url,
            origin=origin_of(url),
            normalized_route_pattern=normalize_route_pattern(url),
            title=await session.page.title() or None,
            language=language if isinstance(language, str) and language else None,
        )

    def drain_navigation_events(self, session_id: str) -> list[NavigationEvent]:
        """Hand the runtime every navigation-adjacent effect seen since the last call."""
        session = self._session(session_id)
        events = list(session.events)
        session.events.clear()
        return events

    async def snapshot(self, session_id: str) -> PageSnapshot:
        self._session(session_id)
        raise DriverNotReadyError(
            "snapshot arrives with Phase 1 task T2 (session/document/snapshot identity) "
            "and task T4 (element normalization)"
        )

    async def execute_action(
        self,
        session_id: str,
        action: PlannedAction,
    ) -> ActionObservation:
        self._session(session_id)
        raise DriverNotReadyError(
            f"executing {action.action} arrives with Phase 1 task T5, which adds the "
            "locator re-resolution the driver needs to address an element"
        )

    # -- internals ---------------------------------------------------------

    def _session(self, session_id: str) -> _Session:
        session = self._sessions.get(session_id)
        if session is None:
            raise SessionNotFoundError(f"no active session {session_id}")
        return session

    async def _browser(self, *, headless: bool) -> Browser:
        if self._playwright is None:
            self._playwright = await async_playwright().start()
        browser = self._browsers.get(headless)
        if browser is None:
            browser = await self._playwright.chromium.launch(headless=headless)
            self._browsers[headless] = browser
        return browser

    async def _arm_observers(self, session: _Session) -> None:
        """Stamp document identity, route requests through the guard, record effects."""
        await session.context.add_init_script(_DOCUMENT_TOKEN_SCRIPT)

        async def on_route(route: Route) -> None:
            request = route.request
            if request.resource_type != "document":
                await route.continue_()
                return
            try:
                self._guard.check(
                    request.url,
                    allow_private_network=session.allow_private_network,
                )
            except NavigationBlockedError as blocked:
                session.events.append(
                    NavigationEvent(
                        kind="redirect" if request.redirected_from else "navigation",
                        url=request.url,
                        blocked=True,
                        reason=blocked.message,
                    )
                )
                await route.abort("blockedbyclient")
                return
            if request.redirected_from is not None:
                session.events.append(NavigationEvent(kind="redirect", url=request.url))
            await route.continue_()

        await session.context.route("**/*", on_route)

        session.context.on(
            "page",
            lambda page: session.events.append(NavigationEvent(kind="new_page", url=page.url)),
        )
        session.page.on(
            "popup",
            lambda page: session.events.append(NavigationEvent(kind="popup", url=page.url)),
        )
        session.page.on(
            "download",
            lambda download: session.events.append(
                NavigationEvent(kind="download", url=download.url)
            ),
        )
