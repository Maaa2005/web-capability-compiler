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


# -- the claim, as a test -------------------------------------------------


async def test_wcc_costs_less_than_a_snapshot_on_a_realistic_page(
    fixture_server: str, tmp_path: Any
) -> None:
    """The project's central claim, asserted rather than believed.

    This test exists because of how the defect it guards was found. Every one of
    the 553 tests around it checked that something *works*; not one checked what
    it *costs*, so ``inspect_page`` could return every entity on a 150-product
    page — making WCC nearly three times more expensive than the baseline on
    exactly the page it exists to be cheaper on — with the suite entirely green.

    A project whose claim is "cheaper" needs the cheapness under test. Functional
    tests fail when the function breaks; they do not fail when the argument does.
    """
    from benchmarks.measure_payloads import measure

    result = await measure(fixture_server, "large-catalog", "/large-catalog/", "150 products")

    assert result.ratio < 0.5, (
        f"a WCC inspection of a 150-product page cost {result.wcc_chars:,} characters "
        f"against a baseline snapshot's {result.baseline_chars:,} ({result.ratio:.2f}×). "
        "The claim of this project is that it is cheaper on pages like this one."
    )


async def test_the_cost_of_an_inspection_tracks_controls_not_page_size(
    fixture_server: str,
) -> None:
    """Why the ratio moves: the two costs are proportional to different things.

    A snapshot carries everything on the page, so it grows with the page. An
    inspection carries the capabilities the page offers, so it grows with the
    controls. The 150-product catalog has the same handful of controls as the
    small search page and thirty times the content, and the numbers should show
    exactly that asymmetry.
    """
    from benchmarks.measure_payloads import measure

    small = await measure(fixture_server, "search-page", "/search-page/", "6 controls")
    large = await measure(fixture_server, "large-catalog", "/large-catalog/", "150 products")

    assert large.baseline_chars > small.baseline_chars * 10, "the page did grow"
    assert large.wcc_chars < small.wcc_chars * 3, (
        "an inspection grew with the content it should be summarizing"
    )


# -- counting tokens exactly ----------------------------------------------


def test_a_measurement_without_a_key_reports_characters_and_says_so() -> None:
    """The absence of a key must not look like the absence of a result.

    Specification 14.3 forbids estimating tokens, so a keyless run reports
    characters — labelled as characters — rather than dividing by four and
    calling the answer a token count. The unit travels with the number, into the
    printed table and into the JSON, so a figure cannot be quoted as tokens later
    by someone reading only the file.
    """
    from benchmarks.measure_payloads import Measurement

    characters = Measurement(
        page="large-catalog", description="150 products", baseline_chars=15234, wcc_chars=2527
    )

    assert characters.unit == "characters"
    assert characters.ratio == pytest.approx(2527 / 15234)


def test_a_measurement_with_counted_tokens_uses_them_for_the_ratio() -> None:
    """When the provider has counted, the provider's numbers decide."""
    from benchmarks.measure_payloads import Measurement

    counted = Measurement(
        page="large-catalog",
        description="150 products",
        baseline_chars=15234,
        wcc_chars=2527,
        baseline_tokens=3400,
        wcc_tokens=700,
    )

    assert counted.unit == "tokens"
    assert counted.ratio == pytest.approx(700 / 3400)


def test_the_token_counter_refuses_rather_than_guessing_without_a_key(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A missing key is reported as a missing key, not as a zero.

    The environment is cleared explicitly. Without that this test passes on a
    machine with no key and fails on the maintainer\'s — a test whose result
    depends on the shell it runs in is not testing the code.

    The message says the endpoint is free, because the reason someone would not
    have set a key here is usually the assumption that counting costs money.
    """
    from benchmarks.tokens import TokenCounter, TokenCounterUnavailable

    monkeypatch.delenv("ANTHROPIC_API_KEY", raising=False)

    with pytest.raises(TokenCounterUnavailable, match="free"):
        TokenCounter("claude-haiku-4-5")
