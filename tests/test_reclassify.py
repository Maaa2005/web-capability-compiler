"""Risk re-classification at binding time (specification 7.10 step 4)."""

from __future__ import annotations

from typing import Any

from factories import a_chain, a_path_chain, a_snapshot, a_submit, a_template, an_element
from wcc.compiler.reclassify import SnapshotRiskClassifier, current_targets
from wcc.compiler.risk import names_destructive_act
from wcc.models.capability import CapabilityCategory, CapabilityTemplate, PlanStep, RiskLevel
from wcc.models.common import ActionType, ControlType
from wcc.models.element import NormalizedElement
from wcc.models.locator import LocatorChain
from wcc.models.session import PageSnapshot

CLASSIFIER = SnapshotRiskClassifier()


def a_search_page(**overrides: Any) -> PageSnapshot:
    elements = [
        an_element("el_input", locator_chain=a_chain("keyword"), **overrides),
        a_submit("el_submit", locator_chain=a_chain("search-button"), **overrides),
    ]
    return a_snapshot(elements=elements)


def test_a_form_still_submitting_by_get_stays_low() -> None:
    assert CLASSIFIER.classify(a_template(), a_search_page()).level is RiskLevel.LOW


def test_a_form_that_became_a_post_reads_high() -> None:
    risen = a_search_page(form_method="post")
    assert CLASSIFIER.classify(a_template(), risen).level is RiskLevel.HIGH


def test_a_target_that_is_no_longer_on_the_page_reads_high() -> None:
    """Uncertainty raises: "it is gone, so it is harmless" is the inverted reading."""
    page = a_snapshot(elements=[an_element("el_input", locator_chain=a_chain("keyword"))])
    assert current_targets(a_template(), page) is None
    assert CLASSIFIER.classify(a_template(), page).level is RiskLevel.HIGH


def _list_template(members_ref: str = "result_collection") -> CapabilityTemplate:
    return a_template(
        name="open_result_item",
        category=CapabilityCategory.NAVIGATE,
        parameters={
            "type": "object",
            "properties": {"entity_id": {"type": "string"}},
            "required": ["entity_id"],
            "additionalProperties": False,
        },
        execution_plan_template=[
            PlanStep(action=ActionType.ACTIVATE, target_ref=members_ref, parameter="entity_id")
        ],
        target_locators={members_ref: a_path_chain("main>ul")},
    )


def _a_link(element_id: str, label: str, path: str) -> NormalizedElement:
    return an_element(
        element_id,
        tag_name="a",
        control_type=ControlType.LINK,
        resolved_label=label,
        control_name=None,
        form_id=None,
        form_key=None,
        form_method=None,
        form_action_route=None,
        form_action_kind=None,
        locator_chain=a_path_chain(path),
    )


def test_a_container_is_classified_through_the_members_inside_it() -> None:
    page = a_snapshot(
        elements=[
            _a_link("el_1", "Wireless Earbuds", "main>ul>li[1]>a"),
            _a_link("el_2", "Bluetooth Speaker", "main>ul>li[2]>a"),
        ]
    )
    assert CLASSIFIER.classify(_list_template(), page).level is RiskLevel.LOW


def test_one_destructive_member_decides_the_whole_list() -> None:
    """A "Delete" link at position two is not made safe by the product above it."""
    page = a_snapshot(
        elements=[
            _a_link("el_1", "Wireless Earbuds", "main>ul>li[1]>a"),
            _a_link("el_2", "Delete this order", "main>ul>li[2]>a"),
        ]
    )
    assert CLASSIFIER.classify(_list_template(), page).level is RiskLevel.CRITICAL


def test_members_that_disagree_about_what_they_do_refuse_to_be_summarized() -> None:
    page = a_snapshot(
        elements=[
            _a_link("el_1", "Wireless Earbuds", "main>ul>li[1]>a"),
            an_element(
                "el_2",
                tag_name="button",
                control_type=ControlType.BUTTON,
                resolved_label="Buy now",
                submits_form=True,
                form_method="post",
                locator_chain=a_path_chain("main>ul>li[2]>button"),
            ),
        ]
    )
    assert CLASSIFIER.classify(_list_template(), page).level is RiskLevel.HIGH


def test_a_product_name_containing_a_dangerous_word_is_not_a_dangerous_act() -> None:
    """The regression that made a list of headphones read as destructive."""
    assert not names_destructive_act("Noise Cancelling Headphones")
    assert not names_destructive_act("Buyer's guide")
    assert not names_destructive_act("Reorder history")
    assert names_destructive_act("Cancel subscription")
    assert names_destructive_act("Delete")
    assert names_destructive_act("注文を確定")


def test_an_empty_chain_matches_nothing_rather_than_everything() -> None:
    template = a_template(
        target_locators={
            "search_input": LocatorChain.model_validate(a_chain("keyword").model_dump()),
            "search_submit": a_chain("gone"),
        }
    )
    assert current_targets(template, a_search_page()) is None
