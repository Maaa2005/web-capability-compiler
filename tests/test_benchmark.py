"""The benchmark harness, driven without spending a token.

The loop, both arms, the metrics, and the verification all run here against a
scripted model client. That is the whole reason
:class:`~benchmarks.agent_loop.ModelClient` is a Protocol: a benchmark whose
correctness can only be checked by paying for it is a benchmark whose bugs are
discovered in the published numbers.

Both arms are driven against the real fixture site in a real browser, because the
part most likely to be wrong is not the arithmetic — it is whether the tools
actually work.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from typing import Any

import pytest
from benchmarks.agent_loop import ModelReply, ToolRequest, run_loop
from benchmarks.arms import BaselineArm
from benchmarks.metrics import Run, Usage, comparison_table, summarize, write_runs
from benchmarks.tasks import TASKS
from playwright.async_api import Page, async_playwright


class ScriptedClient:
    """Replays a fixed sequence of replies, recording what it was asked."""

    model = "scripted"

    def __init__(self, replies: list[ModelReply]) -> None:
        self._replies = list(replies)
        self.tool_lists: list[list[str]] = []
        self.calls = 0

    async def respond(
        self, system: str, messages: list[dict[str, Any]], tools: list[dict[str, Any]]
    ) -> ModelReply:
        self.calls += 1
        self.tool_lists.append([tool["name"] for tool in tools])
        if not self._replies:
            return _done()
        return self._replies.pop(0)


def _done(text: str = "DONE") -> ModelReply:
    return ModelReply(
        text=text,
        tool_requests=[],
        stop_reason="end_turn",
        input_tokens=10,
        output_tokens=2,
        raw_content=[{"type": "text", "text": text}],
    )


def _uses(name: str, **arguments: Any) -> ModelReply:
    return ModelReply(
        text="",
        tool_requests=[ToolRequest(call_id=f"call_{name}", name=name, arguments=arguments)],
        stop_reason="tool_use",
        input_tokens=100,
        output_tokens=20,
        raw_content=[{"type": "tool_use", "id": f"call_{name}", "name": name, "input": arguments}],
    )


@pytest.fixture
async def page(fixture_server: str) -> AsyncIterator[Page]:
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        try:
            yield await (await browser.new_context()).new_page()
        finally:
            await browser.close()


# -- the loop -------------------------------------------------------------


async def test_the_loop_stops_when_the_model_stops_asking_for_tools(page: Page) -> None:
    client = ScriptedClient([_uses("browser_snapshot"), _done()])
    arm = BaselineArm(page=page)
    await arm.call("browser_navigate", {"url": "about:blank"})

    result = await run_loop(client, arm, "do the thing")

    assert result.tool_calls == 1
    assert result.turn_count == 2
    assert result.final_text == "DONE"
    assert result.stopped_because == "end_turn"


async def test_a_tool_that_raises_is_handed_back_rather_than_ending_the_run(
    page: Page,
) -> None:
    """A failing tool is an ordinary event for an agent, not a crash for the harness."""
    client = ScriptedClient([_uses("browser_click", ref="nonexistent"), _done()])
    arm = BaselineArm(page=page)
    await arm.call("browser_navigate", {"url": "about:blank"})

    result = await run_loop(client, arm, "click something that is not there")

    assert result.tool_calls == 1
    assert result.final_text == "DONE"


async def test_a_turn_without_usage_metadata_makes_the_run_unreportable() -> None:
    """Specification 14.3: tokens come from the API or the run does not count."""
    unmetered = ModelReply(
        text="DONE",
        tool_requests=[],
        stop_reason="end_turn",
        input_tokens=None,
        output_tokens=None,
        raw_content=[],
    )

    class Silent(ScriptedClient):
        pass

    result = await run_loop(Silent([unmetered]), _NoTools(), "anything")

    assert not result.usage.reported


class _NoTools:
    name = "none"

    def tools(self) -> list[dict[str, Any]]:
        return []

    async def call(self, name: str, arguments: dict[str, Any]) -> str:
        raise AssertionError("no tool should be called")


# -- the baseline arm -----------------------------------------------------


async def test_the_baseline_completes_the_task_through_low_level_tools(
    page: Page, fixture_server: str
) -> None:
    """The arm has to actually work, or the comparison measures a broken opponent."""
    arm = BaselineArm(page=page)
    await arm.call("browser_navigate", {"url": f"{fixture_server}/search-page/"})

    snapshot = await arm.call("browser_snapshot", {})
    assert "Interactive elements:" in snapshot
    assert "Search products" in snapshot

    keyword = next(
        line.split(":")[0].strip() for line in snapshot.splitlines() if "Search products" in line
    )
    await arm.call("browser_type", {"ref": keyword, "text": "earbuds"})
    button = next(
        line.split(":")[0].strip() for line in snapshot.splitlines() if "'Search'" in line
    )
    await arm.call("browser_click", {"ref": button})

    assert "keyword=earbuds" in page.url


async def test_a_stale_ref_is_reported_rather_than_clicking_something_else(
    page: Page, fixture_server: str
) -> None:
    """The baseline's contract: refs die with the snapshot that made them."""
    arm = BaselineArm(page=page)
    await arm.call("browser_navigate", {"url": f"{fixture_server}/search-page/"})

    answer = await arm.call("browser_click", {"ref": "e1"})

    assert "browser_snapshot" in answer


# -- metrics --------------------------------------------------------------


def _run(arm: str, tokens: int, *, succeeded: bool = True, reported: bool = True) -> Run:
    return Run(
        task_id="search_products",
        arm=arm,
        attempt=1,
        model="scripted",
        started_at="2026-08-11T00:00:00+00:00",
        finished_at="2026-08-11T00:00:10+00:00",
        duration_seconds=10.0,
        tool_calls=4,
        turns=5,
        succeeded=succeeded,
        input_value="wireless earbuds",
        usage=Usage(input_tokens=tokens, output_tokens=0, reported=reported),
    )


def test_a_failed_run_is_never_credited_with_the_tokens_it_saved() -> None:
    """Specification 14.4, and the reason it exists: giving up is cheap."""
    runs = [
        _run("baseline", 10_000),
        _run("baseline", 12_000),
        _run("baseline", 50, succeeded=False),
    ]

    summary = summarize(runs, "baseline")

    assert summary.runs == 3
    assert summary.successes == 2
    assert summary.median_total_tokens == 11_000


def test_a_run_without_provider_usage_is_excluded_rather_than_counted_as_zero() -> None:
    runs = [_run("wcc_cold", 0, reported=False), _run("wcc_cold", 3_000)]

    assert summarize(runs, "wcc_cold").median_total_tokens == 3_000


def test_an_arm_with_nothing_countable_says_so_instead_of_reporting_a_number() -> None:
    summary = summarize([_run("wcc_warm", 100, succeeded=False)], "wcc_warm")

    assert not summary.reportable
    assert "not reported" in comparison_table([_run("wcc_warm", 100, succeeded=False)])


def test_the_table_reports_the_arms_independently() -> None:
    """No headline ratio: 14.4 reports arms separately, and a ratio travels alone."""
    table = comparison_table([_run("baseline", 10_000), _run("wcc_warm", 2_000)])

    assert "baseline" in table and "wcc_warm" in table
    assert "%" not in table


def test_every_raw_run_is_written_including_the_failures(tmp_path: Any) -> None:
    """Specification 14.4 publishes raw logs, which is what makes a claim checkable."""
    import json

    path = write_runs(tmp_path, [_run("baseline", 10_000), _run("baseline", 5, succeeded=False)])
    written = json.loads(path.read_text())

    assert len(written) == 2
    assert [record["succeeded"] for record in written] == [True, False]
    assert written[0]["usage"]["total_tokens"] == 10_000
    assert written[0]["model"] == "scripted"


# -- tasks ----------------------------------------------------------------


def test_cold_and_warm_use_different_inputs() -> None:
    """Specification 14.2: same value twice would credit HTTP caching to the warm arm."""
    for task in TASKS:
        assert task.cold_input != task.warm_input
        assert task.input_for("wcc_cold") == task.cold_input
        assert task.input_for("wcc_warm") == task.warm_input


def test_both_arms_are_asked_the_same_thing() -> None:
    """14.2 permits differing tool descriptions and nothing else."""
    for task in TASKS:
        assert task.prompt("x") == task.instruction.format(input="x")
