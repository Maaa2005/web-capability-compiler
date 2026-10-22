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
import time
from collections.abc import Coroutine
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any
from urllib.parse import urlsplit

from playwright.async_api import (
    Browser,
    BrowserContext,
    Download,
    ElementHandle,
    FileChooser,
    Frame,
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
    ExtractionIncompleteError,
    LimitExceededError,
    NavigationBlockedError,
    NavigationGuard,
    SessionFailedError,
    SessionLimitExceededError,
    SessionNotFoundError,
)
from wcc.driver.collection_script import EXTRACT_COLLECTIONS
from wcc.driver.normalize import classify_control, is_eligible, normalize_element
from wcc.driver.page_script import DESCRIBE_ELEMENT, EXTRACT_ELEMENTS, EXTRACT_LANDMARKS
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
# page also has to stop changing.
#
# What the page exposes is a *count*, not a timestamp, and the count is behind a
# non-configurable getter over a closure variable. Both choices are about the same
# threat. An earlier version stored a timestamp in a writable property and argued
# that a lying page could only make itself look busy; that was wrong in the one
# direction that matters. Setting the clock to zero made "time since last change"
# enormous, so a page mutating continuously reported itself instantly quiet.
#
# A count cannot be moved backwards into looking quiet: quiet is *no change*, and
# any write the page makes is a change. The getter closes the other route — a page
# cannot redefine the property to freeze what the driver reads, the same protection
# specification 10.3 already asks for around the document token.
_MUTATION_CLOCK_SCRIPT = f"""
(() => {{
  let mutations = 0;
  Object.defineProperty(window, {_MUTATION_CLOCK!r}, {{
    get: () => mutations,
    configurable: false,
    enumerable: false,
  }});
  const start = () => {{
    new MutationObserver((records) => {{ mutations += records.length; }}).observe(
      document.documentElement,
      {{subtree: true, childList: true, attributes: true, characterData: true}},
    );
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
  mutations: window[{_MUTATION_CLOCK!r}],
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
    handles: dict[str, ElementHandle] = field(default_factory=dict)
    """Resolved elements, held by the driver and named by an opaque WCC ID.

    The page never learns these IDs and never sees the elements being tracked, so
    it cannot move a target between the moment it is resolved and the moment it is
    acted on.
    """

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
                # ``goto`` returns at the load event, which a client-rendered page
                # reaches with an empty body: crates.io had no link, button, or
                # input at that moment and eighty-nine a second later. The first
                # observation is the one an agent decides from, so it waits for the
                # same settle an action does. Best effort, because a page with a
                # carousel never goes quiet and must still open.
                await self._settled_quietly(session)
                # The peer check runs on a response event, which means it can still
                # be in flight here. Waiting for it before the session ID is handed
                # back is what makes the boundary a boundary rather than a race:
                # otherwise a caller could snapshot a page served from a refused
                # address in the gap.
                await self._settle_audits(session)
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

    async def _settle_audits(self, session: _Session) -> None:
        """Wait for any peer audit this navigation started to reach a verdict.

        Two yields before draining: Playwright dispatches the response event on this
        loop, and the handler that spawns the audit has to run before there is
        anything to drain.
        """
        for _ in range(2):
            await asyncio.sleep(0)
        if not await self.drain_cleanups():
            # An audit that never answered is an unverified peer, and specification
            # 10.2 does not treat "could not tell" as permission. The session is
            # torn down by the caller's error path either way; the error names a
            # timeout rather than a block because that is what actually happened,
            # and the two differ in whether retrying is sensible.
            raise DriverTimeoutError(
                f"a navigation audit did not finish within "
                f"{self._limits.settle_timeout_ms} ms, so the address this page "
                "was served from is unverified"
            )
        if session.failure is not None:
            raise NavigationBlockedError(session.failure)

    async def drain_cleanups(self, timeout_ms: int | None = None) -> bool:
        """Wait for every scheduled refusal to finish. Returns whether they all did.

        Bounded, and the bound is the point. This used to loop until the set
        emptied, which cannot terminate on two ordinary shapes of page: one whose
        ``server_addr()`` never answers, and one that keeps issuing requests so a
        new audit is spawned for every one that completes. Both presented as
        ``open_session`` never returning — no error, no log line, nothing to
        attribute it to. A wait on the critical path needs a deadline for the same
        reason every other wait in specification 10.5 has one.

        Reporting the outcome rather than raising leaves the decision with the
        caller: the navigation boundary must fail closed, while shutdown only needs
        to stop waiting.
        """
        budget = self._limits.settle_timeout_ms if timeout_ms is None else timeout_ms
        deadline = time.monotonic() + budget / 1000
        while self._cleanups:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                return False
            await asyncio.wait(list(self._cleanups), timeout=remaining)
        return True

    def _abandon_cleanups(self) -> None:
        """Cancel refusals that outlived their deadline, so nothing is left running.

        A cancelled audit still counts as unfinished; this only stops the task, it
        does not decide that the page was acceptable.
        """
        for task in list(self._cleanups):
            task.cancel()
        self._cleanups.clear()

    async def shutdown(self) -> None:
        """Close every session and release the browser processes."""
        if not await self.drain_cleanups():
            self._abandon_cleanups()
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
        await self._wait_settled(self._session(session_id), timeout_ms)

    async def _wait_settled(self, session: _Session, timeout_ms: int) -> None:
        """:meth:`wait_settled` for a session that may not be registered yet.

        ``open_session`` settles the page before the session ID exists for a
        caller, so the wait has to take the session itself rather than look it up.
        """
        budget = min(timeout_ms, self._limits.settle_timeout_ms)
        quiet_period = self._limits.settle_quiet_period_ms / 1000
        loop = asyncio.get_running_loop()
        deadline = loop.time() + budget / 1000

        # How long the page has been quiet is measured here rather than in the
        # page, because the page is the thing being measured. The driver keeps the
        # count it last saw and the moment it last changed; nothing the page can do
        # to its own state shortens that interval.
        seen: int | None = None
        changed_at = loop.time()

        while True:
            now = loop.time()
            remaining = deadline - now
            if remaining <= 0:
                raise DriverTimeoutError(f"page did not settle within {budget} ms")

            if session.in_flight == 0:
                probe = await self._probe_settle(session)
                if probe is None:
                    # Unreadable: mid-navigation, or a page that broke the property.
                    # Either way this is not a settled page, and the deadline still
                    # bounds the wait.
                    seen, changed_at = None, loop.time()
                else:
                    if probe["mutations"] != seen:
                        seen, changed_at = probe["mutations"], loop.time()
                    if (
                        probe["ready"] == "complete"
                        and loop.time() - changed_at >= quiet_period
                        # Re-read: evaluating the probe took time, and a request
                        # that started during it makes the reading already stale.
                        and session.in_flight == 0
                    ):
                        return
            await asyncio.sleep(min(_SETTLE_POLL_MS / 1000, remaining))

    async def _probe_settle(self, session: _Session) -> dict[str, Any] | None:
        """Read the mutation count, or ``None`` if the page cannot answer for it.

        ``None`` covers two cases that both mean "not settled": a page mid-navigation
        whose execution context went away under the evaluate, and a count that is not
        a number, which means the property this depends on is not the one the init
        script installed. Neither is treated as quiet — an unreadable page is exactly
        where a settled verdict would be least defensible.
        """
        try:
            raw = await session.page.evaluate(_SETTLE_PROBE)
        except PlaywrightError:
            return None
        if not isinstance(raw, dict) or not isinstance(raw.get("mutations"), int):
            return None
        return {"ready": raw.get("ready"), "mutations": raw["mutations"]}

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
        # The cap goes into the page. Applied only here, it bounded what Python
        # kept while the script had already described every control on the
        # document — which protected the snapshot and not the browser.
        observed = await session.page.evaluate(
            EXTRACT_ELEMENTS, {"maxElements": self._limits.max_elements_per_snapshot}
        )
        records = observed.get("records", []) if isinstance(observed, dict) else []
        failed = int(observed.get("failed", 0)) if isinstance(observed, dict) else 0
        if failed:
            # Not silently dropped. A page that will not describe some of its own
            # controls is a page this driver has not fully seen, and specification
            # 8.3 would have the compiler report what it could not read rather
            # than present a subset as the whole.
            raise ExtractionIncompleteError(
                f"{failed} element(s) on this page raised while being described; "
                "the observation is incomplete and is refused rather than returned "
                "as if it were the whole page"
            )

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

    async def page_text(self, session_id: str) -> tuple[str, str]:
        """The current address and the text a reader would see on the page.

        For checking an outcome from outside the agent — the benchmark's verifier
        — and never handed to a model. Rendered text rather than markup, so that a
        class name or a title cannot pass for content. Form values are not part of
        it: an input's value is not rendered text.
        """
        session = self._session(session_id)
        return session.page.url, await session.page.inner_text("body")

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
            outcome = await session.page.evaluate_handle(
                RESOLVE_CANDIDATE,
                {
                    "kind": int(candidate.kind),
                    "key": candidate.key,
                    "role": candidate.role,
                    "form_id": candidate.form_id,
                },
            )
            try:
                count = await (await outcome.get_property("count")).json_value()
                element = (await outcome.get_property("node")).as_element()
            finally:
                await outcome.dispose()

            if element is not None:
                handle_id = new_id("res")
                session.handles[handle_id] = element
                return handle_id
            attempts.append(f"{candidate.kind.name.lower()}={count}")

        raise ElementNotResolvedError(
            f"{described_as} is stale: no candidate matched exactly once ({', '.join(attempts)})"
        )

    async def describe_resolved(self, session_id: str, handle_id: str) -> NormalizedElement:
        """Describe the element a resolution produced, by the extractor's own rules.

        The risk classifier needs a :class:`NormalizedElement`, and the only one it
        could previously get came from matching a saved locator chain against the
        snapshot. That is a second opinion about "which element is this?", and two
        opinions can differ: on a page with a duplicated ``id`` the chain matcher
        can find one element unique while the specification 7.7 resolver rejects it
        as ambiguous and falls through to another. The safe one is then the one
        that gets classified and the other is the one that gets clicked.

        Describing the handle removes the second opinion. What is classified is
        what will be acted on, because it is the same element object.
        """
        session = self._session(session_id)
        element = self._handle(session, handle_id)
        record = await session.page.evaluate(DESCRIBE_ELEMENT, element)
        if not isinstance(record, dict):
            raise ElementNotResolvedError(f"{handle_id} could not be described")
        return normalize_element(record, new_id("el"), session.page.url)

    def release(self, session_id: str, handle_id: str) -> None:
        """Forget a resolution, disposing the browser-side handle behind it.

        Resolutions are per action, so without this the map grows for the life of
        the session and every handle keeps a DOM node alive in the browser. The
        disposal is fired and not awaited: it is a best-effort cleanup on a page
        that may already be gone, and an execution must not fail because tidying up
        did not.
        """
        session = self._sessions.get(session_id)
        if session is None:
            return
        element = session.handles.pop(handle_id, None)
        if element is not None:
            self._spawn(element.dispose())

    async def contains(self, session_id: str, container_id: str, member_id: str) -> bool:
        """Whether one resolved element encloses another.

        Specification 7.11's shared navigation capabilities name a *container* as
        their target and let an argument choose a member inside it. Without this
        check the argument would be choosing from the whole document: an
        ``entity_id`` whose record had moved out of the result list, or a page
        number that matched a link in the footer, would resolve uniquely and be
        activated, and the capability's own target would have had no say in it.
        """
        session = self._session(session_id)
        container = self._handle(session, container_id)
        member = self._handle(session, member_id)
        return bool(
            await session.page.evaluate(
                "([container, member]) => container === member || container.contains(member)",
                [container, member],
            )
        )

    def _handle(self, session: _Session, handle_id: str) -> ElementHandle:
        """Return the element a resolution produced, or say it is gone."""
        element = session.handles.get(handle_id)
        if element is None:
            raise ElementNotResolvedError(f"{handle_id} is not a resolution this session produced")
        return element

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
        handle_id = await self.resolve(session_id, action.element_id)
        return await self.execute_resolved_action(session_id, handle_id, action)

    async def execute_resolved_action(
        self,
        session_id: str,
        handle_id: str,
        action: PlannedAction,
    ) -> ActionObservation:
        """Perform one action against an element that has *already* been resolved.

        The coordinator of specification 11.1 needs this shape. Its plan comes from
        a template, which holds locator chains and no element IDs at all, so it
        re-resolves each target itself at step 3 and must then act on precisely
        what it checked. Going back through :meth:`resolve` would resolve a second
        time, and the element that came back the second time is not provably the
        one the risk gate and the containment check were applied to.
        """
        session = self._session(session_id)
        before = session.current_document_id
        element = self._handle(session, handle_id)
        timeout = self._limits.execution_timeout_ms

        succeeded = True
        error_code: str | None = None
        error_message: str | None = None
        try:
            await self._perform(element, action, timeout)
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
            settled=await self._settled_quietly(session),
            navigation_events=list(session.events),
            error_code=error_code,
            error_message=error_message,
        )

    async def _perform(self, element: ElementHandle, action: PlannedAction, timeout: int) -> None:
        """Map one allowlisted action onto Playwright (specification 10.4).

        Acts on the handle the resolution produced rather than on a selector
        re-queried at action time. Re-querying is what let the page substitute the
        target; a handle points at the element that was actually checked.
        """
        if action.action is ActionType.FOCUS:
            await element.focus()
        elif action.action is ActionType.SCROLL_INTO_VIEW:
            await element.scroll_into_view_if_needed(timeout=timeout)
        elif action.action is ActionType.SET_VALUE:
            await element.fill(action.value or "", timeout=timeout)
        elif action.action is ActionType.SELECT_OPTION:
            await element.select_option(action.option, timeout=timeout)
        elif action.action is ActionType.SET_CHECKED:
            await element.set_checked(bool(action.checked), timeout=timeout)
        elif action.action is ActionType.ACTIVATE:
            await element.click(timeout=timeout)
        else:  # pragma: no cover - ActionType is closed and fully covered above
            raise DriverNotReadyError(f"{action.action} has no driver implementation")

    async def _settled_quietly(self, session: _Session) -> bool:
        """Report whether the page settled, without turning a busy page into an error."""
        try:
            await self._wait_settled(session, self._limits.settle_timeout_ms)
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
        if urlsplit(response.url).scheme.lower() not in _NETWORK_SCHEMES:
            # Not a network response, so there is no peer to check. The
            # frame-commit check is what covers documents that arrive this way.
            return

        try:
            peer = await response.server_addr()
        except PlaywrightError as exc:
            self._fail_session(
                session,
                f"the address {response.url} was served from could not be checked: {exc}",
                kind="navigation",
            )
            return

        address = (peer or {}).get("ipAddress")
        if not address:
            # Fail closed. Specification 10.2 is a boundary, and "we could not tell"
            # is not permission to proceed — an unverifiable peer is exactly the
            # shape a rebinding answer would take if the check could be dodged.
            self._fail_session(
                session,
                f"{response.url} was served from an address that could not be determined",
                kind="navigation",
            )
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
            session.in_flight += 1
            # A capability rated low is one whose *own* act reads without changing
            # state. It says nothing about what the page does in response: an
            # `oninput` handler is free to POST, and the URL policy judges addresses
            # rather than methods, so nothing else would notice. This does not stop
            # it — blocking every non-GET would break ordinary pages, and by the
            # time the request is seen it has been made — but an unobserved
            # state-changing request is one nobody can report as not having
            # happened, and specification 16 asks for the signals behind a decision
            # to be observable.
            if request.method.upper() != "GET":
                session.events.append(
                    NavigationEvent(
                        kind="state_changing_request",
                        url=request.url,
                        blocked=False,
                        reason=f"the page issued a {request.method.upper()} request",
                    )
                )

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
