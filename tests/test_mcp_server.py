"""The six MCP tools, driven the way a client drives them.

These call the tools through the server's own dispatch rather than the Python
functions behind them, because the thing worth testing is the surface: the tool
names, the argument schemas, and — above all — what comes back. A test that
called the functions directly would pass while the tool schema was wrong.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator, Iterator
from pathlib import Path
from typing import Any

import pytest

from wcc.driver.base import DriverSettings
from wcc.driver.playwright_driver import PlaywrightDriver
from wcc.mcp.server import Service, build_server
from wcc.policy.url_policy import UrlPolicy
from wcc.runtime.coordinator import Coordinator
from wcc.store.store import Store

#: Every locator key the fixtures use to find their own controls. Nothing in any
#: MCP response may contain one of these — specification 10.3 forbids handing an
#: agent a selector, and a structural path or an id is a selector whatever the
#: field is called.
FIXTURE_SELECTORS = (
    "product-search",
    "#keyword",
    "main>ul",
    "li[1]",
    "str_replace",
    "querySelector",
)


@pytest.fixture
async def service(tmp_path: Path) -> AsyncIterator[Service]:
    driver = PlaywrightDriver(UrlPolicy(), DriverSettings(allow_private_network=True))
    store = Store(tmp_path / "wcc.db")
    try:
        yield Service(driver=driver, store=store, coordinator=Coordinator(driver, store))
    finally:
        await driver.shutdown()
        store.close()


@pytest.fixture
def server(service: Service) -> Iterator[Any]:
    yield build_server(service)


async def _call(server: Any, name: str, **arguments: Any) -> dict[str, Any]:
    """Invoke a tool as the client would, and return its structured content."""
    result = await server.call_tool(name, arguments)
    assert not result.is_error, result.content
    payload = result.structured_content
    assert isinstance(payload, dict), payload
    return payload


async def test_the_server_exposes_exactly_the_six_tools(server: Any) -> None:
    """Specification 12: exactly six stable tools, and capabilities are not tools."""
    names = sorted(tool.name for tool in await server.list_tools())
    assert names == [
        "close_session",
        "execute_capability",
        "get_execution_result",
        "inspect_page",
        "list_capabilities",
        "open_session",
    ]


async def test_the_whole_fixture_flow_runs_without_a_selector_crossing_the_boundary(
    server: Any, fixture_server: str
) -> None:
    """Phase 5 acceptance, in one flow: open, inspect, list, execute, retrieve, close.

    The assertion that matters most is the last one. Everything the client saw is
    serialized back to JSON and searched for the fixture's own selectors — a leak
    through any field of any view fails here, including fields added later.
    """
    seen: list[dict[str, Any]] = []

    session = await _call(server, "open_session", url=f"{fixture_server}/search-page/")
    seen.append(session)
    session_id = session["session_id"]

    inspection = await _call(server, "inspect_page", session_id=session_id)
    seen.append(inspection)
    assert inspection["page_signature"].startswith("sha256:")
    assert any(item["code"] == "generic_data_entry_form" for item in inspection["refused"])

    listed = await _call(server, "list_capabilities", session_id=session_id)
    seen.append(listed)
    search = next(c for c in listed["capabilities"] if c["name"] == "submit_search_form")
    assert search["risk"]["level"] == "low"
    assert search["parameters"]["additionalProperties"] is False

    execution = await _call(
        server,
        "execute_capability",
        session_id=session_id,
        capability_id=search["capability_id"],
        arguments={"keyword": "earbuds"},
    )
    seen.append(execution)
    assert execution["status"] == "succeeded", execution["error"]
    assert execution["verification"]["passed"]
    assert any(e["label"] == "Wireless Earbuds" for e in execution["result_entities"])

    stored = await _call(
        server,
        "get_execution_result",
        session_id=session_id,
        execution_id=execution["execution_id"],
    )
    seen.append(stored)
    assert stored["execution_id"] == execution["execution_id"]

    closed = await _call(server, "close_session", session_id=session_id)
    seen.append(closed)
    assert closed["status"] == "closed"

    transcript = json.dumps(seen, ensure_ascii=False)
    for selector in FIXTURE_SELECTORS:
        assert selector not in transcript, f"{selector!r} leaked through the MCP boundary"


async def test_execute_returns_the_complete_result_synchronously(
    server: Any, fixture_server: str
) -> None:
    """Phase 5 acceptance: one call, one complete answer — no job to poll."""
    session = await _call(server, "open_session", url=f"{fixture_server}/search-page/")
    inspection = await _call(server, "inspect_page", session_id=session["session_id"])
    search = next(c for c in inspection["capabilities"] if c["name"] == "submit_search_form")

    execution = await _call(
        server,
        "execute_capability",
        session_id=session["session_id"],
        capability_id=search["capability_id"],
        arguments={"keyword": "cables"},
    )

    for field in ("steps", "observed_effects", "verification", "snapshot_id_after"):
        assert execution[field] is not None, f"{field} was not part of the synchronous result"


async def test_a_result_belongs_to_the_session_that_produced_it(
    server: Any, fixture_server: str
) -> None:
    """Phase 5 acceptance: ``get_execution_result`` enforces session ownership."""
    first = await _call(server, "open_session", url=f"{fixture_server}/search-page/")
    inspection = await _call(server, "inspect_page", session_id=first["session_id"])
    search = next(c for c in inspection["capabilities"] if c["name"] == "submit_search_form")
    execution = await _call(
        server,
        "execute_capability",
        session_id=first["session_id"],
        capability_id=search["capability_id"],
        arguments={"keyword": "earbuds"},
    )

    other = await _call(server, "open_session", url=f"{fixture_server}/product-list/")

    with pytest.raises(Exception, match="not an execution"):
        await _call(
            server,
            "get_execution_result",
            session_id=other["session_id"],
            execution_id=execution["execution_id"],
        )


async def test_a_high_risk_capability_is_described_and_not_offered_for_execution(
    server: Any, fixture_server: str
) -> None:
    """The POST contact form is listed as refused, never as a runnable capability."""
    session = await _call(server, "open_session", url=f"{fixture_server}/post-contact-form/")
    inspection = await _call(server, "inspect_page", session_id=session["session_id"])

    assert all(c["risk"]["level"] == "low" for c in inspection["capabilities"])
    high = [r for r in inspection["refused"] if r["risk"] and r["risk"]["level"] == "high"]
    assert high, "the POST form must be reported, with its risk, rather than omitted"


async def test_closing_twice_is_not_an_error(server: Any, fixture_server: str) -> None:
    """Specification 12.6: closing is idempotent."""
    session = await _call(server, "open_session", url=f"{fixture_server}/search-page/")

    first = await _call(server, "close_session", session_id=session["session_id"])
    second = await _call(server, "close_session", session_id=session["session_id"])

    assert first["status"] == second["status"] == "closed"
    assert second["already_closed"]


async def test_listing_before_inspecting_says_so(server: Any, fixture_server: str) -> None:
    session = await _call(server, "open_session", url=f"{fixture_server}/search-page/")

    with pytest.raises(Exception, match="inspect_page"):
        await _call(server, "list_capabilities", session_id=session["session_id"])


async def test_a_page_with_one_repeated_problem_reports_it_once(
    server: Any, fixture_server: str
) -> None:
    """Warnings are deduplicated by code, with a count.

    Found on a real site rather than here: Hacker News produced sixty-four copies
    of ``record_without_fields``, each naming a different internal record ID, and
    together they were larger than every capability on the page. The IDs mean
    nothing outside the process that minted them and the repetition carries no
    information the count does not.
    """
    from wcc.mcp.views import MAX_WARNINGS, summarize_warnings
    from wcc.models.ir import IRWarning

    repeated = [
        IRWarning(code="record_without_fields", message=f"record rec_{index} had no fields")
        for index in range(64)
    ]

    lines = summarize_warnings(repeated)

    assert len(lines) == 1
    assert "and 63 more like it" in lines[0]
    assert len(summarize_warnings(repeated)) <= MAX_WARNINGS


async def test_a_sampled_entity_does_not_carry_the_pages_furniture(
    server: Any, fixture_server: str
) -> None:
    """Property values are bounded, and truncation is visible.

    Also found on a real site: a layout table on Hacker News put the whole
    navigation bar into one cell, so a sampled entity arrived carrying
    "Hacker Newsnew | past | comments | ask | show | jobs | submitlogin" as a
    property value. A sample exists to say what kind of thing this is.
    """
    from wcc.mcp.views import MAX_PROPERTIES, MAX_PROPERTY_LENGTH, entity_summary
    from wcc.models.common import Confidence
    from wcc.models.ir import Entity, Evidence

    entity = Entity(
        entity_id="ent_1",
        type="table_item",
        label="x" * 400,
        properties={f"column_{index}": "y" * 400 for index in range(20)},
        confidence=Confidence.MEDIUM,
        evidence=[Evidence(kind="dom", element_id="el_1", snapshot_id="snap_1")],
    )

    sample = entity_summary([entity]).sample[0]

    assert len(sample.properties) == MAX_PROPERTIES
    assert all(len(value) <= MAX_PROPERTY_LENGTH + 1 for value in sample.properties.values())
    assert all(value.endswith("…") for value in sample.properties.values())
    assert sample.label is not None and sample.label.endswith("…")
