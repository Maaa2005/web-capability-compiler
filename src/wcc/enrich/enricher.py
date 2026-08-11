"""The one entry point the compiler pipeline calls, and the only one that swallows.

Everything below this module raises: a malformed response, a reference to a
candidate that was not sent, an unreachable provider. Everything above it must
keep working when any of those happen, because Phase 6's first acceptance
criterion is that the full system works with the LLM disabled — and a provider
that times out is, for the duration of that call, disabled.

So :meth:`Enricher.enrich` returns templates and never raises. Failures are
recorded on the enricher as :attr:`Enricher.last_error` and logged, which keeps
them visible without making the caller's success depend on them. This is the one
place in the project where broad exception handling is right, and it is right for
a reason that is worth stating rather than assuming: enrichment is cosmetic. It
improves names. Nothing downstream reads a name to decide what to do — execution
goes by capability ID, risk by the deterministic classifier, arguments by the
schema — so there is no failure of enrichment that should become a failure of
inspection.

That contract has to cover the *whole* method, including the cache write. An
earlier version put ``save_enrichment`` after the ``try``, so a full disk or a
locked database turned a cosmetic pass into a failed inspection — the exact
outcome this docstring promises cannot happen.
"""

from __future__ import annotations

import asyncio
import hashlib
import logging
from typing import Protocol

from wcc.enrich.apply import apply_enrichment
from wcc.enrich.models import PageEnrichment
from wcc.enrich.payload import EnrichmentRequest, build_request, refs_to_templates
from wcc.enrich.provider import EnrichmentProvider
from wcc.models.capability import CapabilityTemplate
from wcc.models.session import PageSnapshot

logger = logging.getLogger(__name__)

#: Bumped when the prompt or the response contract changes. Part of the cache key,
#: because a cached answer to a question this build no longer asks is not a cache
#: hit — it is a stale answer that looks like one.
PROMPT_VERSION = "2"

#: A ceiling over the whole enrichment, independent of the provider's own timeout.
#: Belt and braces on purpose: the provider timeout bounds one HTTP call, and this
#: bounds everything including a client that hangs before making one.
TOTAL_TIMEOUT_SECONDS = 30.0


class EnrichmentCache(Protocol):
    """Memory of what a provider already said about a page class.

    Keyed by :func:`cache_key`, not by ``page_signature`` alone. Two things made
    the simpler key wrong. A response refers to candidates positionally, as ``c1``
    and ``c2``, while a page signature identifies a *class* of page whose template
    order can differ between visits — so a cached ``c1`` could be applied to a
    different capability than the one it described. And a cached response outlives
    changes to the model and the prompt that produced it.
    """

    def load_enrichment(self, key: str) -> PageEnrichment | None: ...

    def save_enrichment(self, key: str, enrichment: PageEnrichment) -> None: ...


def cache_key(request: EnrichmentRequest, page_signature: str, model: str) -> str:
    """A cache identity that changes whenever the question does.

    The request is serialized canonically and hashed together with the page
    signature, the model, and the prompt version. Anything that would change the
    answer changes the key, which is the only property a cache of paid answers
    actually needs.
    """
    material = "\n".join(
        [page_signature, model, PROMPT_VERSION, request.model_dump_json()]
    ).encode()
    return f"sha256:{hashlib.sha256(material).hexdigest()}"


class Enricher:
    """Applies a provider's naming to a compilation, or leaves it alone."""

    def __init__(
        self,
        provider: EnrichmentProvider | None = None,
        cache: EnrichmentCache | None = None,
        model: str = "unknown",
        timeout: float = TOTAL_TIMEOUT_SECONDS,
    ) -> None:
        self._provider = provider
        self._cache = cache
        self._model = model
        self._timeout = timeout
        self.last_error: str | None = None

    @property
    def enabled(self) -> bool:
        return self._provider is not None

    async def enrich(
        self, snapshot: PageSnapshot, templates: list[CapabilityTemplate]
    ) -> list[CapabilityTemplate]:
        """Return enriched templates, or the originals unchanged."""
        self.last_error = None
        if self._provider is None or not templates:
            return templates

        request = build_request(snapshot, templates)
        if request is None:
            return templates

        key = cache_key(request, snapshot.page_signature, self._model)
        try:
            async with asyncio.timeout(self._timeout):
                return await self._apply(request, key, templates)
        except asyncio.CancelledError:
            # Never swallowed. A cancellation is the caller withdrawing, not a
            # provider misbehaving, and converting it into a quiet fallback would
            # keep this coroutine running after the thing that wanted it left.
            raise
        except Exception as failed:
            # Deliberately broad; see the module docstring.
            self._record(failed)
            return templates

    async def _apply(
        self, request: EnrichmentRequest, key: str, templates: list[CapabilityTemplate]
    ) -> list[CapabilityTemplate]:
        cached = self._cache.load_enrichment(key) if self._cache else None
        enrichment = cached if cached is not None else await self._must_have_provider(request)
        enriched = apply_enrichment(templates, enrichment, refs_to_templates(templates))

        if cached is None and self._cache is not None:
            # Cached only after it applied cleanly, and inside its own guard. A
            # rejected response replayed from cache would repeat its rejection on
            # every visit at no cost, which looks exactly like a working cache;
            # and a cache that cannot be written is still not a reason to fail.
            try:
                self._cache.save_enrichment(key, enrichment)
            except Exception as failed:
                self._record(failed, doing="caching the enrichment")
        return enriched

    async def _must_have_provider(self, request: EnrichmentRequest) -> PageEnrichment:
        assert self._provider is not None  # noqa: S101 - guarded by the caller
        return await self._provider.enrich(request)

    def _record(self, failed: BaseException, doing: str = "enriching") -> None:
        self.last_error = f"{type(failed).__name__}: {failed}"
        logger.warning("semantic enrichment failed while %s: %s", doing, self.last_error)
