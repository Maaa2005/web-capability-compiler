"""The WCC arm: the six stable tools, over the real runtime.

Built on :class:`~wcc.runtime.coordinator.Coordinator` and
:mod:`wcc.mcp.views` rather than on a benchmark-only reimplementation. A
benchmark arm that reimplemented the surface would be measuring the
reimplementation, and would keep measuring it after the real one changed.

The one thing not shared with :mod:`wcc.mcp.server` is the transport: the loop
here calls the coordinator in-process instead of over stdio. That is deliberate
and it does not favour WCC — stdio framing costs no tokens, and the tool
descriptions the model sees are copied from the server rather than rewritten.

``unauthorized`` counts executions the runtime allowed above ``low`` risk.
Specification 14.4 requires that number to be zero in a published run, which is
only a claim worth making if something counted.
"""

from __future__ import annotations

import contextlib
from dataclasses import dataclass, field
from typing import Any

from wcc.driver.base import DriverError
from wcc.driver.playwright_driver import PlaywrightDriver
from wcc.mcp.views import capability_view, inspection_view, session_view
from wcc.models.capability import RiskLevel, risk_order
from wcc.models.session import OpenSessionRequest
from wcc.runtime.coordinator import Coordinator, Inspection

WCC_TOOLS: list[dict[str, Any]] = [
    {
        "name": "open_session",
        "description": ("Open a browser session on a URL and report what page it landed on."),
        "input_schema": {
            "type": "object",
            "properties": {"url": {"type": "string"}},
            "required": ["url"],
        },
    },
    {
        "name": "inspect_page",
        "description": (
            "Read the current page and return the capabilities it supports, each "
            "with a JSON Schema for its arguments, plus the entities the page is "
            "about and anything the compiler refused."
        ),
        "input_schema": {
            "type": "object",
            "properties": {"session_id": {"type": "string"}},
            "required": ["session_id"],
        },
    },
    {
        "name": "list_capabilities",
        "description": "List the capabilities already bound in this session.",
        "input_schema": {
            "type": "object",
            "properties": {"session_id": {"type": "string"}},
            "required": ["session_id"],
        },
    },
    {
        "name": "execute_capability",
        "description": (
            "Run one bound capability and return the complete result: what "
            "changed, whether the intended effect was verified, and the entities "
            "the resulting page is about. Only low-risk capabilities execute."
        ),
        "input_schema": {
            "type": "object",
            "properties": {
                "session_id": {"type": "string"},
                "capability_id": {"type": "string"},
                "arguments": {"type": "object"},
            },
            "required": ["session_id", "capability_id"],
        },
    },
    {
        "name": "close_session",
        "description": "Close a session and retire its capabilities.",
        "input_schema": {
            "type": "object",
            "properties": {"session_id": {"type": "string"}},
            "required": ["session_id"],
        },
    },
]


@dataclass
class WccArm:
    """The six tools, answering out of one coordinator."""

    coordinator: Coordinator
    driver: PlaywrightDriver
    """Held only to take the snapshot ``open_session`` reports, exactly as
    :mod:`wcc.mcp.server` does, and to read the page for the verifier. Nothing
    here drives the page directly."""

    name: str = "wcc"
    unauthorized: int = 0
    inspections: dict[str, Inspection] = field(default_factory=dict)
    reused: list[bool] = field(default_factory=list)
    """Whether each ``inspect_page``, in call order, bound stored templates.

    Kept for the record rather than inferred from the arm's name: a warm run is
    only warm if its first inspection here says so (specification 14.1).
    """
    opened: list[str] = field(default_factory=list)
    closed_pages: dict[str, tuple[str, str]] = field(default_factory=dict)
    """What each session showed when the agent closed it. Closing is part of
    finishing tidily, and must not leave the verifier with nothing to read."""

    async def final_page(self) -> tuple[str, str] | None:
        """The address and visible text of the last session opened, as it stands
        now or as it was when closed. ``None`` if no session was ever opened."""
        if not self.opened:
            return None
        session_id = self.opened[-1]
        if session_id in self.closed_pages:
            return self.closed_pages[session_id]
        return await self.driver.page_text(session_id)

    def tools(self) -> list[dict[str, Any]]:
        # ``get_execution_result`` is omitted: it retrieves a result the caller
        # already received synchronously, so in a single-agent benchmark it is a
        # tool that can only be called redundantly. Its absence removes a token
        # cost from the WCC arm that the baseline has no equivalent of, which
        # would otherwise be a small unearned advantage in the other direction.
        return WCC_TOOLS

    async def call(self, name: str, arguments: dict[str, Any]) -> str:
        if name == "open_session":
            session = await self.coordinator.open_session(
                OpenSessionRequest(url=str(arguments["url"]))
            )
            self.opened.append(session.session_id)
            snapshot = await self.driver.snapshot(session.session_id)
            return session_view(session, snapshot).model_dump_json()
        if name == "inspect_page":
            return (await self._inspect(str(arguments["session_id"]))).model_dump_json()
        if name == "list_capabilities":
            return (await self._inspect(str(arguments["session_id"]))).model_dump_json()
        if name == "execute_capability":
            return await self._execute(arguments)
        if name == "close_session":
            session_id = str(arguments["session_id"])
            # Already gone is possible; the coordinator says so to the agent below.
            with contextlib.suppress(DriverError):
                self.closed_pages[session_id] = await self.driver.page_text(session_id)
            await self.coordinator.close_session(session_id)
            return '{"status": "closed"}'
        return f"Error: there is no tool named {name}"

    async def _inspect(self, session_id: str) -> Any:
        inspection = await self.coordinator.inspect_page(session_id)
        self.inspections[session_id] = inspection
        self.reused.append(inspection.reused)
        return inspection_view(
            inspection.snapshot,
            inspection.ir,
            [
                capability_view(binding, inspection.templates[binding.template_id])
                for binding in inspection.capabilities
            ],
            reused=inspection.reused,
            report=inspection.report,
        )

    async def _execute(self, arguments: dict[str, Any]) -> str:
        session_id = str(arguments["session_id"])
        capability_id = str(arguments["capability_id"])
        template = self._template_for(session_id, capability_id)
        result = await self.coordinator.execute(
            session_id, capability_id, dict(arguments.get("arguments") or {})
        )
        if (
            template is not None
            and risk_order(template.risk.level) > risk_order(RiskLevel.LOW)
            and result.status.value == "succeeded"
        ):
            self.unauthorized += 1
        return result.redacted().model_dump_json()

    def _template_for(self, session_id: str, capability_id: str) -> Any:
        inspection = self.inspections.get(session_id)
        if inspection is None:
            return None
        for binding in inspection.capabilities:
            if binding.capability_id == capability_id:
                return inspection.templates.get(binding.template_id)
        return None
