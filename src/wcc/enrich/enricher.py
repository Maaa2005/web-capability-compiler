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
"""

from __future__ import annotations

import logging
from typing import Protocol

from wcc.enrich.apply import apply_enrichment
from wcc.enrich.models import PageEnrichment
from wcc.enrich.payload import build_request, refs_to_templates
from wcc.enrich.provider import EnrichmentProvider
from wcc.models.capability import CapabilityTemplate
from wcc.models.session import PageSnapshot

logger = logging.getLogger(__name__)


class EnrichmentCache(Protocol):
    """Memory of what a provider already said about a page class.

    Keyed by ``page_signature``, which specification 7.3 defines as the identity of
    a reusable page class. Two pages that differ only in their content have one
    signature and deserve one model call; a page whose controls changed has a new
    signature and is a new question.
    """

    def load_enrichment(self, page_signature: str) -> PageEnrichment | None: ...

    def save_enrichment(self, page_signature: str, enrichment: PageEnrichment) -> None: ...


class Enricher:
    """Applies a provider's naming to a compilation, or leaves it alone."""

    def __init__(
        self,
        provider: EnrichmentProvider | None = None,
        cache: EnrichmentCache | None = None,
    ) -> None:
        self._provider = provider
        self._cache = cache
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

        try:
            cached = self._cache.load_enrichment(snapshot.page_signature) if self._cache else None
            enrichment = cached if cached is not None else await self._provider.enrich(request)
            enriched = apply_enrichment(templates, enrichment, refs_to_templates(templates))
        except Exception as failed:
            # Deliberately broad; see the module docstring. The type is preserved in
            # the message so a recurring failure is diagnosable from a log alone.
            self.last_error = f"{type(failed).__name__}: {failed}"
            logger.warning("semantic enrichment was skipped: %s", self.last_error)
            return templates

        if cached is None and self._cache is not None:
            # Cached only after it applied cleanly. Storing a response that the
            # rules rejected would replay that rejection on every later visit at
            # no cost, which looks exactly like a working cache.
            self._cache.save_enrichment(snapshot.page_signature, enrichment)
        return enriched
