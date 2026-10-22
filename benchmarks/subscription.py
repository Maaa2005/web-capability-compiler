"""The same benchmark, run on a Claude subscription instead of an API key.

Claude Code signed in to a subscription reports provider usage metadata for every
request, exactly as the Messages API does, and it does so without a key or a bill.
This module drives the arms through it. Nothing else changes: the tasks, the arms,
the verification and :data:`~benchmarks.agent_loop.SYSTEM_PROMPT` are the ones the
API path uses, and each arm's tool definitions reach the model unchanged through
an in-process MCP server.

What does change is who owns the loop, and that is stated rather than hidden.
Claude Code runs the request/response cycle here, so three things differ from
:func:`~benchmarks.agent_loop.run_loop`:

**A fixed preamble on every request.** Claude Code adds its own short identity
and environment block to the system prompt, before any tool definition. It is the
same for both arms, so it scales with the number of turns and not with the tool
surface. ``--calibrate`` measures it so a published result can report it beside
the totals; ``docs/progress.md`` records the measured figure.

**Prompt caching.** Claude Code caches the stable prefix, and the provider then
reports part of each request's input as cache reads. :class:`~benchmarks.metrics.Usage`
counts all three input fields, so the total is what the model read regardless of
how it was billed.

**Turn accounting.** A turn is one API request, identified by its message id.
Claude Code streams one assistant message per content block, and each carries the
usage as it stood when the request *started* — the first few output tokens and no
stop reason. So a turn's counts are read from the request's own stream events
instead: input from its start, output and stop reason from its end. The turns must
add up to the run's total, or the run is refused, because a total the transcript
does not account for includes a request the benchmark did not make.

Isolation is checked rather than assumed. A run refuses to continue unless the
session reports subscription sign-in (``apiKeySource`` of ``none``), the requested
model, and no tools beyond the arm's own. Settings, hooks, memory files, slash
commands, session persistence and the requests Claude Code makes on its own
initiative are all switched off, and each run gets a fresh empty working
directory.
"""

from __future__ import annotations

import os
import tempfile
import time
from collections.abc import Callable, Coroutine
from typing import Any

from claude_agent_sdk import (
    AssistantMessage,
    ClaudeAgentOptions,
    ResultError,
    ResultMessage,
    SdkMcpTool,
    StreamEvent,
    SystemMessage,
    TextBlock,
    ToolAnnotations,
    ToolResultBlock,
    ToolUseBlock,
    UserMessage,
    create_sdk_mcp_server,
    query,
)

from benchmarks.agent_loop import MAX_TURNS, SYSTEM_PROMPT, LoopResult, Turn
from benchmarks.arms import Arm
from benchmarks.metrics import Usage

#: One server name for both arms, so the prefix Claude Code puts on every tool
#: name (``mcp__browser__``) costs the arms the same.
SERVER = "browser"

#: Claude Code moves a large tool result into a file and shows the model a
#: preview. The API path hands the whole result back, and a baseline snapshot
#: that arrived as a preview would make the baseline cheaper by not reading it.
MAX_RESULT_CHARS = 5_000_000

#: Variables that would make Claude Code bill an API account instead.
_BILLING_VARIABLES = ("ANTHROPIC_API_KEY", "ANTHROPIC_AUTH_TOKEN")

#: How the SDK's MCP server words a call it answered without reaching the arm:
#: arguments that fail the tool's schema, or a tool name that does not exist. The
#: API path would have passed such a call to the arm, which then fails in its own
#: words; either way the model reads an error and the call is counted.
_REJECTED_BEFORE_THE_ARM = ("Input validation error:", "Tool '")


#: Requests Claude Code would otherwise make on its own initiative. Each is
#: switched off for every request the benchmark makes, calibration included.
QUIET = {
    # Titles the session with a request to a second model, which then appears in
    # the run's usage beside the requested one (smoke run, 2026-10-22).
    "CLAUDE_CODE_DISABLE_TERMINAL_TITLE": "1",
    # Reads and writes a memory directory outside the run's working directory.
    "CLAUDE_CODE_DISABLE_AUTO_MEMORY": "1",
}

#: No saved session to resume from, and no user commands to expand.
_FLAGS: dict[str, str | None] = {"no-session-persistence": None, "disable-slash-commands": None}


#: The model Claude Code names on a message it wrote itself rather than
#: received — how a usage limit or an API failure reaches the stream.
SYNTHETIC = "<synthetic>"


class IsolationError(RuntimeError):
    """The session was not the controlled one the benchmark requires."""


class ProviderError(RuntimeError):
    """The provider failed the request: a usage limit, an outage, a sign-in.

    Raised rather than recorded as a failed run. A run the model never got to
    attempt says nothing about the arm, and counting it would charge the arm for
    the subscription's limit.
    """


def prefixed(name: str) -> str:
    return f"mcp__{SERVER}__{name}"


def bare(name: str) -> str:
    return name.removeprefix(f"mcp__{SERVER}__")


def mcp_tools(arm: Arm, delivered: list[str]) -> list[SdkMcpTool[Any]]:
    """The arm's tools, as an in-process MCP server sees them.

    Name, description and schema are passed through as they are. A tool that
    raises is handed back as text, as :func:`~benchmarks.agent_loop.run_loop`
    does, and every result is appended to ``delivered`` so the run can check
    that the model was shown exactly what the arm returned.
    """

    def handler(name: str) -> Callable[[dict[str, Any]], Coroutine[Any, Any, dict[str, Any]]]:
        async def call(arguments: dict[str, Any]) -> dict[str, Any]:
            try:
                output = await arm.call(name, arguments)
            except Exception as failed:
                output = f"Error: {type(failed).__name__}: {failed}"
            delivered.append(output)
            return {"content": [{"type": "text", "text": output}]}

        return call

    return [
        SdkMcpTool(
            name=spec["name"],
            description=spec["description"],
            input_schema=spec["input_schema"],
            handler=handler(spec["name"]),
            annotations=ToolAnnotations(maxResultSizeChars=MAX_RESULT_CHARS),
        )
        for spec in arm.tools()
    ]


def usage_from(model_usage: dict[str, Any] | None, model: str) -> Usage:
    """Run totals from Claude Code's per-model usage, with cached input counted.

    Exactly one model may appear. Specification 14.2 fixes one model across every
    arm, and a second entry would mean a request this benchmark did not make.
    """
    if not model_usage:
        return Usage(reported=False)
    if len(model_usage) != 1:
        raise IsolationError(f"more than one model answered: {sorted(model_usage)}")
    (served, counts), *_ = model_usage.items()
    if not _same_model(served, model):
        raise IsolationError(f"{served} answered, not {model}")
    fields = ("inputTokens", "outputTokens", "cacheCreationInputTokens", "cacheReadInputTokens")
    if any(not isinstance(counts.get(key), int) for key in fields):
        return Usage(reported=False)
    created = counts["cacheCreationInputTokens"]
    read = counts["cacheReadInputTokens"]
    return Usage(
        input_tokens=counts["inputTokens"] + created + read,
        output_tokens=counts["outputTokens"],
        reported=True,
        cache_creation_input_tokens=created,
        cache_read_input_tokens=read,
    )


def _same_model(served: str, requested: str) -> bool:
    """``claude-haiku-4-5`` is served as itself or as a dated snapshot of itself."""
    return served == requested or served.startswith(f"{requested}-")


def _request_input(usage: dict[str, Any] | None) -> int | None:
    if not usage:
        return None
    parts = [
        usage.get(key)
        for key in ("input_tokens", "cache_creation_input_tokens", "cache_read_input_tokens")
    ]
    if not isinstance(parts[0], int):
        return None
    return sum(part for part in parts if isinstance(part, int))


def _text(message: AssistantMessage) -> str:
    return "".join(block.text for block in message.content if isinstance(block, TextBlock))


def _result_text(content: str | list[dict[str, Any]] | None) -> str:
    if isinstance(content, str):
        return content
    return "".join(
        str(block.get("text", "")) for block in content or [] if block.get("type") == "text"
    )


def _rejected(block: ToolResultBlock) -> bool:
    return bool(block.is_error) and _result_text(block.content).startswith(_REJECTED_BEFORE_THE_ARM)


def check_turns_add_up(turns: list[Turn], usage: Usage) -> None:
    """Every token in the run's total belongs to a request in its transcript."""
    if not usage.reported:
        return
    inputs = [turn.input_tokens for turn in turns if turn.input_tokens is not None]
    outputs = [turn.output_tokens for turn in turns if turn.output_tokens is not None]
    if len(inputs) != len(turns) or len(outputs) != len(turns):
        raise IsolationError("a request was streamed without its usage")
    if (sum(inputs), sum(outputs)) != (usage.input_tokens, usage.output_tokens):
        raise IsolationError(
            f"the turns add up to {sum(inputs)} in / {sum(outputs)} out, but the run "
            f"reported {usage.input_tokens} in / {usage.output_tokens} out"
        )


def check_init(data: dict[str, Any], model: str, tool_names: list[str]) -> None:
    """Refuse a session that is not the isolated, subscription-billed one."""
    if data.get("apiKeySource") != "none":
        raise IsolationError(
            f"the session signed in with {data.get('apiKeySource')!r}, not the subscription"
        )
    if not _same_model(str(data.get("model")), model):
        raise IsolationError(f"the session runs {data.get('model')}, not {model}")
    offered = sorted(data.get("tools") or [])
    if offered != sorted(tool_names):
        raise IsolationError(f"the model was offered {offered}, not only the arm's tools")


def options(model: str, arm_tools: list[SdkMcpTool[Any]], cwd: str) -> ClaudeAgentOptions:
    names = [prefixed(tool.name) for tool in arm_tools]
    return ClaudeAgentOptions(
        system_prompt=SYSTEM_PROMPT,
        model=model,
        max_turns=MAX_TURNS,
        tools=[],
        allowed_tools=names,
        mcp_servers={SERVER: create_sdk_mcp_server(SERVER, tools=arm_tools)},
        strict_mcp_config=True,
        setting_sources=[],
        thinking={"type": "disabled"},
        cwd=cwd,
        env={**QUIET, "MAX_MCP_OUTPUT_TOKENS": str(MAX_RESULT_CHARS)},
        extra_args=dict(_FLAGS),
        include_partial_messages=True,
    )


class SubscriptionAgent:
    """Drives an arm through Claude Code signed in to a subscription."""

    def __init__(self, model: str) -> None:
        present = [name for name in _BILLING_VARIABLES if os.environ.get(name)]
        if present:
            raise SystemExit(
                f"{', '.join(present)} is set, so Claude Code would bill an API account. "
                "Unset it to run on the subscription, or use --provider api."
            )
        self.model = model

    async def run(self, arm: Arm, prompt: str) -> LoopResult:
        delivered: list[str] = []
        shown: list[str] = []
        arm_tools = mcp_tools(arm, delivered)
        turns: dict[str, dict[str, Any]] = {}
        current = ""
        result: ResultMessage | None = None
        served: str | None = None
        started = time.monotonic()

        with tempfile.TemporaryDirectory(prefix="wcc-bench-") as cwd:
            try:
                async for message in query(
                    prompt=prompt, options=options(self.model, arm_tools, cwd)
                ):
                    if isinstance(message, SystemMessage) and message.subtype == "init":
                        check_init(
                            message.data, self.model, [prefixed(tool.name) for tool in arm_tools]
                        )
                    elif isinstance(message, StreamEvent) and message.parent_tool_use_id is None:
                        event = message.event
                        if event.get("type") == "message_start":
                            current = str(event["message"]["id"])
                            turn = turns.setdefault(current, {"text": "", "tools": []})
                            turn["usage"] = event["message"].get("usage")
                        elif event.get("type") == "message_delta" and current in turns:
                            turns[current]["output_tokens"] = (event.get("usage") or {}).get(
                                "output_tokens"
                            )
                            turns[current]["stop_reason"] = (event.get("delta") or {}).get(
                                "stop_reason"
                            )
                    elif isinstance(message, AssistantMessage):
                        if message.error or message.model == SYNTHETIC:
                            # A usage limit or an outage arrives as a message Claude
                            # Code wrote itself, and the run can still end "success".
                            raise ProviderError(
                                f"no model answered ({message.error}): {_text(message)}"
                            )
                        if not _same_model(message.model, self.model):
                            raise IsolationError(f"{message.model} answered, not {self.model}")
                        if served not in (None, message.model):
                            # Specification 14.2 fixes the version, not only the model.
                            raise IsolationError(f"{served} and {message.model} both answered")
                        served = message.model
                        key = message.message_id or f"unidentified-{len(turns)}"
                        turn = turns.setdefault(key, {"text": "", "tools": []})
                        for block in message.content:
                            if isinstance(block, TextBlock):
                                turn["text"] += block.text
                            elif isinstance(block, ToolUseBlock):
                                turn["tools"].append(bare(block.name))
                    elif isinstance(message, UserMessage) and isinstance(message.content, list):
                        shown.extend(
                            _result_text(block.content)
                            for block in message.content
                            if isinstance(block, ToolResultBlock) and not _rejected(block)
                        )
                    elif isinstance(message, ResultMessage):
                        result = message
            except ResultError:
                # The SDK reports a failed run twice: as the result message
                # above, then as this, when Claude Code exits non-zero. The
                # message is what is judged below — running out of turns is
                # the arm's failure, anything else the provider's.
                if result is None:
                    raise

        if result is None:
            raise IsolationError("Claude Code ended without reporting a result")
        if result.permission_denials:
            raise IsolationError(f"Claude Code refused calls: {result.permission_denials}")
        if result.api_error_status or (result.is_error and result.subtype != "error_max_turns"):
            raise ProviderError(
                f"the run ended {result.subtype} with status {result.api_error_status}: "
                f"{result.errors or result.result}"
            )
        if shown != delivered:
            raise IsolationError("the model was not shown exactly what the arm returned")

        recorded = [
            Turn(
                stop_reason=turn.get("stop_reason"),
                input_tokens=_request_input(turn.get("usage")),
                output_tokens=turn.get("output_tokens"),
                tool_calls=turn["tools"],
                text=turn["text"],
            )
            for turn in turns.values()
        ]
        usage = usage_from(dict(result.model_usage or {}), self.model)
        check_turns_add_up(recorded, usage)
        stopped = "max_turns" if result.subtype == "error_max_turns" else result.subtype
        return LoopResult(
            turns=recorded,
            usage=usage,
            tool_calls=sum(len(turn.tool_calls) for turn in recorded),
            duration_seconds=round(time.monotonic() - started, 3),
            final_text=result.result or (recorded[-1].text if recorded else ""),
            stopped_because=stopped,
            served_model=served,
        )


async def calibrate(model: str) -> Usage:
    """The input Claude Code sends when the benchmark sends almost nothing.

    One request, no tools, a one-word system prompt and a one-word question. What
    comes back is the per-request preamble, which a reader needs in order to
    separate Claude Code's share of a total from the arm's.
    """
    result: ResultMessage | None = None
    with tempfile.TemporaryDirectory(prefix="wcc-bench-") as cwd:
        settings = ClaudeAgentOptions(
            system_prompt="Answer.",
            model=model,
            max_turns=1,
            tools=[],
            strict_mcp_config=True,
            setting_sources=[],
            thinking={"type": "disabled"},
            cwd=cwd,
            env=dict(QUIET),
            extra_args=dict(_FLAGS),
        )
        async for message in query(prompt="Hi.", options=settings):
            if isinstance(message, SystemMessage) and message.subtype == "init":
                check_init(message.data, model, [])
            elif isinstance(message, ResultMessage):
                result = message
    if result is None:
        raise IsolationError("Claude Code ended without reporting a result")
    return usage_from(dict(result.model_usage or {}), model)
