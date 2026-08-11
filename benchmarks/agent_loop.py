"""The deliberately small model loop (specification Phase 7).

One loop, shared by both arms. Specification 14.2 permits the arms to differ in
their tool descriptions and in nothing else, and the surest way to honour that is
for there to be only one place where a turn happens.

It is small on purpose. There is no planner, no retry policy, no scratchpad, no
reflection step. Every one of those would be a variable that the two arms could
respond to differently, and the benchmark would then be measuring the harness.
Specification Phase 7 rules out building "an autonomous benchmark agent" and this
is where that restraint has to live.

:class:`ModelClient` is a Protocol with one method so the loop can be exercised
without a network — the scripted client in the tests drives the same code path
that the Anthropic client drives, which is what makes it worth having.
"""

from __future__ import annotations

import time
from dataclasses import dataclass
from typing import Any, Protocol

from benchmarks.arms import Arm
from benchmarks.metrics import Usage

#: A run that has not finished by here is a failed run, not a slow one. Without a
#: ceiling a confused model can loop until the budget is gone, and the resulting
#: token count would be reported as the cost of the arm rather than of the loop.
MAX_TURNS = 25

SYSTEM_PROMPT = """\
You are operating a web browser through the tools you have been given, to
complete one task on one site.

Use the tools. Do not describe what you would do. When the task is complete,
reply with the single word DONE.
"""


@dataclass(frozen=True)
class Turn:
    """One request/response pair, as recorded in the raw log."""

    stop_reason: str | None
    input_tokens: int | None
    output_tokens: int | None
    tool_calls: list[str]
    text: str


class ModelClient(Protocol):
    """One model turn: messages and tools in, a normalized reply out."""

    model: str

    async def respond(
        self,
        system: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
    ) -> ModelReply: ...


@dataclass(frozen=True)
class ToolRequest:
    call_id: str
    name: str
    arguments: dict[str, Any]


@dataclass(frozen=True)
class ModelReply:
    """A provider response, reduced to what the loop and the metrics need.

    ``input_tokens`` and ``output_tokens`` are ``None`` when the provider reported
    none. Not zero — specification 14.3 requires the numbers to come from usage
    metadata, and a zero would summarize as a very cheap turn.
    """

    text: str
    tool_requests: list[ToolRequest]
    stop_reason: str | None
    input_tokens: int | None
    output_tokens: int | None
    raw_content: list[dict[str, Any]]


@dataclass
class LoopResult:
    turns: list[Turn]
    usage: Usage
    tool_calls: int
    duration_seconds: float
    final_text: str
    stopped_because: str

    @property
    def turn_count(self) -> int:
        return len(self.turns)


async def run_loop(client: ModelClient, arm: Arm, prompt: str) -> LoopResult:
    """Drive ``arm`` with ``client`` until the model stops asking for tools."""
    messages: list[dict[str, Any]] = [{"role": "user", "content": prompt}]
    turns: list[Turn] = []
    usage = Usage(reported=True)
    tool_calls = 0
    started = time.monotonic()
    stopped_because = "max_turns"

    for _ in range(MAX_TURNS):
        reply = await client.respond(SYSTEM_PROMPT, messages, arm.tools())
        usage = usage.plus(reply.input_tokens, reply.output_tokens)
        turns.append(
            Turn(
                stop_reason=reply.stop_reason,
                input_tokens=reply.input_tokens,
                output_tokens=reply.output_tokens,
                tool_calls=[request.name for request in reply.tool_requests],
                text=reply.text,
            )
        )

        if not reply.tool_requests:
            stopped_because = reply.stop_reason or "no_tool_use"
            break

        messages.append({"role": "assistant", "content": reply.raw_content})
        results: list[dict[str, Any]] = []
        for request in reply.tool_requests:
            tool_calls += 1
            try:
                output = await arm.call(request.name, request.arguments)
            except Exception as failed:
                # Handed back rather than raised. A tool that errors is an
                # ordinary event for an agent, and a harness that aborted here
                # would score a recoverable mistake as a crash.
                output = f"Error: {type(failed).__name__}: {failed}"
            results.append(
                {
                    "type": "tool_result",
                    "tool_use_id": request.call_id,
                    "content": output,
                }
            )
        messages.append({"role": "user", "content": results})

    return LoopResult(
        turns=turns,
        usage=usage,
        tool_calls=tool_calls,
        duration_seconds=round(time.monotonic() - started, 3),
        final_text=turns[-1].text if turns else "",
        stopped_because=stopped_because,
    )
