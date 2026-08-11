"""The reduced structured payload a provider is allowed to see (specification 9.3).

The specification's sentence is that the LLM "receives a reduced structured
payload, never the full raw DOM". This module is the whole of that promise, so it
is written as a positive list rather than as filtering: a summary is *constructed*
from named fields, and anything not named here cannot reach a provider no matter
what a later edit adds to a template.

What is deliberately absent, and why:

**Locator chains, structural paths, element IDs.** Specification 10.3 forbids
handing selectors to an agent, and a provider is an agent with a network
connection. A model that never sees a selector cannot return one.

**Values.** Labels describe controls; the text a user typed into them describes a
person. Specification 18 redacts before persistence, and this is a stricter
boundary than persistence.

**The page's body text.** Page type is inferable from the route and the shape of
what was compiled. Shipping the article someone was reading in order to name a
search box better is not a trade this project makes.

**The page title.** It was sent in the first version, and a counter-review was
right to object. A title is free text a site chooses, and on a page behind a login
it is routinely the most identifying string on it — ``Alice Smith medical record
search``. It bought a marginally better page-type guess and cost the one field
most likely to carry a person's name.

What remains, exactly, is: the origin, the normalized route pattern, and per
capability its generated name, category, risk level, side effect, and its
parameters' names, types, and labels. That list is the contract, it is repeated in
the README, and this module constructs it field by field so nothing joins it by
being added to a template.

Two of those are page-authored and cannot be otherwise: parameter labels are what
naming is *for*, and the route pattern normalizes digits and UUIDs but not a
tenant slug. Anyone enabling enrichment on an authenticated site is sending those
to a third party, which is why it is off by default and why the README says so in
those words rather than in reassuring ones.
"""

from __future__ import annotations

from pydantic import Field

from wcc.models.capability import CapabilityTemplate
from wcc.models.common import WCCModel
from wcc.models.session import PageSnapshot


class ParameterSummary(WCCModel):
    """One argument of one candidate, as a provider sees it.

    ``name`` is shown but is not up for renaming. It is the key of the JSON Schema
    property, the ``target_ref`` of a plan step, and the key of a locator chain all
    at once, so changing it is changing the execution plan — which Phase 6's
    acceptance forbids. A provider may say what the parameter *means*; it may not
    say what it is called.
    """

    name: str
    type: str
    label: str | None = None


class CandidateSummary(WCCModel):
    """One compiled capability, reduced to what naming it requires."""

    ref: str
    """A payload-local handle, positional and meaningless outside this request.

    Not the template ID. A provider that learns a template ID learns a durable
    name for a stored object, and there is no reason for it to hold one.
    """

    name: str
    category: str
    risk_level: str
    side_effect: str
    parameters: list[ParameterSummary] = Field(default_factory=list)


class EnrichmentRequest(WCCModel):
    """Everything a provider is given about a page. The whole list, not a subset."""

    origin: str
    route_pattern: str
    candidates: list[CandidateSummary] = Field(min_length=1)


def build_request(
    snapshot: PageSnapshot, templates: list[CapabilityTemplate]
) -> EnrichmentRequest | None:
    """Reduce a compilation to a request, or return ``None`` if there is nothing to ask.

    A page that compiled to no capabilities has no naming question, and asking one
    anyway would spend a model call to be told so.
    """
    if not templates:
        return None
    return EnrichmentRequest(
        origin=snapshot.origin,
        route_pattern=snapshot.normalized_route_pattern,
        candidates=[
            CandidateSummary(
                ref=f"c{index}",
                name=template.name,
                category=str(template.category),
                risk_level=str(template.risk.level),
                side_effect=str(template.risk.side_effect),
                parameters=_parameters(template),
            )
            for index, template in enumerate(templates, start=1)
        ],
    )


def refs_to_templates(templates: list[CapabilityTemplate]) -> dict[str, CapabilityTemplate]:
    """The inverse of the ``ref`` assignment made by :func:`build_request`."""
    return {f"c{index}": template for index, template in enumerate(templates, start=1)}


def _parameters(template: CapabilityTemplate) -> list[ParameterSummary]:
    properties = template.parameters.get("properties")
    if not isinstance(properties, dict):
        return []
    summaries: list[ParameterSummary] = []
    for name, schema in sorted(properties.items()):
        if not isinstance(schema, dict):
            continue
        declared = schema.get("type")
        described = schema.get("description")
        summaries.append(
            ParameterSummary(
                name=str(name),
                type=str(declared) if isinstance(declared, str) else "string",
                label=described if isinstance(described, str) else None,
            )
        )
    return summaries
