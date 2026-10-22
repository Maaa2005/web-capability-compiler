"""The subscription path, driven without signing in to anything.

:mod:`benchmarks.subscription` hands the arms to Claude Code and reads the usage
back. Both halves can be wrong in ways that flatter one arm: a tool definition
that arrives changed, a result the model was shown that the arm did not return,
cached input left out of a total, a second model counted under the first. Each of
those is checked here with Claude Code replaced by a scripted stream of the
messages it sends, so the checks cost nothing and run on every change.
"""

from __future__ import annotations

import json
from collections.abc import AsyncIterator
from pathlib import Path
from typing import Any, cast

import pytest
from benchmarks import run as runner
from benchmarks import subscription
from benchmarks.agent_loop import MAX_TURNS, SYSTEM_PROMPT
from benchmarks.arms import BaselineArm
from benchmarks.metrics import Run, Usage
from benchmarks.subscription import (
    IsolationError,
    ProviderError,
    SubscriptionAgent,
    check_init,
    mcp_tools,
    usage_from,
)
from benchmarks.tasks import TASKS
from benchmarks.wcc_arm import WccArm
from claude_agent_sdk import (
    AssistantMessage,
    ResultError,
    ResultMessage,
    StreamEvent,
    SystemMessage,
    TextBlock,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
)

MODEL = "claude-haiku-4-5"
SERVED = "claude-haiku-4-5-20251001"


class EchoArm:
    """One tool that repeats its argument, or raises when asked to."""

    name = "echo"

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def tools(self) -> list[dict[str, Any]]:
        return [
            {
                "name": "echo",
                "description": "Repeat the text.",
                "input_schema": {
                    "type": "object",
                    "properties": {"text": {"type": "string"}},
                    "required": ["text"],
                },
            }
        ]

    async def call(self, name: str, arguments: dict[str, Any]) -> str:
        self.calls.append(arguments)
        if arguments["text"] == "boom":
            raise ValueError("the page went away")
        return f"echo: {arguments['text']}"


def _counts(input_tokens: int, created: int, read: int, output: int) -> dict[str, int]:
    return {
        "inputTokens": input_tokens,
        "cacheCreationInputTokens": created,
        "cacheReadInputTokens": read,
        "outputTokens": output,
    }


def _request(input_tokens: int, created: int, read: int, output: int) -> dict[str, int]:
    return {
        "input_tokens": input_tokens,
        "cache_creation_input_tokens": created,
        "cache_read_input_tokens": read,
        "output_tokens": output,
    }


def _result(**changes: Any) -> ResultMessage:
    fields: dict[str, Any] = {
        "subtype": "success",
        "duration_ms": 1,
        "duration_api_ms": 1,
        "is_error": False,
        "num_turns": 2,
        "session_id": "scripted",
        "result": "DONE",
        "model_usage": {SERVED: _counts(20, 100, 1000, 9)},
    }
    fields.update(changes)
    return ResultMessage(**fields)


def _start(message_id: str, usage: dict[str, int], model: str = SERVED) -> StreamEvent:
    return StreamEvent(
        uuid=f"{message_id}-start",
        session_id="scripted",
        event={
            "type": "message_start",
            "message": {"id": message_id, "model": model, "usage": usage},
        },
    )


def _end(stop_reason: str, output_tokens: int) -> StreamEvent:
    return StreamEvent(
        uuid=f"{stop_reason}-end",
        session_id="scripted",
        event={
            "type": "message_delta",
            "delta": {"stop_reason": stop_reason},
            "usage": {"output_tokens": output_tokens},
        },
    )


def _init(**changes: Any) -> SystemMessage:
    data: dict[str, Any] = {
        "apiKeySource": "none",
        "model": MODEL,
        "tools": ["mcp__browser__echo"],
    }
    data.update(changes)
    return SystemMessage(subtype="init", data=data)


class ScriptedClaudeCode:
    """Stands in for ``query``: one tool call, then DONE, as Claude Code streams it.

    The tool is really called, through the handler the run built, so what the
    model is "shown" afterwards can be compared with what the arm returned.
    """

    def __init__(
        self,
        *,
        init: SystemMessage | None = None,
        result: ResultMessage | None = None,
        shown: str | None = None,
        rejected: bool = False,
        error: Any = None,
        model: str = SERVED,
    ) -> None:
        self.init = init or _init()
        self.result = result or _result()
        self.shown = shown
        self.rejected = rejected
        self.error = error
        self.model = model
        self.options: Any = None

    async def __call__(self, *, prompt: str, options: Any) -> AsyncIterator[Any]:
        self.options = options
        tool = options.mcp_servers[subscription.SERVER]["tools"][0]
        yield self.init
        # Claude Code streams one message per content block, all sharing the id
        # of the request that produced them and all carrying the usage as it
        # stood when that request *started*: the output count is the first few
        # tokens and there is no stop reason yet. Both arrive at the request's end.
        started = _request(10, 100, 500, 1)
        yield _start("m1", started)
        yield AssistantMessage(
            content=[TextBlock("Looking.")], model=SERVED, message_id="m1", usage=started
        )
        yield AssistantMessage(
            content=[ToolUseBlock(id="t1", name="mcp__browser__echo", input={"text": "hi"})],
            model=SERVED,
            message_id="m1",
            usage=started,
            error=self.error,
        )
        yield _end("tool_use", 5)
        answered = await tool.handler({"text": "hi"})
        if self.shown is not None:
            answered = {"content": [{"type": "text", "text": self.shown}]}
        results = [ToolResultBlock(tool_use_id="t1", content=answered["content"])]
        if self.rejected:
            results.append(
                ToolResultBlock(
                    tool_use_id="t2",
                    content=[
                        {"type": "text", "text": "Input validation error: 'text' is required"}
                    ],
                    is_error=True,
                )
            )
        yield UserMessage(content=cast(Any, results))
        started = _request(10, 0, 500, 1)
        yield _start("m2", started, model=self.model)
        yield AssistantMessage(
            content=[TextBlock("DONE")], model=self.model, message_id="m2", usage=started
        )
        yield _end("end_turn", 4)
        yield self.result
        if self.result.is_error:
            # As the SDK does: an error result is yielded, and then Claude Code
            # exits non-zero and the same result is raised again.
            raise ResultError(
                "Claude Code returned an error result",
                data={"subtype": self.result.subtype, "is_error": True},
                exit_code=1,
            )


@pytest.fixture
def claude_code(monkeypatch: pytest.MonkeyPatch) -> Any:
    """Install a scripted Claude Code, and an MCP server that only holds its tools."""
    for name in ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN"):
        monkeypatch.delenv(name, raising=False)

    def server(name: str, version: str = "1.0.0", tools: Any = None) -> Any:
        return {"type": "sdk", "name": name, "tools": tools}

    monkeypatch.setattr(subscription, "create_sdk_mcp_server", server)

    def install(**script: Any) -> ScriptedClaudeCode:
        scripted = ScriptedClaudeCode(**script)
        monkeypatch.setattr(subscription, "query", scripted)
        return scripted

    return install


# -- usage ------------------------------------------------------------------


def test_cached_input_is_counted_and_its_share_kept() -> None:
    """A total without cache reads would credit an arm for Claude Code's caching."""
    usage = usage_from({SERVED: _counts(20, 100, 1000, 9)}, MODEL)

    assert usage == Usage(
        input_tokens=1120,
        output_tokens=9,
        reported=True,
        cache_creation_input_tokens=100,
        cache_read_input_tokens=1000,
    )
    assert usage.total_tokens == 1129


@pytest.mark.parametrize(
    "model_usage",
    [
        {SERVED: _counts(1, 0, 0, 1), "claude-sonnet-4-5": _counts(1, 0, 0, 1)},
        {"claude-sonnet-4-5": _counts(1, 0, 0, 1)},
        {"claude-haiku-4-50": _counts(1, 0, 0, 1)},
    ],
)
def test_usage_from_another_model_is_refused(model_usage: dict[str, Any]) -> None:
    """Specification 14.2 fixes one model; a second entry is a request we did not make."""
    with pytest.raises(IsolationError):
        usage_from(model_usage, MODEL)


@pytest.mark.parametrize(
    "model_usage",
    [None, {}, {SERVED: {"inputTokens": 20, "outputTokens": 9}}],
)
def test_missing_usage_is_unreported_not_zero(model_usage: dict[str, Any] | None) -> None:
    assert usage_from(model_usage, MODEL).reported is False


def test_adding_a_turn_keeps_the_cache_breakdown() -> None:
    usage = Usage(
        input_tokens=10,
        output_tokens=1,
        reported=True,
        cache_creation_input_tokens=3,
        cache_read_input_tokens=4,
    )

    added = usage.plus(5, 2)

    assert (added.input_tokens, added.output_tokens) == (15, 3)
    assert (added.cache_creation_input_tokens, added.cache_read_input_tokens) == (3, 4)


# -- tools ------------------------------------------------------------------


@pytest.mark.parametrize(
    "arm",
    [
        BaselineArm(page=cast(Any, None)),
        WccArm(coordinator=cast(Any, None), driver=cast(Any, None)),
    ],
    ids=["baseline", "wcc"],
)
def test_each_arm_reaches_the_model_unchanged(arm: Any) -> None:
    """Specification 14.2: the arms differ in their tools and nothing else."""
    tools = mcp_tools(arm, [])

    assert [(tool.name, tool.description, tool.input_schema) for tool in tools] == [
        (spec["name"], spec["description"], spec["input_schema"]) for spec in arm.tools()
    ]
    # Read the way the SDK reads it: the field is Claude Code's, not MCP's.
    assert all(
        getattr(tool.annotations, "maxResultSizeChars", None) == subscription.MAX_RESULT_CHARS
        for tool in tools
    )


async def test_a_tool_that_raises_is_handed_back_and_recorded() -> None:
    delivered: list[str] = []
    (tool,) = mcp_tools(EchoArm(), delivered)

    answered = await tool.handler({"text": "boom"})

    assert answered == {
        "content": [{"type": "text", "text": "Error: ValueError: the page went away"}]
    }
    assert delivered == ["Error: ValueError: the page went away"]


# -- the session ------------------------------------------------------------


@pytest.mark.parametrize(
    "changes",
    [
        {"apiKeySource": "ANTHROPIC_API_KEY"},
        {"model": "claude-sonnet-4-5"},
        {"tools": ["mcp__browser__echo", "ListMcpResourcesTool"]},
        {"tools": []},
    ],
    ids=["api-key", "model", "extra-tool", "no-tools"],
)
def test_a_session_that_is_not_isolated_is_refused(changes: dict[str, Any]) -> None:
    with pytest.raises(IsolationError):
        check_init(_init(**changes).data, MODEL, ["mcp__browser__echo"])


def test_an_api_key_in_the_environment_is_refused(monkeypatch: pytest.MonkeyPatch) -> None:
    """Claude Code would bill the key's account instead of the subscription."""
    monkeypatch.setenv("ANTHROPIC_API_KEY", "not-a-real-key")

    with pytest.raises(SystemExit):
        SubscriptionAgent(MODEL)


async def test_a_run_is_read_back_per_request(claude_code: Any) -> None:
    scripted = claude_code()
    arm = EchoArm()

    result = await SubscriptionAgent(MODEL).run(arm, "Say hi.")

    assert arm.calls == [{"text": "hi"}]
    assert [turn.tool_calls for turn in result.turns] == [["echo"], []]
    assert [turn.text for turn in result.turns] == ["Looking.", "DONE"]
    assert [turn.input_tokens for turn in result.turns] == [610, 510]
    # From the end of each request, not the snapshot its messages carry.
    assert [turn.output_tokens for turn in result.turns] == [5, 4]
    assert [turn.stop_reason for turn in result.turns] == ["tool_use", "end_turn"]
    assert result.tool_calls == 1
    assert result.usage.input_tokens == 1120
    assert result.usage.cache_read_input_tokens == 1000
    assert result.final_text == "DONE"
    assert result.stopped_because == "success"
    # The snapshot that answered, not the alias that was asked for (14.2).
    assert result.served_model == SERVED
    # The run's own conditions, not whatever Claude Code would have chosen.
    assert scripted.options.system_prompt == SYSTEM_PROMPT
    assert scripted.options.max_turns == MAX_TURNS
    assert scripted.options.model == MODEL
    assert scripted.options.tools == []
    assert scripted.options.setting_sources == []
    assert scripted.options.allowed_tools == ["mcp__browser__echo"]
    assert scripted.options.include_partial_messages is True


QUIET = {"CLAUDE_CODE_DISABLE_TERMINAL_TITLE": "1", "CLAUDE_CODE_DISABLE_AUTO_MEMORY": "1"}


async def test_claude_code_makes_no_requests_of_its_own_choosing(
    claude_code: Any, monkeypatch: pytest.MonkeyPatch
) -> None:
    """Without these, Claude Code titled the session with a second model, and the
    run's usage named two models (smoke run, 2026-10-22). Calibration is held to
    the same settings, or the overhead it reports is not the one the runs paid."""
    scripted = claude_code()
    await SubscriptionAgent(MODEL).run(EchoArm(), "Say hi.")

    calibrated: dict[str, Any] = {}

    async def calibration(*, prompt: str, options: Any) -> AsyncIterator[Any]:
        calibrated["options"] = options
        yield _init(tools=[])
        yield _result(num_turns=1)

    monkeypatch.setattr(subscription, "query", calibration)
    await subscription.calibrate(MODEL)

    for settings in (scripted.options, calibrated["options"]):
        assert QUIET.items() <= settings.env.items()
    assert scripted.options.extra_args == calibrated["options"].extra_args


@pytest.mark.parametrize(
    "counts",
    [_counts(21, 100, 1000, 9), _counts(20, 100, 1000, 10)],
    ids=["input", "output"],
)
async def test_a_total_the_turns_do_not_add_up_to_is_refused(
    claude_code: Any, counts: dict[str, int]
) -> None:
    """Every token in the total belongs to a request in the transcript, or the
    total includes a request the benchmark did not make."""
    claude_code(result=_result(model_usage={SERVED: counts}))

    with pytest.raises(IsolationError):
        await SubscriptionAgent(MODEL).run(EchoArm(), "Say hi.")


async def test_a_result_the_arm_did_not_return_is_refused(claude_code: Any) -> None:
    claude_code(shown="echo: hi [truncated]")

    with pytest.raises(IsolationError):
        await SubscriptionAgent(MODEL).run(EchoArm(), "Say hi.")


async def test_a_call_the_sdk_rejected_is_not_mistaken_for_tampering(claude_code: Any) -> None:
    """Schema failures are answered before the arm sees them, so it has no record."""
    claude_code(rejected=True)

    result = await SubscriptionAgent(MODEL).run(EchoArm(), "Say hi.")

    assert result.tool_calls == 1


async def test_running_out_of_turns_is_the_arms_failure(claude_code: Any) -> None:
    claude_code(result=_result(subtype="error_max_turns", is_error=True, result=None))

    result = await SubscriptionAgent(MODEL).run(EchoArm(), "Say hi.")

    assert result.stopped_because == "max_turns"
    assert result.final_text == "DONE"


@pytest.mark.parametrize(
    "script",
    [
        {"error": "rate_limit"},
        {"result": _result(is_error=True, api_error_status=429)},
        {"result": _result(subtype="error_during_execution", is_error=True)},
        # What a subscription limit looks like when nothing else says so: a
        # message Claude Code wrote itself, and a run that still ends "success".
        {"model": "<synthetic>", "result": _result(is_error=True)},
        {"result": _result(is_error=True)},
    ],
    ids=["limit", "status", "execution", "synthetic", "success-with-error"],
)
async def test_a_provider_failure_stops_the_benchmark(
    claude_code: Any, script: dict[str, Any]
) -> None:
    """A run the model never got to finish says nothing about the arm."""
    claude_code(**script)

    with pytest.raises(ProviderError):
        await SubscriptionAgent(MODEL).run(EchoArm(), "Say hi.")


async def test_a_session_on_an_api_key_never_reaches_the_arm(claude_code: Any) -> None:
    claude_code(init=_init(apiKeySource="ANTHROPIC_API_KEY"))
    arm = EchoArm()

    with pytest.raises(IsolationError):
        await SubscriptionAgent(MODEL).run(arm, "Say hi.")

    assert arm.calls == []


async def test_another_model_answering_mid_run_is_refused(claude_code: Any) -> None:
    claude_code(model="claude-sonnet-4-5")

    with pytest.raises(IsolationError):
        await SubscriptionAgent(MODEL).run(EchoArm(), "Say hi.")


async def test_two_snapshots_answering_one_run_is_refused(claude_code: Any) -> None:
    """Specification 14.2 fixes the version as well as the model."""
    claude_code(model=f"{MODEL}-20991231")

    with pytest.raises(IsolationError):
        await SubscriptionAgent(MODEL).run(EchoArm(), "Say hi.")


async def test_a_refused_tool_call_is_an_isolation_failure(claude_code: Any) -> None:
    claude_code(result=_result(permission_denials=[{"tool_name": "Bash"}]))

    with pytest.raises(IsolationError):
        await SubscriptionAgent(MODEL).run(EchoArm(), "Say hi.")


# -- the runner -------------------------------------------------------------


def _fake_run(task_id: str, arm: str, attempt: int) -> Run:
    return Run(
        task_id=task_id,
        arm=arm,
        attempt=attempt,
        model=MODEL,
        started_at="2026-10-22T00:00:00+00:00",
        finished_at="2026-10-22T00:00:10+00:00",
        duration_seconds=10.0,
        tool_calls=1,
        turns=2,
        succeeded=True,
        input_value="x",
        usage=Usage(input_tokens=1, output_tokens=1, reported=True),
    )


async def test_every_cold_run_starts_empty_and_every_warm_run_follows_it(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    """The previous fixed directory survived the invocation, so a second run of
    the benchmark started its cold arm with the first one's template."""
    seen: list[tuple[str, int, list[str]]] = []

    async def run_one(
        agent: Any, task: Any, arm: str, attempt: int, base_url: str, data_dir: Path
    ) -> Run:
        seen.append((arm, attempt, sorted(path.name for path in data_dir.iterdir())))
        if arm == "wcc_cold":
            (data_dir / "template").write_text("persisted by the cold run")
        return _fake_run(task.task_id, arm, attempt)

    monkeypatch.setattr(runner, "RESULTS", tmp_path)
    monkeypatch.setattr(runner, "run_one", run_one)

    for _ in range(2):
        await runner._all(cast(Any, None), list(TASKS[:1]), 2, [])

    assert [files for arm, _, files in seen if arm == "wcc_cold"] == [[], [], [], []]
    assert [files for arm, _, files in seen if arm == "wcc_warm"] == [["template"]] * 4
    assert [files for arm, _, files in seen if arm == "baseline"] == [[], [], [], []]


def test_runs_that_finished_are_kept_when_the_provider_stops(
    monkeypatch: pytest.MonkeyPatch, tmp_path: Path
) -> None:
    class Agent:
        model = MODEL

        def __init__(self, model: str) -> None:
            pass

    async def stopped(agent: Any, tasks: Any, attempts: int, runs: list[Run]) -> list[Run]:
        runs.append(_fake_run(TASKS[0].task_id, "baseline", 1))
        raise ProviderError("usage limit reached")

    monkeypatch.setattr(runner, "RESULTS", tmp_path)
    monkeypatch.setattr(runner, "_all", stopped)
    monkeypatch.setattr(subscription, "SubscriptionAgent", Agent)

    with pytest.raises(ProviderError):
        runner.main([])

    (written,) = (tmp_path / "incomplete").iterdir()
    assert [run["arm"] for run in json.loads(written.read_text())] == ["baseline"]
    assert not list(tmp_path.glob("runs-*.json"))
