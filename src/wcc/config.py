"""Trusted local configuration, read from the environment (specification 10.2).

One rule shapes this module: nothing here may be set by an agent. The MCP server
takes its instructions from a client over stdio, and everything that arrives that
way is a request. What lives here arrives the other way — from whoever launched
the process — and that is the only reason it is allowed to relax a boundary.

The private-network allowance is the case that matters. Specification 10.2 calls
it developer-only, so it was removed from ``OpenSessionRequest`` in Phase 1; this
is where it legitimately comes from instead.
"""

from __future__ import annotations

import logging
import os

from wcc.driver.base import DriverSettings
from wcc.enrich.enricher import Enricher, EnrichmentCache
from wcc.enrich.provider import AnthropicProvider, EnrichmentUnavailable
from wcc.limits import ResourceLimits

logger = logging.getLogger(__name__)

ALLOW_PRIVATE_NETWORK_ENV = "WCC_ALLOW_PRIVATE_NETWORK"
ENRICHMENT_ENV = "WCC_ENRICHMENT"
ENRICHMENT_MODEL_ENV = "WCC_ENRICHMENT_MODEL"
API_KEY_ENV = "ANTHROPIC_API_KEY"

#: Off. Specification principle 9 makes the LLM optional, and an optional feature
#: that is on unless disabled is not optional — it would mean a default install
#: sending page structure to a third party because nobody set a variable.
DEFAULT_ENRICHMENT_MODEL = "claude-opus-5"

#: Values that mean "yes". Anything else — including a typo, an empty string, or
#: the word "false" — means no. A boundary that opened on an unrecognised value
#: would open on a mistake.
_TRUTHY = frozenset({"1", "true", "yes", "on"})


def allow_private_network(environ: dict[str, str] | None = None) -> bool:
    """Whether this process may reach loopback and private addresses."""
    env = os.environ if environ is None else environ
    return env.get(ALLOW_PRIVATE_NETWORK_ENV, "").strip().lower() in _TRUTHY


def driver_settings(environ: dict[str, str] | None = None) -> DriverSettings:
    """The driver's permissions and budgets for this process."""
    return DriverSettings(
        allow_private_network=allow_private_network(environ),
        limits=ResourceLimits(),
    )


def enrichment_enabled(environ: dict[str, str] | None = None) -> bool:
    """Whether this process may send reduced page structure to a model."""
    env = os.environ if environ is None else environ
    return env.get(ENRICHMENT_ENV, "").strip().lower() in _TRUTHY


def enrichment_model(environ: dict[str, str] | None = None) -> str:
    env = os.environ if environ is None else environ
    return env.get(ENRICHMENT_MODEL_ENV, "").strip() or DEFAULT_ENRICHMENT_MODEL


def build_enricher(
    cache: EnrichmentCache | None = None, environ: dict[str, str] | None = None
) -> Enricher:
    """The enricher this process should use — inert unless asked for.

    A provider that cannot be constructed produces an inert enricher rather than
    an exception, for the same reason :mod:`wcc.enrich.enricher` swallows at call
    time: a missing optional dependency or an unset API key must not stop a server
    whose core pipeline never needed a model.

    The key is read from ``environ`` alongside everything else rather than left to
    the SDK's own lookup. One function that half-consults its argument and half
    consults the process is a function whose result depends on which half you were
    thinking of.
    """
    if not enrichment_enabled(environ):
        return Enricher()
    env = os.environ if environ is None else environ
    api_key = env.get(API_KEY_ENV, "").strip()
    if not api_key:
        logger.warning(
            "semantic enrichment is configured but %s is not set; continuing without it",
            API_KEY_ENV,
        )
        return Enricher()
    try:
        model = enrichment_model(environ)
        provider = AnthropicProvider(model, api_key=api_key)
    except EnrichmentUnavailable as unavailable:
        logger.warning("semantic enrichment is configured but unavailable: %s", unavailable)
        return Enricher()
    return Enricher(provider, cache=cache, model=model)
