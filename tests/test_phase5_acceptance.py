"""Phase 5 acceptance: an external MCP client, over a real stdio transport.

Everything else in the suite runs inside the same process as the runtime. This
does not. It launches ``wcc serve`` as a subprocess, speaks MCP to it over stdin
and stdout with the official client, and completes the fixture flow — which is
the only arrangement that can show what the phase actually claims: that someone
else's agent, in someone else's process, can use this.

The subprocess takes its data directory and its private-network allowance from
the environment, which is the point of those being environment settings. Neither
can be set by an agent over the wire.
"""

from __future__ import annotations

import json
import os
import sys
from collections.abc import AsyncIterator
from contextlib import asynccontextmanager
from pathlib import Path
from typing import Any

from mcp import ClientSession, StdioServerParameters
from mcp.client.stdio import stdio_client

REPO_ROOT = Path(__file__).resolve().parent.parent

#: The tools a browser-driving agent would have and this server deliberately does
#: not. Their absence is the phase's central claim, so it is asserted rather than
#: assumed.
FORBIDDEN_TOOLS = ("click", "type", "screenshot", "press", "goto", "evaluate", "select")

EXPECTED_TOOLS = [
    "close_session",
    "execute_capability",
    "get_execution_result",
    "inspect_page",
    "list_capabilities",
    "open_session",
]


@asynccontextmanager
async def mcp_client(tmp_path: Path) -> AsyncIterator[ClientSession]:
    """Speak MCP to a real ``wcc serve`` subprocess.

    A context manager used inside each test rather than a pytest fixture. The
    stdio client is built on anyio task groups, and a generator fixture is
    finalized in a different task than the one that entered it — which anyio
    refuses at teardown, turning four passing tests into four teardown errors.
    Entering and exiting in the test body keeps both in the same task.
    """
    environment = dict(os.environ)
    environment["WCC_DATA_DIR"] = str(tmp_path)
    environment["WCC_ALLOW_PRIVATE_NETWORK"] = "1"
    environment["PYTHONPATH"] = str(REPO_ROOT / "src")

    parameters = StdioServerParameters(
        command=sys.executable,
        args=["-m", "wcc.main", "serve"],
        env=environment,
        cwd=str(REPO_ROOT),
    )
    async with stdio_client(parameters) as (read, write), ClientSession(read, write) as session:
        await session.initialize()
        yield session


def _text(result: Any) -> str:
    """The human-readable half of a tool result.

    Content blocks are a union — text, image, audio, resource — so the text is
    collected rather than indexed out of position zero.
    """
    return " ".join(block.text for block in result.content if block.type == "text")


def _content(result: Any) -> dict[str, Any]:
    assert not result.is_error, result.content
    if result.structured_content is not None:
        return dict(result.structured_content)
    return dict(json.loads(_text(result)))


async def _search_capability(client: ClientSession, base: str) -> tuple[str, str]:
    """Open the search fixture, inspect it, and return the session and capability."""
    session = _content(await client.call_tool("open_session", {"url": f"{base}/search-page/"}))
    inspection = _content(
        await client.call_tool("inspect_page", {"session_id": session["session_id"]})
    )
    search = next(c for c in inspection["capabilities"] if c["name"] == "submit_search_form")
    return session["session_id"], search["capability_id"]


async def test_an_external_client_completes_the_flow_with_no_low_level_tools(
    tmp_path: Path, fixture_server: str
) -> None:
    """Acceptance 1: the fixture flow, without selectors, screenshots, or coordinates."""
    async with mcp_client(tmp_path) as client:
        listed = await client.list_tools()
        names = sorted(tool.name for tool in listed.tools)
        assert names == EXPECTED_TOOLS
        for forbidden in FORBIDDEN_TOOLS:
            assert not any(forbidden in name for name in names), f"{forbidden} is exposed"

        session_id, capability_id = await _search_capability(client, fixture_server)
        execution = _content(
            await client.call_tool(
                "execute_capability",
                {
                    "session_id": session_id,
                    "capability_id": capability_id,
                    "arguments": {"keyword": "earbuds"},
                },
            )
        )

        assert execution["status"] == "succeeded", execution["error"]
        labels = {entity["label"] for entity in execution["result_entities"]}
        assert "Wireless Earbuds" in labels, "the flow must return structured results"

        closed = _content(await client.call_tool("close_session", {"session_id": session_id}))
        assert closed["status"] == "closed"


async def test_execute_capability_answers_completely_in_one_call(
    tmp_path: Path, fixture_server: str
) -> None:
    """Acceptance 2 and 3: the complete result arrives synchronously, and is owned."""
    async with mcp_client(tmp_path) as client:
        session_id, capability_id = await _search_capability(client, fixture_server)

        execution = _content(
            await client.call_tool(
                "execute_capability",
                {
                    "session_id": session_id,
                    "capability_id": capability_id,
                    "arguments": {"keyword": "cables"},
                },
            )
        )

        assert execution["verification"] is not None
        assert execution["snapshot_id_after"] is not None
        assert execution["steps"], "the steps that ran are part of the answer"

        stored = _content(
            await client.call_tool(
                "get_execution_result",
                {"session_id": session_id, "execution_id": execution["execution_id"]},
            )
        )
        assert stored["execution_id"] == execution["execution_id"]

        other = _content(
            await client.call_tool("open_session", {"url": f"{fixture_server}/product-list/"})
        )
        refused = await client.call_tool(
            "get_execution_result",
            {"session_id": other["session_id"], "execution_id": execution["execution_id"]},
        )
        assert refused.is_error, "another session must not read this execution"


async def test_a_second_session_reuses_the_first_session_s_templates(
    tmp_path: Path, fixture_server: str
) -> None:
    """The warm path, across two sessions of one server process and one database."""
    async with mcp_client(tmp_path) as client:
        first = _content(
            await client.call_tool("open_session", {"url": f"{fixture_server}/search-page/"})
        )
        cold = _content(await client.call_tool("inspect_page", {"session_id": first["session_id"]}))
        assert not cold["reused_templates"]
        await client.call_tool("close_session", {"session_id": first["session_id"]})

        second = _content(
            await client.call_tool("open_session", {"url": f"{fixture_server}/search-page/"})
        )
        warm = _content(
            await client.call_tool("inspect_page", {"session_id": second["session_id"]})
        )

        assert warm["reused_templates"]
        assert warm["refused"] == [], "a warm inspection compiles nothing, so refuses nothing"
        assert {c["name"] for c in warm["capabilities"]} == {
            c["name"] for c in cold["capabilities"]
        }


async def test_the_server_refuses_an_address_the_policy_blocks(tmp_path: Path) -> None:
    """The SSRF boundary of specification 10.2 holds across the MCP surface too."""
    async with mcp_client(tmp_path) as client:
        result = await client.call_tool(
            "open_session", {"url": "http://169.254.169.254/latest/meta-data/"}
        )

        assert result.is_error
        assert "blocked" in _text(result).lower()
