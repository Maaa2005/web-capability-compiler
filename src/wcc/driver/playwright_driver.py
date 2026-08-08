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

import asyncio
from collections.abc import Coroutine
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any

from playwright.async_api import (
    Browser,
    BrowserContext,
    Download,
    FileChooser,
    Locator,
    Page,
    Playwright,
    Request,
    Route,
    async_playwright,
)
from playwright.async_api import (
    Error as PlaywrightError,
)
from playwright.async_api import (
    TimeoutError as PlaywrightTimeoutError,
)

from wcc.compiler.collections import build_collection, parse_structured_data
from wcc.compiler.fingerprints import page_signature, structural_hash
from wcc.compiler.route import normalize_route_pattern, origin_of
from wcc.driver.base import (
    DocumentIdentity,
    DriverError,
    DriverNotReadyError,
    DriverSettings,
    DriverTimeoutError,
    ElementNotResolvedError,
    NavigationBlockedError,
    NavigationGuard,
    SessionLimitExceededError,
    SessionNotFoundError,
)
from wcc.driver.collection_script import EXTRACT_COLLECTIONS
from wcc.driver.normalize import classify_control, is_eligible, normalize_element
from wcc.driver.page_script import EXTRACT_ELEMENTS, EXTRACT_LANDMARKS
from wcc.driver.resolve import RESOLVE_CANDIDATE
from wcc.identifiers import new_id
from wcc.models.action import ActionObservation, NavigationEvent, PlannedAction
from wcc.models.common import ActionType, SessionStatus
from wcc.models.element import CollectionSnapshot, NormalizedElement
from wcc.models.locator import LocatorChain
from wcc.models.session import (
    OpenSessionRequest,
    PageSnapshot,
    SessionInfo,
    StructuredDataBlock,
)

_DOCUMENT_TOKEN = "__wccDocumentToken"
_IDENTITY_READ_ATTEMPTS = 3

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


def _event_kind(request: Request) -> str:
    """Name a refused request so the runtime can tell a hop from a subresource."""
    if request.resource_type != "document":
        return "subresource"
    return "redirect" if request.redirected_from else "navigation"


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
    chains: dict[str, LocatorChain] = field(default_factory=dict)
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
        self._reserved: set[str] = set()
        self._lock = asyncio.Lock()
        self._cleanups: set[asyncio.Task[Any]] = set()

    # -- lifecycle ---------------------------------------------------------

    async def open_session(self, request: OpenSessionRequest) -> SessionInfo:
        allow_private = self._settings.allow_private_network
        self._guard.check(request.url, allow_private_network=allow_private)

        session_id = new_id("sess")
        async with self._lock:
            # The slot is reserved under the lock and released on failure. Counting
            # sessions before the awaits that create one would let two concurrent
            # callers both see room that only one of them can have.
            if len(self._sessions) + len(self._reserved) >= self._settings.max_active_sessions:
                raise SessionLimitExceededError(
                    f"{len(self._sessions) + len(self._reserved)} sessions are already "
                    f"active or opening; the configured maximum is "
                    f"{self._settings.max_active_sessions}"
                )
            self._reserved.add(session_id)

        context: BrowserContext | None = None
        try:
            browser = await self._browser(headless=request.headless)
            # ``accept_downloads`` defaults to true, which writes the file first and
            # lets the handler cancel afterwards. Refusing at the context means a
            # blocked download never reaches the filesystem at all.
            context = await browser.new_context(
                locale=request.locale,
                accept_downloads=False,
                service_workers="block",
            )
            page = await context.new_page()
            session = _Session(
                session_id=session_id,
                context=context,
                page=page,
                initial_url=request.url,
                created_at=datetime.now(UTC),
                allow_private_network=allow_private,
            )
            await self._arm_observers(session)

            try:
                await page.goto(request.url, timeout=self._settings.page_load_timeout_ms)
            except PlaywrightTimeoutError as exc:
                raise DriverTimeoutError(
                    f"loading {request.url} exceeded {self._settings.page_load_timeout_ms} ms"
                ) from exc
            except PlaywrightError as exc:
                blocked = next((e for e in session.events if e.blocked), None)
                if blocked is not None:
                    raise NavigationBlockedError(
                        blocked.reason or f"navigation to {blocked.url} was refused"
                    ) from exc
                raise

            self._sessions[session_id] = session
            await self.identify(session_id)
        except BaseException:
            # Includes cancellation: a context whose ID was never returned can never
            # be closed by the caller, so it must not outlive this call.
            self._sessions.pop(session_id, None)
            if context is not None:
                await context.close()
            raise
        finally:
            self._reserved.discard(session_id)

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

    def _spawn(self, work: Coroutine[Any, Any, Any]) -> None:
        """Run cleanup that a synchronous Playwright event handler cannot await.

        The task is held per driver, not in a module-level set, so
        :meth:`shutdown` can wait for it. Without that wait, a popup close or a
        download cancel scheduled moments earlier would still be pending when
        Playwright stops, and the refusal would not actually have happened.
        """

        def _finish(task: asyncio.Task[Any]) -> None:
            self._cleanups.discard(task)
            if not task.cancelled():
                # The page or download may already be gone; the refusal itself is
                # recorded as a blocked navigation event either way.
                task.exception()

        task = asyncio.ensure_future(work)
        self._cleanups.add(task)
        task.add_done_callback(_finish)

    async def drain_cleanups(self) -> None:
        """Wait for every scheduled refusal to finish."""
        while self._cleanups:
            await asyncio.gather(*list(self._cleanups), return_exceptions=True)

    async def shutdown(self) -> None:
        """Close every session and release the browser processes."""
        await self.drain_cleanups()
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
        token, url, language, title = await self._read_document(session)
        if token != session.document_token:
            session.document_token = token
            session.current_document_id = new_id("doc")

        snapshot_id = new_id("snap")
        session.current_snapshot_id = snapshot_id
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
            title=title,
            language=language,
        )

    async def _read_document(self, session: _Session) -> tuple[str, str, str | None, str | None]:
        """Read token, URL, language, and title as one consistent observation.

        Reading them across separate awaits lets a navigation land in between, so a
        result could carry document A's identity beside document B's URL and title.
        The token is re-read afterwards, and a mismatch means the page moved while
        it was being described: the read is simply repeated.
        """
        for _ in range(_IDENTITY_READ_ATTEMPTS):
            observed = await session.page.evaluate(
                f"""() => ({{
                    token: window.{_DOCUMENT_TOKEN} ?? null,
                    url: document.location.href,
                    language: document.documentElement.lang || null,
                    title: document.title || null,
                }})"""
            )
            if not isinstance(observed, dict) or not isinstance(observed.get("token"), str):
                raise DriverError(
                    "the current document carries no WCC token; it was created before "
                    "the session was armed or the page is not one this driver opened"
                )
            confirmation = await session.page.evaluate(f"() => window.{_DOCUMENT_TOKEN} ?? null")
            if confirmation == observed["token"]:
                return (
                    observed["token"],
                    str(observed["url"]),
                    observed.get("language"),
                    observed.get("title"),
                )
        raise DriverError(
            "the page kept navigating while it was being identified; no consistent "
            f"observation after {_IDENTITY_READ_ATTEMPTS} attempts"
        )

    async def extract_elements(self, session_id: str) -> list[NormalizedElement]:
        """Report every eligible interactive element on the current document.

        Element IDs are minted per observation rather than derived from the DOM:
        specification 7 requires opaque WCC-issued IDs, and a DOM-derived ID would
        leak page structure to an agent that must never receive a selector.
        """
        session = self._session(session_id)
        records = await session.page.evaluate(EXTRACT_ELEMENTS)
        elements: list[NormalizedElement] = []
        for record in records:
            if not isinstance(record, dict):
                continue
            control_type = classify_control(record)
            if not is_eligible(record, control_type):
                continue
            element = normalize_element(record, new_id("el"), session.page.url)
            session.chains[element.element_id] = element.locator_chain
            elements.append(element)
        return elements

    async def extract_collections(
        self, session_id: str
    ) -> tuple[list[CollectionSnapshot], list[StructuredDataBlock]]:
        """Report repeated structures and JSON-LD blocks (specification 8.4, 3.2)."""
        session = self._session(session_id)
        raw = await session.page.evaluate(EXTRACT_COLLECTIONS)
        if not isinstance(raw, dict):
            return [], []

        collections = [
            build_collection(entry)
            for entry in raw.get("collections", [])
            if isinstance(entry, dict)
        ]
        structured = [
            StructuredDataBlock(source="json-ld", data=block)
            for block in parse_structured_data(list(raw.get("structured", [])))
        ]
        return collections, structured

    def drain_navigation_events(self, session_id: str) -> list[NavigationEvent]:
        """Hand the runtime every navigation-adjacent effect seen since the last call."""
        session = self._session(session_id)
        events = list(session.events)
        session.events.clear()
        return events

    async def snapshot(self, session_id: str) -> PageSnapshot:
        """One complete observation of the current document (specification 7.2)."""
        session = self._session(session_id)
        identity = await self.identify(session_id)
        elements = await self.extract_elements(session_id)
        collections, structured = await self.extract_collections(session_id)
        landmarks = [
            str(entry)
            for entry in await session.page.evaluate(EXTRACT_LANDMARKS)
            if isinstance(entry, str)
        ]

        return PageSnapshot(
            session_id=identity.session_id,
            document_id=identity.document_id,
            snapshot_id=identity.snapshot_id,
            captured_at=identity.captured_at,
            url=identity.url,
            origin=identity.origin,
            normalized_route_pattern=identity.normalized_route_pattern,
            title=identity.title,
            language=identity.language,
            elements=elements,
            collections=collections,
            structured_data=structured,
            structural_hash=structural_hash(elements, collections, landmarks),
            page_signature=page_signature(
                identity.origin, identity.normalized_route_pattern, elements, landmarks
            ),
        )

    async def resolve(self, session_id: str, element_id: str) -> str:
        """Re-resolve an element, returning an in-page marker for the single match.

        Specification 7.7: candidates are tried in priority order, and only an
        exactly-one match counts. Both zero matches and an ambiguous match move on
        to the next candidate; neither is ever resolved by picking the closest
        element.
        """
        session = self._session(session_id)
        chain = session.chains.get(element_id)
        if chain is None:
            raise ElementNotResolvedError(
                f"{element_id} was not produced by an observation of this session"
            )

        attempts: list[str] = []
        for candidate in chain.ordered():
            outcome = await session.page.evaluate(
                RESOLVE_CANDIDATE,
                {
                    "kind": int(candidate.kind),
                    "key": candidate.key,
                    "role": candidate.role,
                    "form_id": candidate.form_id,
                },
            )
            count = outcome.get("count") if isinstance(outcome, dict) else None
            if isinstance(outcome, dict) and outcome.get("handle"):
                return str(outcome["handle"])
            attempts.append(f"{candidate.kind.name.lower()}={count}")

        raise ElementNotResolvedError(
            f"{element_id} is stale: no candidate matched exactly once ({', '.join(attempts)})"
        )

    async def execute_action(
        self,
        session_id: str,
        action: PlannedAction,
    ) -> ActionObservation:
        """Perform one allowlisted action against a freshly re-resolved element.

        Re-resolution happens per action rather than once per observation: the
        page may have changed since it was described, and specification 11.2 wants
        a stale target reported rather than guessed at.
        """
        session = self._session(session_id)
        before = session.current_document_id
        marker = await self.resolve(session_id, action.element_id)
        locator = session.page.locator(f"[data-wcc-resolved='{marker}']")
        timeout = self._settings.action_timeout_ms

        succeeded = True
        error_code: str | None = None
        error_message: str | None = None
        try:
            await self._perform(locator, action, timeout)
        except PlaywrightTimeoutError as exc:
            succeeded, error_code, error_message = False, "driver_timeout", str(exc)
        except PlaywrightError as exc:
            succeeded, error_code, error_message = False, "action_failed", str(exc)

        identity = await self.identify(session_id)
        return ActionObservation(
            action=action,
            succeeded=succeeded,
            observed_at=datetime.now(UTC),
            document_id=identity.document_id,
            snapshot_id=identity.snapshot_id,
            document_changed=before is not None and identity.document_id != before,
            settled=await self._settled_quietly(session_id),
            navigation_events=list(session.events),
            error_code=error_code,
            error_message=error_message,
        )

    async def _perform(self, locator: Locator, action: PlannedAction, timeout: int) -> None:
        """Map one allowlisted action onto Playwright (specification 10.4)."""
        if action.action is ActionType.FOCUS:
            await locator.focus(timeout=timeout)
        elif action.action is ActionType.SCROLL_INTO_VIEW:
            await locator.scroll_into_view_if_needed(timeout=timeout)
        elif action.action is ActionType.SET_VALUE:
            await locator.fill(action.value or "", timeout=timeout)
        elif action.action is ActionType.SELECT_OPTION:
            await locator.select_option(action.option, timeout=timeout)
        elif action.action is ActionType.SET_CHECKED:
            await locator.set_checked(bool(action.checked), timeout=timeout)
        elif action.action is ActionType.ACTIVATE:
            await locator.click(timeout=timeout)
        else:  # pragma: no cover - ActionType is closed and fully covered above
            raise DriverNotReadyError(f"{action.action} has no driver implementation")

    async def _settled_quietly(self, session_id: str) -> bool:
        """Report whether the page settled, without turning a busy page into an error."""
        try:
            await self.wait_settled(session_id, self._settings.settle_timeout_ms)
        except DriverTimeoutError:
            return False
        return True

    # -- internals ---------------------------------------------------------

    def _session(self, session_id: str) -> _Session:
        session = self._sessions.get(session_id)
        if session is None:
            raise SessionNotFoundError(f"no active session {session_id}")
        return session

    async def _browser(self, *, headless: bool) -> Browser:
        """Return the shared browser, launching it at most once.

        Serialized: two concurrent opens would otherwise each start Playwright and
        launch a browser, and the loser's process would be dropped from the dict
        while still running.
        """
        async with self._lock:
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
            """Judge every request, not only the top-level document.

            A page that can issue ``fetch("http://127.0.0.1:8080/admin")`` or embed
            ``<img src="http://192.168.1.1/reboot">`` reaches the private network
            just as effectively as a navigation would. Guarding documents alone
            secures the address bar and leaves the browser context open.
            """
            request = route.request
            try:
                self._guard.check(
                    request.url,
                    allow_private_network=session.allow_private_network,
                )
            except NavigationBlockedError as blocked:
                session.events.append(
                    NavigationEvent(
                        kind=_event_kind(request),
                        url=request.url,
                        blocked=True,
                        reason=blocked.message,
                    )
                )
                await route.abort("blockedbyclient")
                return
            if request.resource_type == "document" and request.redirected_from is not None:
                session.events.append(NavigationEvent(kind="redirect", url=request.url))
            await route.continue_()

        await session.context.route("**/*", on_route)

        def on_new_page(page: Page) -> None:
            """Refuse a popup, tab, or window and close it (specification 10.2)."""
            session.events.append(
                NavigationEvent(
                    kind="new_page",
                    url=page.url,
                    blocked=True,
                    reason="popups, tabs, and windows are refused by default",
                )
            )
            self._spawn(page.close())

        def on_download(download: Download) -> None:
            """Refuse a download rather than let it reach the filesystem."""
            session.events.append(
                NavigationEvent(
                    kind="download",
                    url=download.url,
                    blocked=True,
                    reason="downloads are blocked",
                )
            )
            self._spawn(download.cancel())

        def on_file_chooser(chooser: FileChooser) -> None:
            """Record a refused file chooser; specification 10.2 rejects uploads.

            Leaving the chooser unanswered is the refusal: setting no files is the
            only way to decline, and ``set_files`` is never called anywhere in this
            driver.
            """
            del chooser
            session.events.append(
                NavigationEvent(
                    kind="file_chooser",
                    blocked=True,
                    reason="file upload is not an allowed action",
                )
            )

        session.context.on("page", on_new_page)
        session.page.on("download", on_download)
        session.page.on("filechooser", on_file_chooser)
