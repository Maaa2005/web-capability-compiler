"""The six MCP tools of specification 12, over stdio.

The tool surface is deliberately small and deliberately *not* a browser. There is
no click tool, no type tool, no screenshot, no coordinate, and no selector in
either direction — an agent inspects a page, reads what it may do, and runs one
of those things by name. That is the whole claim of the project, and this file is
where it either holds or does not.

Three rules keep it holding:

**Generated capabilities are data, not tools.** Specification 12 says the six
tools are stable; a capability compiled from a page is returned as a typed
description and executed through ``execute_capability``. Registering capabilities
as MCP tools would make the tool list change under the client every time it
navigated, and would invalidate its prompt cache on every page.

**Nothing internal crosses the boundary.** Every response is built from
:mod:`wcc.mcp.views`, which names its fields explicitly.

**The runtime is shared, and single-threaded per session.** One driver, one store,
one coordinator for the process; the coordinator serializes work per session.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from dataclasses import dataclass, field
from typing import Any

from mcp.server.mcpserver import MCPServer

from wcc import __version__
from wcc.config import build_enricher, driver_settings
from wcc.driver.base import DriverError
from wcc.driver.playwright_driver import PlaywrightDriver
from wcc.mcp.views import (
    CapabilityListView,
    CapabilityView,
    ClosedSessionView,
    InspectionView,
    SessionView,
    capability_view,
    inspection_view,
    session_view,
    within_risk,
)
from wcc.models.capability import CapabilityCategory, RiskLevel
from wcc.models.execution import ExecutionResult
from wcc.models.session import OpenSessionRequest
from wcc.policy.url_policy import UrlPolicy
from wcc.runtime.coordinator import Coordinator, Inspection
from wcc.store.store import Store

SERVER_NAME = "wcc"

INSTRUCTIONS = """\
Compile a web page into typed capabilities and run them.

Call open_session with a URL, then inspect_page. Inspection returns the
capabilities the page supports, each with a JSON Schema for its arguments, and
the entities the page is about. Run one with execute_capability.

There are no low-level tools here on purpose: no clicking, no typing, no
screenshots, no selectors. Anything a capability does not offer, this server
cannot do — and a capability above low risk is described but never executed.
"""


class ToolError(Exception):
    """A tool was called in a way this server will not act on.

    Raised rather than returned for the cases that are a caller mistake — an
    unknown session, an unknown execution. Execution refusals are different and
    travel inside :class:`~wcc.models.execution.ExecutionResult`, because there
    the call was well formed and the *page* was the problem.
    """


@dataclass
class Service:
    """The one runtime the tools share.

    Assembled once per process. The driver holds a browser, the store holds a
    SQLite connection, and neither is cheap enough to build per call — nor would
    it be correct to, since a session is a live browser page that later calls
    must find again.
    """

    driver: PlaywrightDriver
    store: Store
    coordinator: Coordinator
    inspections: dict[str, Inspection] = field(default_factory=dict)
    """The most recent inspection per session, so ``list_capabilities`` can answer
    without touching the page. Re-inspecting on a read would let a list call
    change what it is listing."""


def build_service(store: Store | None = None) -> Service:
    """Assemble the runtime, retiring anything a previous process left behind."""
    opened = store or Store()
    opened.expire_bindings_from_previous_runs()
    driver = PlaywrightDriver(UrlPolicy(), driver_settings())
    # The store is the enrichment cache: a page class outlives the process, and so
    # should the model call that named it.
    enricher = build_enricher(opened)
    return Service(
        driver=driver,
        store=opened,
        coordinator=Coordinator(driver, opened, enricher=enricher),
    )


def build_server(service: Service) -> MCPServer:
    """Register the six tools of specification 12 against ``service``."""

    @asynccontextmanager
    async def lifespan(_: MCPServer) -> AsyncIterator[None]:
        try:
            yield
        finally:
            await service.driver.shutdown()
            service.store.close()

    server = MCPServer(
        name=SERVER_NAME,
        version=__version__,
        instructions=INSTRUCTIONS,
        lifespan=lifespan,
    )

    @server.tool(
        name="open_session",
        description=(
            "Open a browser session on a URL and report what page it landed on. "
            "The URL policy is enforced before the session is returned, so a "
            "refused address never becomes a session."
        ),
    )
    async def open_session(url: str) -> SessionView:
        try:
            session = await service.coordinator.open_session(OpenSessionRequest(url=url))
            snapshot = await service.driver.snapshot(session.session_id)
        except DriverError as refused:
            raise ToolError(f"{refused.code}: {refused.message}") from refused
        return session_view(session, snapshot)

    @server.tool(
        name="inspect_page",
        description=(
            "Read the current page and return the capabilities it supports, the "
            "entities it is about, and anything the compiler refused. Reuses a "
            "stored template for the page class when one binds, and compiles a "
            "new one only when none does."
        ),
    )
    async def inspect_page(session_id: str, refresh: bool = True) -> InspectionView:
        # ``refresh`` is accepted because specification 12.2 defines it, and
        # honoured by not caching: an inspection is an observation, and returning
        # a remembered one for refresh=True would answer a question about now with
        # a fact about earlier. refresh=False returns the last inspection when
        # there is one, which is the only reading under which the flag does
        # anything at all.
        if not refresh and session_id in service.inspections:
            inspection = service.inspections[session_id]
        else:
            inspection = await _inspect(service, session_id)
        return inspection_view(
            inspection.snapshot,
            inspection.ir,
            _views(inspection),
            reused=inspection.reused,
            report=inspection.report,
            unbound=inspection.unbound,
        )

    @server.tool(
        name="list_capabilities",
        description=(
            "List the capabilities already bound in this session, optionally "
            "filtered by category and by a maximum risk level. Inspect the page "
            "first; this reports what that inspection found."
        ),
    )
    async def list_capabilities(
        session_id: str,
        category: CapabilityCategory | None = None,
        max_risk: RiskLevel = RiskLevel.LOW,
    ) -> CapabilityListView:
        inspection = service.inspections.get(session_id)
        if inspection is None:
            raise ToolError(
                f"{session_id} has not been inspected; call inspect_page before listing"
            )
        chosen = [
            view
            for view in _views(inspection)
            if within_risk(view, max_risk) and (category is None or view.category is category)
        ]
        return CapabilityListView(session_id=session_id, capabilities=chosen)

    @server.tool(
        name="execute_capability",
        description=(
            "Run one bound capability and return the complete result: the steps "
            "that ran, what changed on the page, whether the intended effect was "
            "verified, and the entities the resulting page is about. Only low-risk "
            "capabilities execute; anything else is refused with a reason."
        ),
    )
    async def execute_capability(
        session_id: str,
        capability_id: str,
        arguments: dict[str, Any] | None = None,
    ) -> ExecutionResult:
        return await service.coordinator.execute(session_id, capability_id, arguments or {})

    @server.tool(
        name="get_execution_result",
        description=(
            "Retrieve a stored execution result. Only the session that produced "
            "the execution can read it."
        ),
    )
    async def get_execution_result(session_id: str, execution_id: str) -> ExecutionResult:
        result = service.coordinator.execution_result(session_id, execution_id)
        if result is None:
            raise ToolError(f"{execution_id} is not an execution of {session_id}")
        return result

    @server.tool(
        name="close_session",
        description=(
            "Close a session and retire its capabilities. Closing an already "
            "closed session is not an error."
        ),
    )
    async def close_session(session_id: str) -> ClosedSessionView:
        known = session_id in service.inspections or service.coordinator.registry.is_live(
            session_id
        )
        service.inspections.pop(session_id, None)
        try:
            await service.coordinator.close_session(session_id)
        except DriverError:
            # Idempotent by specification 12.6. A session the driver no longer
            # holds is a session that is closed, which is the state the caller
            # asked for; reporting a failure would make a correct retry look wrong.
            return ClosedSessionView(session_id=session_id, status="closed", already_closed=True)
        return ClosedSessionView(session_id=session_id, status="closed", already_closed=not known)

    return server


async def _inspect(service: Service, session_id: str) -> Inspection:
    try:
        inspection = await service.coordinator.inspect_page(session_id)
    except DriverError as failed:
        raise ToolError(f"{failed.code}: {failed.message}") from failed
    service.inspections[session_id] = inspection
    return inspection


def _views(inspection: Inspection) -> list[CapabilityView]:
    return [
        capability_view(binding, inspection.templates[binding.template_id])
        for binding in inspection.capabilities
    ]


def serve() -> int:
    """Run the stdio server until the client disconnects."""
    service = build_service()
    build_server(service).run("stdio")
    return 0
