"""Deterministic risk classification (specification 10.1), without a browser."""

from __future__ import annotations

import pytest

from factories import a_chain
from wcc.compiler.risk import classify_plan
from wcc.models.capability import CapabilityCategory, PlanStep, RiskLevel, SideEffect
from wcc.models.common import ActionType, ControlType, FormActionKind
from wcc.models.element import NormalizedElement


def _element(**overrides: object) -> NormalizedElement:
    fields: dict[str, object] = {
        "element_id": "el_1",
        "tag_name": "button",
        "control_type": ControlType.BUTTON,
        "locator_chain": a_chain("submit"),
    }
    fields.update(overrides)
    return NormalizedElement(**fields)  # type: ignore[arg-type]


_ACTIVATE = [PlanStep(action=ActionType.ACTIVATE, target_ref="submit")]


def _classify(element: NormalizedElement, category: CapabilityCategory = CapabilityCategory.SEARCH):
    return classify_plan(category, _ACTIVATE, {"submit": element})


def test_a_get_search_submission_is_low() -> None:
    """One of specification 10.1's named deterministic low signals."""
    risk, why = _classify(_element(submits_form=True, form_method="get", resolved_label="Search"))
    assert risk.level is RiskLevel.LOW
    assert risk.side_effect is SideEffect.NONE
    assert "GET" in why or "reads without changing state" in why


def test_a_pagination_link_is_low() -> None:
    risk, _ = _classify(
        _element(control_type=ControlType.LINK, resolved_label="2", is_pagination_control=True),
        CapabilityCategory.NAVIGATE,
    )
    assert risk.level is RiskLevel.LOW


def test_every_post_submission_is_high() -> None:
    """Specification 10.1 admits no exception, including a 'read-only POST search'."""
    risk, why = _classify(_element(submits_form=True, form_method="post", resolved_label="Search"))
    assert risk.level is RiskLevel.HIGH
    assert risk.side_effect is SideEffect.PERSISTENT
    assert "POST" in why


def test_a_post_search_gets_no_exception_for_looking_like_a_search() -> None:
    """The tempting special case the specification names and forbids."""
    risk, _ = _classify(
        _element(submits_form=True, form_method="post", resolved_label="Search products"),
        CapabilityCategory.SEARCH,
    )
    assert risk.level is not RiskLevel.LOW


@pytest.mark.parametrize(
    "label",
    ["Delete account", "Cancel order", "Pay now", "Transfer funds", "削除する", "退会"],
)
def test_a_destructive_label_raises_above_its_method(label: str) -> None:
    """A GET form is a low signal; a button that says 'delete' is not a search."""
    risk, why = _classify(_element(submits_form=True, form_method="get", resolved_label=label))
    assert risk.level is RiskLevel.CRITICAL
    assert risk.side_effect is SideEffect.DESTRUCTIVE_OR_FINANCIAL
    assert label in why


def test_subscribing_is_persistent_rather_than_destructive() -> None:
    """Both refuse execution; only one of them reports the right reason."""
    risk, why = _classify(
        _element(submits_form=True, form_method="post", resolved_label="Subscribe")
    )
    assert risk.side_effect is SideEffect.PERSISTENT
    assert "destructive" not in why


def test_a_cross_origin_submission_is_not_low() -> None:
    """An effect on another origin is an effect this session cannot verify."""
    risk, why = _classify(
        _element(
            submits_form=True,
            form_method="get",
            form_action_kind=FormActionKind.CROSS_ORIGIN,
            resolved_label="Search",
        )
    )
    assert risk.level is RiskLevel.HIGH
    assert "another origin" in why


def test_a_plan_that_submits_nothing_is_local() -> None:
    plan = [
        PlanStep(action=ActionType.SET_VALUE, target_ref="field", parameter="keyword"),
        PlanStep(action=ActionType.SET_CHECKED, target_ref="field"),
    ]
    risk, _ = classify_plan(
        CapabilityCategory.FILTER, plan, {"field": _element(control_type=ControlType.TEXT)}
    )
    assert risk.level is RiskLevel.LOW
    assert risk.side_effect is SideEffect.LOCAL


def test_an_unidentifiable_activation_is_not_assumed_safe() -> None:
    """Uncertainty raises the level; it never lowers it (specification 10.1)."""
    risk, why = _classify(_element(control_type=ControlType.BUTTON, resolved_label="Go"))
    assert risk.level is RiskLevel.HIGH
    assert "unknown" in why


def test_the_level_always_matches_the_side_effect_table() -> None:
    """The level is derived from 10.1's table, never chosen beside it."""
    from wcc.models.capability import RISK_OF_SIDE_EFFECT

    for label, method in [("Search", "get"), ("Subscribe", "post"), ("Delete", "get")]:
        risk, _ = _classify(_element(submits_form=True, form_method=method, resolved_label=label))
        assert risk.level is RISK_OF_SIDE_EFFECT[risk.side_effect]


def test_only_low_capabilities_are_ever_confirmation_free() -> None:
    low, _ = _classify(_element(submits_form=True, form_method="get", resolved_label="Search"))
    high, _ = _classify(_element(submits_form=True, form_method="post", resolved_label="Send"))
    assert low.requires_confirmation is False
    assert high.requires_confirmation is True
