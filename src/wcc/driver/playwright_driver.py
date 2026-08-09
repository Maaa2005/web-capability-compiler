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
from urllib.parse import urlsplit

from playwright.async_api import (
    Browser,
    BrowserContext,
    Download,
    FileChooser,
    Frame,
    Locator,
    Page,
    Playwright,
    Request,
    Response,
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
    LimitExceededError,
    NavigationBlockedError,
    NavigationGuard,
    SessionFailedError,
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
_MUTATION_CLOCK = "__wccLastMutation"
_IDENTITY_READ_ATTEMPTS = 3

#: How often the settle loop asks the page whether it has gone quiet. Short enough
#: that the quiet period is measured rather than rounded up to it.
_SETTLE_POLL_MS = 50

#: Schemes whose documents arrive through the routing layer. Not a policy list —
#: the policy's allowed set lives in :mod:`wcc.policy.url_policy` and is the
#: runtime's to decide. This is the driver stating which commits it can actually
#: see, so that a document arriving by any other route is treated as unchecked.
_NETWORK_SCHEMES = frozenset({"http", "https"})

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

# The DOM half of the settle definition. Network quiet alone calls a page settled
# while a framework is still rendering the response it already received, so the
# page reports when it last changed and the driver waits for that to go quiet too.
# The clock is a plain property rather than a frozen one: it must stay writable to
# be a clock, and a page that lies about it can only make itself look busy — the
# settle timeout bounds that, and nothing downstream trusts the value for anything
# but waiting.
_MUTATION_CLOCK_SCRIPT = f"""
(() => {{
  window[{_MUTATION_CLOCK!r}] = Date.now();
  const bump = () => {{ window[{_MUTATION_CLOCK!r}] = Date.now(); }};
  const start = () => {{
    new MutationObserver(bump).observe(document.documentElement, {{
      subtree: true,
      childList: true,
      attributes: true,
      characterData: true,
    }});
  }};
  if (document.documentElement) {{
    start();
  }} else {{
    document.addEventListener('readystatechange', function once() {{
      if (document.documentElement) {{
        document.removeEventListener('readystatechange', once);
        start();
      }}
    }});
  }}
}})();
"""

#: Read together so the two halves of the answer describe the same instant.
_SETTLE_PROBE = f"""
() => ({{
  quietFor: Date.now() - (window[{_MUTATION_CLOCK!r}] || 0),
  ready: document.readyState,
}})
"""


def _redirect_depth(request: Request) -> int:
    """Count the hops that led to ``request``.

    Walked from the request rather than counted per session: two navigations in one
    session are two chains, and a counter that did not reset would refuse the
    second for the sins of the first.
    """
    depth = 0
    hop = request.redirected_from
    while hop is not None:
        depth += 1
        hop = hop.redirected_from
    return depth


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
    in_flight: int = 0
    """Requests started and not yet finished or failed.

    Counted from context events rather than from the route handler: a request the
    handler aborts still has to be seen through to its failure, and a handler that
    both admitted and counted would have to be correct twice.
    """

    failure: str | None = None
    """Why the session was failed closed, if it was (specification 10.2)."""

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
        self._limits = self._settings.limits
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
            if len(self._sessions) + len(self._reserved) >= self._limits.max_active_sessions:
                raise SessionLimitExceededError(
                    f"{len(self._sessions) + len(self._reserved)} sessions are already "
                    f"active or opening; the configured maximum is "
                    f"{self._limits.max_active_sessions}"
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
                await page.goto(request.url, timeout=self._limits.page_load_timeout_ms)
            except PlaywrightTimeoutError as exc:
                raise DriverTimeoutError(
                    f"loading {request.url} exceeded {self._limits.page_load_timeout_ms} ms"
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

        Settled means three things at once, and all three are needed. The document
        has finished loading. Nothing is in flight. And the DOM has not changed for
        the configured quiet period.

        Network idle alone — what this used to wait for — reports a page settled at
        the moment the last response arrives, which is before the framework that
        asked for it has rendered anything. A snapshot taken there sees the old
        page and calls it the new one. Requiring DOM quiet as well is what moves
        the wait from "the data arrived" to "the page stopped reacting to it".

        The three conditions must hold *together*, so any of them failing restarts
        the wait rather than banking the progress: a render that triggers another
        fetch is not a settled page, however quiet the instant between them was.
        """
        session = self._session(session_id)
        budget = min(timeout_ms, self._limits.settle_timeout_ms)
        quiet_period = self._limits.settle_quiet_period_ms
        loop = asyncio.get_running_loop()
        deadline = loop.time() + budget / 1000

        while True:
            remaining = deadline - loop.time()
            if remaining <= 0:
                raise DriverTimeoutError(f"page did not settle within {budget} ms")
            if session.in_flight == 0:
                probe = await self._probe_settle(session)
                if (
                    probe is not None
                    and probe["ready"] == "complete"
                    and probe["quiet_for"] >= quiet_period
                    # Re-read: evaluating the probe took time, and a request that
                    # started during it means the quiet reading is already stale.
                    and session.in_flight == 0
                ):
                    return
            await asyncio.sleep(min(_SETTLE_POLL_MS / 1000, remaining))

    async def _probe_settle(self, session: _Session) -> dict[str, Any] | None:
        """Ask the page how long it has been quiet, or ``None`` if it cannot answer.

        A page mid-navigation destroys the execution context under the evaluate.
        That is not a failure to settle, it is a page that has not started; the
        caller keeps waiting and the deadline still applies.
        """
        try:
            raw = await session.page.evaluate(_SETTLE_PROBE)
        except PlaywrightError:
            return None
        if not isinstance(raw, dict):
            return None
        return {"ready": raw.get("ready"), "quiet_for": raw.get("quietFor") or 0}

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
            if len(elements) > self._limits.max_elements_per_snapshot:
                raise LimitExceededError(
                    f"the page has more than {self._limits.max_elements_per_snapshot} "
                    "eligible elements; specification 10.5 refuses the snapshot rather "
                    "than returning a subset an agent would mistake for all of them"
                )
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
        collections, warnings = self._apply_display_limits(collections)

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
            warnings=warnings,
        )

    def _apply_display_limits(
        self, collections: list[CollectionSnapshot]
    ) -> tuple[list[CollectionSnapshot], list[str]]:
        """Trim the parts of a snapshot an agent reads but never acts on.

        Specification 10.5 allows truncating non-critical display data and requires
        saying so. Collections qualify: an agent reads them, and postconditions
        compare ``collection_hash`` values, which are computed over what was
        actually kept. Elements do not qualify and are never trimmed here — an
        element an agent cannot address is a capability that silently lost a step.

        ``record_count`` and ``collection_hash`` keep describing what the page held,
        not what survived the trim. Both are evidence about the page: a
        postcondition that compares them must not read our truncation as the result
        set having shrunk. ``truncated`` is what tells a reader the records list is
        the short version.
        """
        warnings: list[str] = []
        kept = collections
        if len(collections) > self._limits.max_collections_per_snapshot:
            warnings.append(
                f"kept {self._limits.max_collections_per_snapshot} of "
                f"{len(collections)} collections (specification 10.5 limit)"
            )
            kept = collections[: self._limits.max_collections_per_snapshot]

        trimmed: list[CollectionSnapshot] = []
        for collection in kept:
            if len(collection.records) <= self._limits.max_records_per_collection:
                trimmed.append(collection)
                continue
            warnings.append(
                f"collection {collection.collection_id} kept "
                f"{self._limits.max_records_per_collection} of "
                f"{len(collection.records)} records (specification 10.5 limit)"
            )
            trimmed.append(
                collection.model_copy(
                    update={
                        "records": collection.records[: self._limits.max_records_per_collection],
                        "truncated": True,
                    }
                )
            )
        return trimmed, warnings

    async def resolve(self, session_id: str, element_id: str) -> str:
        """Re-resolve an element this session observed, by its ID.

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
        return await self.resolve_chain(session_id, chain, described_as=element_id)

    async def resolve_chain(
        self, session_id: str, chain: LocatorChain, *, described_as: str = "the target"
    ) -> str:
        """Re-resolve from a locator chain that no session has to have observed.

        This is what lets a template saved yesterday bind today. A template holds
        chains, never element IDs — specification 7.8 forbids it to hold a live
        handle — so binding cannot go through :meth:`resolve`, whose whole input is
        an ID minted by one session's observation. The re-resolution rule is
        identical either way; only where the chain comes from differs.
        """
        session = self._session(session_id)
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
            f"{described_as} is stale: no candidate matched exactly once ({', '.join(attempts)})"
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
        timeout = self._limits.execution_timeout_ms

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
            await self.wait_settled(session_id, self._limits.settle_timeout_ms)
        except DriverTimeoutError:
            return False
        return True

    # -- internals ---------------------------------------------------------

    async def _audit_peer(self, session: _Session, response: Response) -> None:
        """Fail the session closed if the connected address would not have passed.

        The peer is judged by the same rules as a resolved address, through the
        guard the runtime supplied, so there is no second policy here to drift out
        of step with the first.
        """
        if session.failure is not None:
            return
        try:
            peer = await response.server_addr()
        except PlaywrightError:
            return
        if not peer:
            return
        address = peer.get("ipAddress")
        if not address:
            return

        # A bare address is checked as itself: passing it back through the guard as
        # a URL means the same category rules apply, and the resolver returns the
        # literal unchanged rather than asking DNS a second question.
        host = f"[{address}]" if ":" in address else address
        try:
            self._guard.check(
                f"{urlsplit(response.url).scheme}://{host}",
                allow_private_network=session.allow_private_network,
            )
        except NavigationBlockedError as blocked:
            self._fail_session(
                session,
                f"{response.url} was served from {address}, which the policy "
                f"refuses: {blocked.message}",
                kind=(
                    "navigation" if response.request.resource_type == "document" else "subresource"
                ),
            )

    def _fail_session(self, session: _Session, reason: str, *, kind: str) -> None:
        """Record the breach and close the session's page to it.

        Recording before closing matters: the runtime learns *why* from the drained
        events, and a session closed without a reason is indistinguishable from one
        the agent closed itself.
        """
        if session.failure is not None:
            return
        session.failure = reason
        session.events.append(
            NavigationEvent(kind=kind, url=session.page.url, blocked=True, reason=reason)
        )
        self._spawn(session.context.close())

    def _session(self, session_id: str) -> _Session:
        session = self._sessions.get(session_id)
        if session is None:
            raise SessionNotFoundError(f"no active session {session_id}")
        if session.failure is not None:
            # Every entry point comes through here, so a failed session cannot be
            # observed or acted on by any route, only closed.
            raise SessionFailedError(session.failure)
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
        await session.context.add_init_script(_MUTATION_CLOCK_SCRIPT)

        def on_request(request: Request) -> None:
            del request
            session.in_flight += 1

        def on_request_done(request: Request) -> None:
            del request
            session.in_flight = max(0, session.in_flight - 1)

        session.context.on("request", on_request)
        session.context.on("requestfinished", on_request_done)
        session.context.on("requestfailed", on_request_done)

        def on_response(response: Response) -> None:
            """Check the address Chromium actually connected to (specification 10.2).

            The policy resolves a hostname with ``getaddrinfo`` while Chromium
            resolves it independently, so the address that was checked is not
            provably the address that was reached. That gap is the DNS-rebinding
            window, and no amount of re-resolving in the policy closes it — it only
            proves the policy keeps getting the same answer.

            The connected peer is the one fact that settles it. Playwright reports
            it per response, after the connection exists, so this cannot prevent the
            connection; what it can do is stop the session before anything observed
            over that connection becomes evidence.
            """
            self._spawn(self._audit_peer(session, response))

        session.page.on("response", on_response)

        def on_frame_navigated(frame: Frame) -> None:
            """Catch a document the route handler never saw.

            ``window.location = "about:blank"``, and the same move with ``data:``,
            ``blob:``, or a ``srcdoc`` frame, commits a document without issuing a
            network request. The route handler is a network boundary, so none of
            these reach it, and a snapshot taken afterwards would describe a
            document that passed no check at all.
            """
            if frame is not session.page.main_frame:
                return
            scheme = urlsplit(frame.url).scheme.lower()
            if scheme in _NETWORK_SCHEMES or not frame.url:
                return
            # ``about:blank`` is where every context starts, so it is only a breach
            # once the session has committed the document it was opened for.
            if frame.url == "about:blank" and session.document_token is None:
                return
            self._fail_session(
                session,
                f"the page committed {frame.url!r}, a document the network boundary never saw",
                kind="navigation",
            )

        session.page.on("framenavigated", on_frame_navigated)

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
                if _redirect_depth(request) > self._limits.max_redirects_per_navigation:
                    reason = (
                        f"the redirect chain exceeded "
                        f"{self._limits.max_redirects_per_navigation} hops"
                    )
                    session.events.append(
                        NavigationEvent(
                            kind="redirect", url=request.url, blocked=True, reason=reason
                        )
                    )
                    await route.abort("blockedbyclient")
                    return
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
