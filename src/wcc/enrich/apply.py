"""Merging a provider response into templates, under three hard rules.

Phase 6's acceptance names them: enrichment "cannot alter target locator chains,
execution actions, or lower deterministic risk". They are enforced twice here, and
the duplication is deliberate.

The first enforcement is *constructive*. :func:`_enriched` builds the new template
by copying the old one and replacing exactly three things — the name, a
locally-composed description, and the ``description`` annotations inside the
parameter schema. A locator chain is never named, so no response can reach one.

The second is a *check*, and it is an allowlist rather than a denylist. An earlier
version listed the fields enrichment must not touch, which a counter-review
correctly called out: it omitted ``schema_version``, never compared
``risk.reversible`` or ``risk.requires_confirmation``, and did no deep comparison
of the parameter schema. A list of what must not move has to be revised every time
a field is added, and nothing fails when it is not. :func:`_only_allowed_changes`
diffs the two full ``model_dump()`` documents instead and permits exactly four
paths. A new field is protected the day it is added, by default, without anyone
remembering to protect it.

Risk is the third rule and works differently. A ``risk_hint`` may raise a level and
is ignored when it would lower one — ignored rather than rejected, because a model
guessing low on a form the compiler classified high is the expected failure and is
exactly what the deterministic classification is for.
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


#: The only paths that may differ between a template and its enriched form.
#: ``parameters`` is matched per property by :func:`_allowed_parameter_change`.
ALLOWED_PATHS = frozenset({"name", "description", "risk", "parameters"})


def describe(template: CapabilityTemplate, subject: str) -> str:
    """Compose the capability's description from a provider's noun phrase.

    The sentence is WCC's; the noun phrase is the provider's. That split is the
    whole defence against a page laundering an instruction into text an agent
    reads as this project's own, so the composition lives here rather than in a
    format string at the call site where a later edit could widen it.
    """
    return f"{template.category.capitalize()} {subject}."


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
            "description": describe(template, enrichment.subject),
            "parameters": _annotated(template.parameters, enrichment),
            "risk": _raised(template.risk, enrichment),
        }
    )
    # ``model_copy`` does not re-run validators, so the result is revalidated
    # rather than trusted: an update that produced an invalid template would
    # otherwise be persisted and only fail when something read it back.
    revalidated = CapabilityTemplate.model_validate(updated.model_dump())
    _only_allowed_changes(template, revalidated)
    return revalidated


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
        rewritten[parameter.name] = {**existing, "description": parameter.subject}

    return {**parameters, "properties": rewritten}


def _raised(risk: Risk, enrichment: CapabilityEnrichment) -> Risk:
    """Take the higher of the deterministic level and the hint.

    Rebuilt through the constructor rather than copied, so :class:`Risk`'s own
    floor validator runs on the result: the level may rise above what the side
    effect implies, and can never be argued below it.
    """
    hint = enrichment.risk_hint
    if hint is None or risk_order(hint) <= risk_order(risk.level):
        return risk
    return Risk(
        level=hint,
        side_effect=risk.side_effect,
        reversible=risk.reversible,
        requires_confirmation=risk.requires_confirmation,
    )


def _only_allowed_changes(before: CapabilityTemplate, after: CapabilityTemplate) -> None:
    """Diff the whole document and permit exactly the paths enrichment owns."""
    original = before.model_dump()
    result = after.model_dump()

    moved = sorted(
        key
        for key in set(original) | set(result)
        if original.get(key) != result.get(key) and key not in ALLOWED_PATHS
    )
    if moved:
        raise EnrichmentRejected(f"enrichment altered {moved}, which it may never do")

    _only_descriptions_changed(original.get("parameters"), result.get("parameters"))
    _only_the_level_rose(before.risk, after.risk)


def _only_descriptions_changed(original: Any, result: Any) -> None:
    """Every part of the parameter schema is fixed except property descriptions."""
    if original == result:
        return
    if not isinstance(original, dict) or not isinstance(result, dict):
        raise EnrichmentRejected("enrichment replaced the parameter schema")

    if set(original) != set(result):
        raise EnrichmentRejected("enrichment changed the keys of the parameter schema")
    for key in original:
        if key == "properties":
            continue
        if original[key] != result[key]:
            raise EnrichmentRejected(f"enrichment changed parameters.{key}")

    before_properties, after_properties = original.get("properties"), result.get("properties")
    if not isinstance(before_properties, dict) or not isinstance(after_properties, dict):
        raise EnrichmentRejected("enrichment replaced the parameter properties")
    if set(before_properties) != set(after_properties):
        raise EnrichmentRejected("enrichment added or removed a parameter")

    for name, schema in before_properties.items():
        other = after_properties[name]
        if not isinstance(schema, dict) or not isinstance(other, dict):
            raise EnrichmentRejected(f"enrichment replaced the schema of {name!r}")
        if set(schema) - {"description"} != set(other) - {"description"}:
            raise EnrichmentRejected(f"enrichment changed the shape of {name!r}")
        for key in set(schema) | set(other):
            if key != "description" and schema.get(key) != other.get(key):
                raise EnrichmentRejected(f"enrichment changed {name}.{key}")


def _only_the_level_rose(before: Risk, after: Risk) -> None:
    if risk_order(after.level) < risk_order(before.level):
        raise EnrichmentRejected("enrichment lowered a deterministic risk classification")
    if (
        after.side_effect is not before.side_effect
        or after.reversible != before.reversible
        or after.requires_confirmation != before.requires_confirmation
    ):
        raise EnrichmentRejected("enrichment changed a risk field other than the level")
