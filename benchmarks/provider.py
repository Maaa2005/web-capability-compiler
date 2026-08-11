"""The one model client, and the only place a token is spent.

Isolated in its own module so that everything else in :mod:`benchmarks` can be
tested without a key, a network, or a bill. :class:`~benchmarks.agent_loop.ModelClient`
is a Protocol; the tests implement it with a scripted class and drive the same
loop this drives.

Usage metadata is passed through as ``None`` when the provider does not report it,
never as zero. Specification 14.3 requires the token counts to come from the API,
and :class:`~benchmarks.metrics.Usage` marks any run with a missing count as
unreportable rather than cheap.
"""

from __future__ import annotations

from typing import Any

from benchmarks.agent_loop import ModelReply, ToolRequest

#: Room for a tool-heavy turn without letting a confused run write an essay.
MAX_TOKENS = 4096


class AnthropicModelClient:
    """One turn of the Messages API, normalized into :class:`ModelReply`."""

    def __init__(self, model: str, api_key: str | None = None) -> None:
        from anthropic import AsyncAnthropic

        self.model = model
        self._client = AsyncAnthropic(api_key=api_key) if api_key else AsyncAnthropic()
        if self._client.api_key is None:
            raise SystemExit(
                "the benchmark calls a real API and needs ANTHROPIC_API_KEY. "
                "Use --dry-run to build everything without calling it."
            )

    async def respond(
        self,
        system: str,
        messages: list[dict[str, Any]],
        tools: list[dict[str, Any]],
    ) -> ModelReply:
        message = await self._client.messages.create(
            model=self.model,
            max_tokens=MAX_TOKENS,
            system=system,
            messages=messages,  # type: ignore[arg-type]
            tools=tools,  # type: ignore[arg-type]
        )

        text = "".join(
            block.text for block in message.content if getattr(block, "type", None) == "text"
        )
        requests = [
            ToolRequest(
                call_id=block.id,
                name=block.name,
                arguments=dict(block.input) if isinstance(block.input, dict) else {},
            )
            for block in message.content
            if getattr(block, "type", None) == "tool_use"
        ]
        usage = getattr(message, "usage", None)
        return ModelReply(
            text=text,
            tool_requests=requests,
            stop_reason=message.stop_reason,
            input_tokens=getattr(usage, "input_tokens", None),
            output_tokens=getattr(usage, "output_tokens", None),
            raw_content=[block.model_dump() for block in message.content],
        )
