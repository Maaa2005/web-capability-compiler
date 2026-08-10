"""Optional semantic enrichment (specification 9.3, Phase 6).

The balance of these tests is deliberate: a handful cover the happy path, and the
rest cover a provider behaving badly. That is the right proportion because the
feature's value is small and its blast radius is not. A better capability name is
worth having; a model that can rewrite an execution plan, lower a risk
classification, or hand a selector to an agent is worth never having.

Nothing here reaches a network. The provider is a Protocol, so a test provider is
a class with one method, and "what happens when the model says X" is expressed by
writing X.
"""

from __future__ import annotations

import json

import pytest
from pydantic import ValidationError

from factories import a_snapshot, a_template
from wcc.enrich.apply import PROTECTED_FIELDS, EnrichmentRejected, apply_enrichment
from wcc.enrich.enricher import Enricher
from wcc.enrich.models import CapabilityEnrichment, PageEnrichment, ParameterEnrichment
from wcc.enrich.payload import build_request, refs_to_templates
from wcc.enrich.provider import EnrichmentUnavailable, parse_response
from wcc.models.capability import CapabilityTemplate, Risk, RiskLevel, SideEffect


class StubProvider:
    """A provider that says whatever the test needs it to say."""

    def __init__(self, response: PageEnrichment | Exception) -> None:
        self._response = response
        self.calls = 0

    async def enrich(self, request: object) -> PageEnrichment:
        self.calls += 1
        if isinstance(self._response, Exception):
            raise self._response
        return self._response


class DictCache:
    def __init__(self) -> None:
        self.entries: dict[str, PageEnrichment] = {}

    def load_enrichment(self, page_signature: str) -> PageEnrichment | None:
        return self.entries.get(page_signature)

    def save_enrichment(self, page_signature: str, enrichment: PageEnrichment) -> None:
        self.entries[page_signature] = enrichment


def a_response(**overrides: object) -> PageEnrichment:
    capability: dict[str, object] = {
        "ref": "c1",
        "name": "search_products",
        "description": "Search the catalogue for products matching a keyword.",
    }
    capability.update(overrides)
    return PageEnrichment(
        page_type="product search",
        capabilities=[CapabilityEnrichment(**capability)],  # type: ignore[arg-type]
    )


# -- the payload ----------------------------------------------------------


def test_the_payload_carries_no_locator_of_any_kind() -> None:
    """Specification 9.3: reduced structured payload, never the raw DOM.

    Asserted over the serialized request rather than field by field, for the
    reason Phase 5 learned the hard way: a field added later is covered by this
    test and would not be covered by a list of field names.

    Parameter names are the deliberate exception, and the distinction is worth
    stating because it is the one this whole package turns on. An argument name
    must cross every boundary — it is the key a caller supplies — so the fixture's
    ``keyword`` appears here legitimately even though the same string happens to be
    the input's ``id``. What must not cross is a way to *reach* an element: the
    ``target_ref`` names the plan uses, the keys of chains that are not parameters,
    and anything shaped like a selector.
    """
    template = a_template()
    request = build_request(a_snapshot(), [template])
    assert request is not None

    serialized = request.model_dump_json()
    # ``[`` is left out on purpose: it is JSON's own punctuation here, so testing
    # for it would test the serializer rather than the payload.
    for locator in ("search-button", "search_input", "search_submit", "#", ">"):
        assert locator not in serialized, f"{locator!r} reached the provider payload"
    assert "tmpl_1" not in serialized, "a template ID is a durable name a provider need not hold"


def test_a_page_with_nothing_compiled_produces_no_request() -> None:
    assert build_request(a_snapshot(), []) is None


# -- rejecting malformed output -------------------------------------------


@pytest.mark.parametrize(
    "name",
    ["SearchProducts", "search products", "products", "_search", "search__products", ""],
)
def test_a_name_that_is_not_a_verb_led_snake_case_name_is_rejected(name: str) -> None:
    """Specification 9.2, enforced on the way in rather than hoped for."""
    with pytest.raises(ValidationError):
        a_response(name=name)


def test_a_description_carrying_selector_punctuation_is_rejected() -> None:
    """The Phase 3 leak, in the one place a model could reintroduce it."""
    with pytest.raises(ValidationError, match="selector punctuation"):
        a_response(description="Search using the #product-search form.")


def test_two_capabilities_may_not_be_given_one_name() -> None:
    with pytest.raises(ValidationError, match="same name"):
        PageEnrichment(
            page_type="search",
            capabilities=[
                CapabilityEnrichment(ref="c1", name="search_products", description="One."),
                CapabilityEnrichment(ref="c2", name="search_products", description="Two."),
            ],
        )


def test_an_unknown_field_in_the_response_is_rejected_not_ignored() -> None:
    payload = {
        "page_type": "search",
        "capabilities": [
            {
                "ref": "c1",
                "name": "search_products",
                "description": "Search.",
                "execution_plan_template": [],
            }
        ],
    }
    with pytest.raises(ValidationError):
        parse_response(json.dumps(payload))


def test_output_that_is_not_json_is_rejected_and_not_repaired() -> None:
    """Specification 9.3: invalid output is rejected, not repaired.

    The fenced block is the realistic failure — a model prefacing its JSON — and
    the point of the test is that nothing here goes looking for the first brace.
    """
    with pytest.raises(EnrichmentUnavailable, match="did not return JSON"):
        parse_response('```json\n{"page_type": "search", "capabilities": []}\n```')


# -- what enrichment may never do -----------------------------------------


def test_enrichment_cannot_alter_a_locator_chain_or_an_execution_plan() -> None:
    """Phase 6 acceptance, stated as the invariant it is."""
    template = a_template()
    enriched = apply_enrichment([template], a_response(), refs_to_templates([template]))[0]

    assert enriched.name == "search_products"
    for field in PROTECTED_FIELDS:
        assert getattr(enriched, field) == getattr(template, field), f"{field} moved"


def test_a_risk_hint_below_the_deterministic_level_is_ignored() -> None:
    """Phase 6 acceptance: enrichment cannot lower deterministic risk."""
    risky = a_template(
        risk=Risk(
            level=RiskLevel.HIGH,
            side_effect=SideEffect.PERSISTENT,
            reversible=False,
            requires_confirmation=True,
        )
    )
    enriched = apply_enrichment(
        [risky], a_response(risk_hint=RiskLevel.LOW), refs_to_templates([risky])
    )[0]

    assert enriched.risk == risky.risk


def test_a_risk_hint_above_the_deterministic_level_raises_it() -> None:
    """Specification 10.1: uncertainty raises and never lowers, so a hint may."""
    template = a_template()
    enriched = apply_enrichment(
        [template], a_response(risk_hint=RiskLevel.HIGH), refs_to_templates([template])
    )[0]

    assert enriched.risk.level is RiskLevel.HIGH
    assert enriched.risk.side_effect is template.risk.side_effect


def test_a_parameter_description_is_rewritten_and_the_schema_shape_is_not() -> None:
    template = a_template()
    enriched = apply_enrichment(
        [template],
        a_response(
            parameters=[
                ParameterEnrichment(name="keyword", description="Words to match against a product.")
            ]
        ),
        refs_to_templates([template]),
    )[0]

    properties = enriched.parameters["properties"]
    assert isinstance(properties, dict)
    assert properties["keyword"] == {
        "type": "string",
        "description": "Words to match against a product.",
    }
    assert enriched.parameters["required"] == ["keyword"]
    assert enriched.parameters["additionalProperties"] is False


def test_a_response_naming_a_parameter_that_does_not_exist_is_refused() -> None:
    template = a_template()
    with pytest.raises(EnrichmentRejected, match="not a parameter"):
        apply_enrichment(
            [template],
            a_response(parameters=[ParameterEnrichment(name="sort_by", description="Order.")]),
            refs_to_templates([template]),
        )


def test_a_response_naming_a_candidate_that_was_never_sent_is_refused() -> None:
    template = a_template()
    with pytest.raises(EnrichmentRejected, match="not one of the candidates"):
        apply_enrichment([template], a_response(ref="c9"), refs_to_templates([template]))


def test_a_name_colliding_with_one_the_compiler_kept_is_refused() -> None:
    """A page cannot offer two capabilities under one name, however it got there."""
    first = a_template(template_id="tmpl_1", name="open_result_item")
    second = a_template(template_id="tmpl_2", name="search_products")
    response = PageEnrichment(
        page_type="search",
        capabilities=[
            CapabilityEnrichment(ref="c1", name="search_products", description="Search.")
        ],
    )

    with pytest.raises(EnrichmentRejected, match="cannot offer two capabilities"):
        apply_enrichment([first, second], response, refs_to_templates([first, second]))


# -- the enricher: never a source of failure ------------------------------


async def test_the_system_works_with_no_provider_configured() -> None:
    """Phase 6 acceptance: the full system works with the LLM disabled."""
    templates = [a_template()]
    enricher = Enricher()

    assert not enricher.enabled
    assert await enricher.enrich(a_snapshot(), templates) == templates


@pytest.mark.parametrize(
    "failure",
    [
        EnrichmentUnavailable("the provider is unreachable"),
        TimeoutError("the provider did not answer"),
        ValueError("something nobody predicted"),
    ],
)
async def test_a_failing_provider_leaves_the_deterministic_output_usable(
    failure: Exception,
) -> None:
    """Phase 6 acceptance: deterministic output remains usable on invalid output."""
    templates = [a_template()]
    enricher = Enricher(StubProvider(failure))

    result = await enricher.enrich(a_snapshot(), templates)

    assert result == templates
    assert enricher.last_error is not None, "a swallowed failure must still be visible"


async def test_a_rejected_response_leaves_the_deterministic_output_usable() -> None:
    """A response that parses but breaks a rule is a failure of enrichment only."""
    templates = [a_template()]
    enricher = Enricher(StubProvider(a_response(ref="c9")))

    assert await enricher.enrich(a_snapshot(), templates) == templates
    assert enricher.last_error is not None
    assert "EnrichmentRejected" in enricher.last_error


async def test_an_applied_response_replaces_the_generated_name() -> None:
    templates = [a_template(name="submit_search_form")]
    enricher = Enricher(StubProvider(a_response()))

    enriched = await enricher.enrich(a_snapshot(), templates)

    assert enriched[0].name == "search_products"
    assert enriched[0].description == "Search the catalogue for products matching a keyword."


# -- the cache ------------------------------------------------------------


async def test_a_second_visit_to_one_page_class_costs_no_second_call() -> None:
    """Phase 6 task: cache by page signature."""
    templates = [a_template()]
    provider = StubProvider(a_response())
    cache = DictCache()
    enricher = Enricher(provider, cache=cache)

    first = await enricher.enrich(a_snapshot(), templates)
    second = await enricher.enrich(a_snapshot(snapshot_id="snap_2"), templates)

    assert provider.calls == 1
    assert first[0].name == second[0].name == "search_products"


async def test_a_rejected_response_is_not_cached() -> None:
    """Caching a refusal would replay it forever and look like a working cache."""
    templates = [a_template()]
    cache = DictCache()
    enricher = Enricher(StubProvider(a_response(ref="c9")), cache=cache)

    await enricher.enrich(a_snapshot(), templates)

    assert cache.entries == {}


async def test_a_different_page_class_is_a_different_question() -> None:
    templates = [a_template()]
    provider = StubProvider(a_response())
    enricher = Enricher(provider, cache=DictCache())

    await enricher.enrich(a_snapshot(), templates)
    await enricher.enrich(a_snapshot(page_signature="sha256:page-class-two"), templates)

    assert provider.calls == 2


def test_the_store_is_a_usable_enrichment_cache(tmp_path: object) -> None:
    """The Protocol is satisfied by the real store, not only by the test double."""
    from wcc.store.store import Store

    with Store(tmp_path / "wcc.db") as store:  # type: ignore[operator]
        assert store.load_enrichment("sha256:page-class-one") is None
        store.save_enrichment("sha256:page-class-one", a_response())
        loaded = store.load_enrichment("sha256:page-class-one")

    assert loaded == a_response()


def test_the_schema_sent_to_the_provider_matches_the_model_that_validates_it() -> None:
    """Two statements of one contract must not drift apart.

    ``RESPONSE_SCHEMA`` constrains generation and :class:`PageEnrichment` decides
    what is accepted. If the schema permits a field the model forbids, every
    response is rejected after being paid for; if it omits one the model requires,
    every response is rejected before being read. Neither failure is visible from
    either file alone.
    """
    from wcc.enrich.provider import RESPONSE_SCHEMA

    capability = RESPONSE_SCHEMA["properties"]["capabilities"]["items"]
    assert set(capability["properties"]) == set(CapabilityEnrichment.model_fields)
    assert set(RESPONSE_SCHEMA["properties"]) == set(PageEnrichment.model_fields)

    parameter = capability["properties"]["parameters"]["items"]
    assert set(parameter["properties"]) == set(ParameterEnrichment.model_fields)

    # A response containing every field the schema allows must validate, which is
    # the direction that costs money to get wrong.
    PageEnrichment.model_validate(
        {
            "page_type": "product search",
            "capabilities": [
                {
                    "ref": "c1",
                    "name": "search_products",
                    "description": "Search the catalogue.",
                    "parameters": [{"name": "keyword", "description": "A word to match."}],
                    "risk_hint": "medium",
                }
            ],
        }
    )


# -- configuration --------------------------------------------------------


@pytest.mark.parametrize("value", ["", "0", "false", "no", "off", "yes please", "TRUE "])
def test_enrichment_is_off_unless_the_variable_says_exactly_yes(value: str) -> None:
    """The same fail-closed reading the private-network flag gets.

    ``"TRUE "`` is in the list on purpose: it is stripped and lowercased, so it
    means yes. ``"yes please"`` is not, and a setting that opened on an
    unrecognised value would open on a typo.
    """
    from wcc.config import ENRICHMENT_ENV, enrichment_enabled

    expected = value.strip().lower() in {"1", "true", "yes", "on"}
    assert enrichment_enabled({ENRICHMENT_ENV: value}) is expected


def test_no_variable_at_all_means_off() -> None:
    """Phase 6 acceptance, at the layer that decides: opt in, never opt out."""
    from wcc.config import build_enricher, enrichment_enabled

    assert enrichment_enabled({}) is False
    assert not build_enricher(environ={}).enabled


def test_a_configured_provider_that_cannot_be_built_yields_an_inert_enricher() -> None:
    """A missing key must not stop a server whose pipeline never needed a model."""
    from wcc.config import ENRICHMENT_ENV, build_enricher

    assert not build_enricher(environ={ENRICHMENT_ENV: "1", "ANTHROPIC_API_KEY": ""}).enabled


def test_templates_are_returned_unchanged_when_nothing_matches() -> None:
    """An empty response is valid and means "no better name occurred to me"."""
    template: CapabilityTemplate = a_template()
    empty = PageEnrichment(page_type="search", capabilities=[])

    assert apply_enrichment([template], empty, refs_to_templates([template])) == [template]
