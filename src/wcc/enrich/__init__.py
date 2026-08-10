"""Optional semantic enrichment (specification 9.3, Phase 6).

Everything in this package is optional in the strongest sense: with no provider
configured, nothing here runs, and the compiler's output is the system's output.
That is not a fallback path bolted on for robustness — specification principle 9
requires the core extraction and execution pipeline to run without an LLM, so the
deterministic result is the product and enrichment is a rewording of its prose.

The three rules the package exists to enforce are in :mod:`wcc.enrich.apply`:
enrichment may not touch a locator chain, may not touch an execution plan, and may
not lower a deterministic risk classification.
"""

from wcc.enrich.apply import EnrichmentRejected, apply_enrichment
from wcc.enrich.enricher import Enricher
from wcc.enrich.models import CapabilityEnrichment, PageEnrichment
from wcc.enrich.payload import CandidateSummary, EnrichmentRequest, build_request
from wcc.enrich.provider import EnrichmentProvider, EnrichmentUnavailable

__all__ = [
    "CandidateSummary",
    "CapabilityEnrichment",
    "EnrichmentProvider",
    "EnrichmentRejected",
    "EnrichmentRequest",
    "EnrichmentUnavailable",
    "Enricher",
    "PageEnrichment",
    "apply_enrichment",
    "build_request",
]
