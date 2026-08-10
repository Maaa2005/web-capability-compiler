"""Merging a provider response into templates, under three hard rules.

Phase 6's acceptance names them: enrichment "cannot alter target locator chains,
execution actions, or lower deterministic risk". They are enforced twice here, and
the duplication is deliberate.

The first enforcement is *constructive*. :func:`_enriched` builds the new template
by copying the old one and replacing exactly three fields — name, description, and
the ``title``/``description`` annotations inside the parameter schema. A locator
chain is never named, so no response can reach one, whatever it contains.

The second is a *check*. :func:`_unchanged` compares the result against the input
on the fields that must not have moved, and raises if any did. That check cannot
fail today, which is the point: it is aimed at the edit that adds a fourth field
to the copy a year from now, when the sentence in this docstring has been skimmed
rather than read. A rule that only lives in a docstring stops being a rule.

Risk is the third, and works differently. A ``risk_hint`` may raise a level and is
ignored when it would lower one — not rejected, ignored, because a model guessing
low on a form the compiler classified high is the expected failure and is exactly
what the deterministic classification is for.
"""

from __future__ import annotations

from typing import Any

from wcc.enrich.models import CapabilityEnrichment, PageEnrichment
from wcc.models.capability import CapabilityTemplate, Risk, risk_order


class EnrichmentRejected(Exception):
    """A response was well-formed but did not describe the page it was asked about.

    Distinct from a validation error, which :mod:`wcc.enrich.models` raises for a
    response that is malformed in itself. This one is about the *relationship*
    between a response and its request — a reference to a candidate that was never
    sent, or a parameter that does not exist.
    """


def apply_enrichment(
    templates: list[CapabilityTemplate],
    enrichment: PageEnrichment,
    refs: dict[str, CapabilityTemplate],
) -> list[CapabilityTemplate]:
    """Return ``templates`` with the enrichment applied, or raise.

    All or nothing. A response is one statement about one page, and applying the
    half of it that parsed would produce a page named partly by the compiler and
    partly by a model, with no record of which was which.
    """
    by_template_id: dict[str, CapabilityEnrichment] = {}
    for item in enrichment.capabilities:
        template = refs.get(item.ref)
        if template is None:
            raise EnrichmentRejected(f"{item.ref} was not one of the candidates")
        by_template_id[template.template_id] = item

    enriched = [
        _enriched(template, by_template_id[template.template_id])
        if template.template_id in by_template_id
        else template
        for template in templates
    ]

    names = [template.name for template in enriched]
    if len(set(names)) != len(names):
        raise EnrichmentRejected(
            "the enriched names collide with a name the compiler kept; a page "
            "cannot offer two capabilities under one name"
        )
    return enriched


def _enriched(template: CapabilityTemplate, enrichment: CapabilityEnrichment) -> CapabilityTemplate:
    updated = template.model_copy(
        update={
            "name": enrichment.name,
            "description": enrichment.description,
            "parameters": _annotated(template.parameters, enrichment),
            "risk": _raised(template.risk, enrichment),
        }
    )
    _unchanged(template, updated)
    return updated


def _annotated(parameters: dict[str, Any], enrichment: CapabilityEnrichment) -> dict[str, Any]:
    """Rewrite parameter descriptions, leaving the schema's shape alone.

    Only ``description`` moves. Types, ``required``, and ``additionalProperties``
    are the contract :mod:`wcc.runtime.arguments` validates against at execution
    time, and a model that could widen a type would be able to make an argument
    check pass that the compiler intended to fail.
    """
    if not enrichment.parameters:
        return parameters

    properties = parameters.get("properties")
    if not isinstance(properties, dict):
        raise EnrichmentRejected("the response describes parameters this capability has none of")

    rewritten = dict(properties)
    for parameter in enrichment.parameters:
        existing = rewritten.get(parameter.name)
        if not isinstance(existing, dict):
            raise EnrichmentRejected(f"{parameter.name!r} is not a parameter of this capability")
        rewritten[parameter.name] = {**existing, "description": parameter.description}

    return {**parameters, "properties": rewritten}


def _raised(risk: Risk, enrichment: CapabilityEnrichment) -> Risk:
    """Take the higher of the deterministic level and the hint.

    ``side_effect`` is never touched, so :class:`Risk`'s own floor still applies:
    the level may rise above what the side effect implies, and can never be argued
    below it. A hint is an opinion about danger, and the only safe direction for an
    opinion to move a safety classification is up.
    """
    hint = enrichment.risk_hint
    if hint is None or risk_order(hint) <= risk_order(risk.level):
        return risk
    return risk.model_copy(update={"level": hint})


#: The fields a response must leave exactly as the compiler produced them. Named
#: rather than derived, so that adding a field to :class:`CapabilityTemplate`
#: without deciding whether enrichment may touch it does not silently opt it in.
PROTECTED_FIELDS = (
    "template_id",
    "origin",
    "normalized_route_pattern",
    "page_signature",
    "category",
    "execution_plan_template",
    "target_locators",
    "expected_effect",
    "evidence",
    "confidence",
    "created_at",
    "provenance",
)


def _unchanged(before: CapabilityTemplate, after: CapabilityTemplate) -> None:
    moved = [field for field in PROTECTED_FIELDS if getattr(before, field) != getattr(after, field)]
    if moved:
        raise EnrichmentRejected(f"enrichment altered {moved}, which it may never do")
    if risk_order(after.risk.level) < risk_order(before.risk.level):
        raise EnrichmentRejected("enrichment lowered a deterministic risk classification")
    if after.risk.side_effect is not before.risk.side_effect:
        raise EnrichmentRejected("enrichment changed a side effect")
