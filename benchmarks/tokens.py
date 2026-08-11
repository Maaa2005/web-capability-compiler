"""Exact token counts, without running a model (specification 14.3).

``POST /v1/messages/count_tokens`` returns the number of input tokens a request
would consume. It runs no inference, so it is free, and it is the provider's own
count rather than an approximation — which is exactly what specification 14.3
means by "machine-readable API usage metadata". Four characters per token is a
folk constant, and publishing one as though it were a measurement is the kind of
small dishonesty that makes the large ones easier.

What this measures is one observation's payload, as it would arrive in a message.
Every count includes the same fixed per-message overhead, so it is present on both
sides of every comparison and cancels out of the ratio. The tool definitions are
counted separately, because they are a per-turn cost the two arms do not share
equally and hiding them inside the payload figure would flatter whichever arm has
the smaller schema.

An API key is required. No credit is: this endpoint is not billed.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

#: A message that is as close to empty as the API accepts, used to subtract the
#: fixed overhead every request carries so that "tokens for these tool
#: definitions" is a number about the definitions.
_MINIMAL: list[dict[str, Any]] = [{"role": "user", "content": "."}]


class TokenCounterUnavailable(Exception):
    """No key, or no SDK. Distinct from a failed count, which is a real error."""


@dataclass(frozen=True)
class ArmTokens:
    """What one arm costs per turn, in provider-counted tokens."""

    tool_definitions: int
    observation: int


class TokenCounter:
    """Counts input tokens for one model, using the free endpoint."""

    def __init__(self, model: str, api_key: str | None = None) -> None:
        try:
            from anthropic import AsyncAnthropic
        except ImportError as missing:  # pragma: no cover - exercised by absence
            raise TokenCounterUnavailable(
                "counting tokens needs the anthropic package: install wcc with the 'llm' extra"
            ) from missing

        self.model = model
        self._client = AsyncAnthropic(api_key=api_key) if api_key else AsyncAnthropic()
        if self._client.api_key is None:
            raise TokenCounterUnavailable(
                "counting tokens needs ANTHROPIC_API_KEY. The endpoint itself is free "
                "— it runs no inference — but it still authenticates."
            )
        self._overhead: int | None = None

    async def _count(self, **arguments: Any) -> int:
        result = await self._client.messages.count_tokens(model=self.model, **arguments)
        return result.input_tokens

    async def overhead(self) -> int:
        """The tokens a request costs before anything of ours is in it."""
        if self._overhead is None:
            self._overhead = await self._count(messages=_MINIMAL)
        return self._overhead

    async def of_text(self, text: str) -> int:
        """Tokens for one payload, with the fixed request overhead removed.

        Subtracted rather than left in, because the interesting comparison is
        between two payloads and a shared constant in both makes a ratio look
        closer to 1 than it is.
        """
        counted = await self._count(messages=[{"role": "user", "content": text}])
        return counted - await self.overhead()

    async def of_tools(self, tools: list[dict[str, Any]]) -> int:
        """Tokens for a tool list, with the same overhead removed."""
        counted = await self._count(messages=_MINIMAL, tools=tools)
        return counted - await self.overhead()

    async def arm(self, tools: list[dict[str, Any]], observation: str) -> ArmTokens:
        return ArmTokens(
            tool_definitions=await self.of_tools(tools),
            observation=await self.of_text(observation),
        )
