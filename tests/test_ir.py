"""Agent Page IR: what it must carry, and when two readings mean the same."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from factories import a_collection, a_snapshot, a_template, an_ir
from wcc.ir import build_page_ir, canonical_projection, semantically_equivalent
from wcc.models.capability import (
    CapabilityCategory,
    CapabilityTemplate,
    Risk,
    RiskLevel,
    SideEffect,
)
from wcc.models.common import Confidence
from wcc.models.ir import Entity, Evidence

_RECORDS = [
    ("rec_1", {"title": "Wireless Earbuds", "price_text": "4,980 yen"}, ["el_1", "el_2"]),
    ("rec_2", {"title": "USB-C Cable", "price_text": "980 yen"}, ["el_3"]),
]


# -- evidence is not optional ---------------------------------------------------


def test_an_entity_without_evidence_cannot_be_built() -> None:
    """Phase 2 acceptance: every entity has evidence."""
    with pytest.raises(ValidationError):
        Entity(entity_id="entity_1", type="list_item", evidence=[])


def test_evidence_pointing_at_nothing_is_refused() -> None:
    """Otherwise 'has evidence' passes while nothing is actually supported."""
    with pytest.raises(ValidationError):
        Evidence(kind="dom")


def test_a_template_without_evidence_cannot_be_built() -> None:
    with pytest.raises(ValidationError):
        a_template(evidence=[])


# -- templates are checkable before they are stored -----------------------------


def test_a_plan_step_without_a_locator_is_refused() -> None:
    """A plan that cannot locate its target would fail mid-sequence instead."""
    with pytest.raises(ValidationError) as refused:
        a_template(target_locators={"search_input": a_template().target_locators["search_input"]})
    assert "missing locators for ['search_submit']" in str(refused.value)


def test_an_unused_locator_is_refused() -> None:
    from factories import a_chain

    with pytest.raises(ValidationError) as refused:
        a_template(
            target_locators={
                **a_template().target_locators,
                "orphan": a_chain("nobody-refers-to-me"),
            }
        )
    assert "unused locators for ['orphan']" in str(refused.value)


def test_every_target_is_required_in_v0_1() -> None:
    template = a_template()
    assert template.required_targets() == frozenset({"search_input", "search_submit"})


# -- building -------------------------------------------------------------------


def test_records_become_entities_carrying_their_evidence() -> None:
    snapshot = a_snapshot(collections=[a_collection(_RECORDS)])
    ir = build_page_ir(snapshot)

    assert [entity.label for entity in ir.entities] == ["USB-C Cable", "Wireless Earbuds"] or [
        entity.label for entity in ir.entities
    ] == ["Wireless Earbuds", "USB-C Cable"]
    assert all(entity.evidence for entity in ir.entities)
    earbuds = next(entity for entity in ir.entities if entity.label == "Wireless Earbuds")
    assert [item.element_id for item in earbuds.evidence] == ["el_1", "el_2"]


def test_a_record_with_no_fields_is_reported_rather_than_dropped() -> None:
    """An agent seeing fewer results than the page shows should be told why."""
    snapshot = a_snapshot(collections=[a_collection([("rec_1", {}, ["el_1"])])])
    ir = build_page_ir(snapshot)

    assert ir.entities == []
    assert [warning.code for warning in ir.warnings] == ["record_without_fields"]


def test_a_truncated_collection_is_reported() -> None:
    snapshot = a_snapshot(collections=[a_collection(_RECORDS, truncated=True, record_count=200)])
    ir = build_page_ir(snapshot)
    assert any(warning.code == "collection_truncated" for warning in ir.warnings)


def test_snapshot_limit_warnings_survive_into_the_ir() -> None:
    snapshot = a_snapshot(warnings=["kept 50 of 900 collections (specification 10.5 limit)"])
    ir = build_page_ir(snapshot)
    assert [warning.code for warning in ir.warnings] == ["snapshot_limit"]


def test_a_record_without_element_evidence_still_records_where_it_came_from() -> None:
    snapshot = a_snapshot(collections=[a_collection([("rec_1", {"title": "Only JSON-LD"}, [])])])
    ir = build_page_ir(snapshot)

    (entity,) = ir.entities
    assert entity.evidence[0].kind == "collection"
    assert entity.evidence[0].collection_id == "coll_1"


# -- determinism ----------------------------------------------------------------


def test_two_readings_of_an_unchanged_page_are_semantically_equivalent() -> None:
    """Phase 2 acceptance. Plain equality cannot say this: the IDs differ."""
    snapshot = a_snapshot(collections=[a_collection(_RECORDS)])
    first = build_page_ir(snapshot)
    second = build_page_ir(snapshot)

    assert first.ir_id != second.ir_id
    assert first != second
    assert semantically_equivalent(first, second)


def test_the_order_records_arrive_in_does_not_change_the_meaning() -> None:
    forwards = a_snapshot(collections=[a_collection(_RECORDS)])
    backwards = a_snapshot(collections=[a_collection(list(reversed(_RECORDS)))])

    assert semantically_equivalent(build_page_ir(forwards), build_page_ir(backwards))


def test_a_changed_page_is_not_equivalent() -> None:
    before = a_snapshot(collections=[a_collection(_RECORDS)])
    after = a_snapshot(
        collections=[a_collection([("rec_1", {"title": "Something else"}, ["el_1"])])]
    )
    assert not semantically_equivalent(build_page_ir(before), build_page_ir(after))


def test_evidence_that_came_unhooked_is_not_equivalent() -> None:
    """Dropping IDs entirely would hide this; renumbering them keeps the structure."""
    snapshot = a_snapshot(collections=[a_collection(_RECORDS)])
    correct = build_page_ir(snapshot)

    broken = correct.model_copy(
        update={
            "entities": [
                correct.entities[0].model_copy(
                    update={
                        "evidence": [
                            Evidence(
                                kind="dom", element_id="el_elsewhere", attribute="text_content"
                            )
                        ]
                    }
                ),
                *correct.entities[1:],
            ]
        }
    )
    assert not semantically_equivalent(correct, broken)


def test_two_entities_citing_one_element_keep_that_relationship() -> None:
    shared = a_snapshot(
        collections=[
            a_collection(
                [
                    ("rec_1", {"title": "A"}, ["el_shared"]),
                    ("rec_2", {"title": "B"}, ["el_shared"]),
                ]
            )
        ]
    )
    projection = canonical_projection(build_page_ir(shared))
    cited = [entity["evidence"][0]["element"] for entity in projection["entities"]]
    assert cited[0] == cited[1]


def test_the_projection_drops_which_reading_it_is() -> None:
    snapshot = a_snapshot()
    one = an_ir(snapshot, ir_id="ir_a")
    other = an_ir(snapshot, ir_id="ir_b", session_id="sess_other")
    assert canonical_projection(one) == canonical_projection(other)


def test_a_warning_reworded_is_still_the_same_warning() -> None:
    """Codes are the contract; prose is written for a person."""
    from wcc.models.ir import IRWarning

    snapshot = a_snapshot()
    one = an_ir(snapshot, warnings=[IRWarning(code="collection_truncated", message="kept 1 of 9")])
    other = an_ir(
        snapshot, warnings=[IRWarning(code="collection_truncated", message="1 of 9 records kept")]
    )
    assert semantically_equivalent(one, other)


# -- what the counter-review found ---------------------------------------------


def test_only_the_three_categories_the_specification_defines_are_accepted() -> None:
    """Specification 3.1: search, filter, navigate. Paging is navigate, not its own."""
    from pydantic import ValidationError as _VE

    assert {c.value for c in CapabilityCategory} == {"search", "filter", "navigate"}
    with pytest.raises(_VE):
        a_template(category="paginate")


def test_a_low_confidence_candidate_never_becomes_a_template() -> None:
    """Specification 7.4: low means placeholder-only or positional inference."""
    with pytest.raises(ValidationError) as refused:
        a_template(confidence=Confidence.LOW)
    assert "specification 7.4" in str(refused.value)


def test_confidence_has_no_default_on_a_template() -> None:
    """Inheriting a claim about evidence is how a guess becomes a stored fact."""
    fields = dict(a_template().model_dump(mode="python"))
    fields.pop("confidence")
    with pytest.raises(ValidationError):
        CapabilityTemplate(**fields)


def test_a_risk_level_below_its_side_effect_is_refused() -> None:
    """Specification 10.1: uncertainty raises the level and never lowers it."""
    with pytest.raises(ValidationError) as refused:
        Risk(
            level=RiskLevel.LOW,
            side_effect=SideEffect.DESTRUCTIVE_OR_FINANCIAL,
            reversible=False,
            requires_confirmation=True,
        )
    assert "at least critical risk" in str(refused.value)


def test_a_risk_level_above_its_side_effect_is_allowed() -> None:
    """A classifier with a further reason for concern may raise; it may not lower."""
    cautious = Risk(
        level=RiskLevel.HIGH,
        side_effect=SideEffect.LOCAL,
        reversible=True,
        requires_confirmation=True,
    )
    assert cautious.level is RiskLevel.HIGH
