"""Exact token counts, without running a model (specification 14.3).

Three counters, tried in order, and which one answered is printed with every
number it produces. That ordering is the whole design: the most authoritative
measurement available is used, and the report never pretends to a precision it
did not have.

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

An API key is required for the exact Claude count. No credit is: that endpoint is
not billed.

Without a key there is still a real measurement to be had. Anthropic does not
publish a tokenizer for current Claude models, but OpenAI does, and ``tiktoken``
runs offline. Its counts are not Claude's — a different tokenizer produces
different numbers — but this file's job is comparing two payloads of similar text
against each other, and a consistent ruler measures a *ratio* correctly even when
it disagrees about the absolute lengths. That is a real second choice, and it is
labelled as what it is rather than reported as "tokens" unqualified.

Characters are the third, and nothing anywhere divides them by four.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol, runtime_checkable

#: A message that is as close to empty as the API accepts, used to subtract the
#: fixed overhead every request carries so that "tokens for these tool
#: definitions" is a number about the definitions.
_MINIMAL: list[dict[str, Any]] = [{"role": "user", "content": "."}]


class TokenCounterUnavailable(Exception):
    """No key, no SDK, or no tokenizer. Distinct from a failed count."""


@runtime_checkable
class Counter(Protocol):
    """Anything that can say how long a payload is, and what its unit means."""

    unit: str
    """Printed beside every number. Never just "tokens": a reader has to be able
    to tell an Anthropic count from a tiktoken one without reading the code."""

    async def of_text(self, text: str) -> int: ...


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
        self.unit = f"tokens ({model}, counted by Anthropic)"
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


class LocalTokenCounter:
    """Offline counts, using OpenAI's published tokenizer.

    Not Claude's numbers, and the ``unit`` says so everywhere it is printed. What
    it is good for is the comparison this file exists to make: two payloads of
    similar JSON and English text, measured with one consistent ruler. A
    tokenizer that disagrees with Claude's about absolute length still divides
    two such payloads in nearly the same proportion, and a ratio is what the
    published claim rests on.

    The encoding is fetched once and cached by ``tiktoken``, so the first run
    needs a network and later ones do not.
    """

    def __init__(self, encoding: str = "o200k_base") -> None:
        try:
            import tiktoken
        except ImportError as missing:  # pragma: no cover - exercised by absence
            raise TokenCounterUnavailable(
                "counting tokens offline needs tiktoken: install the dev dependencies"
            ) from missing

        try:
            self._encoding = tiktoken.get_encoding(encoding)
        except Exception as failed:
            raise TokenCounterUnavailable(
                f"tiktoken could not load {encoding!r}: {failed}"
            ) from failed
        self.unit = f"tokens ({encoding}, not Claude's tokenizer)"

    async def of_text(self, text: str) -> int:
        return len(self._encoding.encode(text))


def build_counter(model: str, *, allow_local: bool = True) -> Counter | None:
    """The best counter available, or ``None`` to fall back to characters.

    Order matters and is not a preference: an Anthropic count is what
    specification 14.3 asks for, a tiktoken count is an honest approximation of
    it, and characters are what remains when neither is possible. Each step down
    is reported to the caller rather than taken silently, because a number whose
    provenance is invisible is a number that will eventually be quoted as
    something it is not.
    """
    try:
        return TokenCounter(model)
    except TokenCounterUnavailable as no_key:
        print(f"note: {no_key}")

    if not allow_local:
        return None

    try:
        counter = LocalTokenCounter()
    except TokenCounterUnavailable as no_tokenizer:
        print(f"note: {no_tokenizer}\n      Reporting characters instead.\n")
        return None

    print(f"      Falling back to {counter.unit}.\n")
    return counter
