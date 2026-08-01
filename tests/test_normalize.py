"""Element normalization rules (specification 7.4, 8.1, 8.2), without a browser."""

from __future__ import annotations

from typing import Any

import pytest

from wcc.driver.normalize import (
    assess_confidence,
    build_locator_chain,
    classify_control,
    is_eligible,
    normalize_element,
    resolve_label,
)
from wcc.models.common import Confidence, ControlType, LabelSource
from wcc.models.locator import LocatorKind


def _record(**overrides: Any) -> dict[str, Any]:
    base: dict[str, Any] = {
        "tag": "input",
        "type": "text",
        "role": None,
        "id": "keyword",
        "name": "keyword",
        "form_id": "product-search",
        "form_present": True,
        "label_for": None,
        "wrapping_label": None,
        "aria_labelledby": None,
        "aria_label": None,
        "element_text": None,
        "placeholder": None,
        "legend_or_heading": None,
        "value": None,
        "sensitive": False,
        "required": False,
        "disabled": False,
        "readonly": False,
        "checked": None,
        "visible": True,
        "box": {"x": 0.0, "y": 0.0, "width": 120.0, "height": 24.0},
        "structural_path": "form>input",
        "search_signal": False,
    }
    base.update(overrides)
    return base


def test_label_priority_follows_the_specification_order() -> None:
    """Specification 8.1 ranks a wrapping label above ``aria-labelledby``."""
    every_source = _record(
        label_for="From label for",
        wrapping_label="From wrapping label",
        aria_labelledby="From labelledby",
        aria_label="From aria-label",
        element_text="From text",
        placeholder="From placeholder",
        legend_or_heading="From legend",
    )
    assert resolve_label(every_source) == ("From label for", LabelSource.LABEL_FOR)

    del every_source["label_for"]
    assert resolve_label(every_source) == ("From wrapping label", LabelSource.WRAPPING_LABEL)

    del every_source["wrapping_label"]
    assert resolve_label(every_source) == ("From labelledby", LabelSource.ARIA_LABELLEDBY)


@pytest.mark.parametrize(
    ("remaining", "expected_source"),
    [
        ("aria_label", LabelSource.ARIA_LABEL),
        ("element_text", LabelSource.BUTTON_TEXT),
        ("name", LabelSource.NAME_ATTRIBUTE),
        ("placeholder", LabelSource.PLACEHOLDER),
        ("legend_or_heading", LabelSource.LEGEND_OR_HEADING),
    ],
)
def test_each_lower_priority_source_is_reachable(
    remaining: str, expected_source: LabelSource
) -> None:
    record = _record(name=None)
    record[remaining] = "Only source"
    assert resolve_label(record) == ("Only source", expected_source)


def test_an_unlabelled_control_has_no_label() -> None:
    assert resolve_label(_record(name=None)) == (None, None)


def test_whitespace_only_labels_do_not_count() -> None:
    assert resolve_label(_record(name=None, aria_label="   ")) == (None, None)


@pytest.mark.parametrize(
    ("record", "expected"),
    [
        (_record(type="search"), ControlType.SEARCH),
        (_record(type="password"), ControlType.PASSWORD),
        (_record(type="file"), ControlType.FILE),
        (_record(type="hidden"), ControlType.HIDDEN),
        (_record(type="checkbox"), ControlType.CHECKBOX),
        (_record(type="submit"), ControlType.BUTTON),
        (_record(tag="select", type=""), ControlType.SELECT),
        (_record(tag="textarea", type=""), ControlType.TEXTAREA),
        (_record(tag="a", type=""), ControlType.LINK),
        (_record(tag="div", type="", role="button"), ControlType.BUTTON),
        (_record(tag="div", type="", role="searchbox"), ControlType.SEARCH),
        (_record(tag="div", type="", role=None), ControlType.OTHER),
    ],
)
def test_controls_are_classified(record: dict[str, Any], expected: ControlType) -> None:
    assert classify_control(record) == expected


@pytest.mark.parametrize(
    ("record", "eligible"),
    [
        (_record(), True),
        (_record(disabled=True), False),
        (_record(visible=False), False),
        (_record(type="hidden"), False),
        (_record(tag="div", type="", role="button"), True),
        (_record(tag="div", type="", role=None, form_present=False), False),
    ],
)
def test_eligibility_follows_specification_8_2(record: dict[str, Any], eligible: bool) -> None:
    assert is_eligible(record, classify_control(record)) is eligible


@pytest.mark.parametrize(
    ("record", "source", "expected"),
    [
        (_record(search_signal=True), None, Confidence.HIGH),
        (_record(), LabelSource.LABEL_FOR, Confidence.HIGH),
        (_record(), LabelSource.ARIA_LABEL, Confidence.MEDIUM),
        (_record(), LabelSource.NAME_ATTRIBUTE, Confidence.MEDIUM),
        (_record(), LabelSource.PLACEHOLDER, Confidence.LOW),
        (_record(), LabelSource.LEGEND_OR_HEADING, Confidence.LOW),
        (_record(), None, Confidence.LOW),
    ],
)
def test_confidence_reflects_the_strength_of_the_evidence(
    record: dict[str, Any], source: LabelSource | None, expected: Confidence
) -> None:
    assert assess_confidence(record, source) == expected


def test_a_locator_chain_lists_candidates_in_priority_order() -> None:
    chain = build_locator_chain(_record(), "Search products")
    kinds = [candidate.kind for candidate in chain.ordered()]
    assert kinds == sorted(kinds)
    assert chain.primary.kind is LocatorKind.ID_ATTRIBUTE


def test_a_chain_is_never_empty_even_without_id_name_or_label() -> None:
    chain = build_locator_chain(_record(id=None, name=None), None)
    assert chain.primary.kind is LocatorKind.STRUCTURAL_PATH
    assert len(chain.candidates) == 1


def test_a_sensitive_record_is_normalized_without_its_value() -> None:
    element = normalize_element(
        _record(type="password", value="SENTINEL-SHOULD-NOT-SURVIVE", sensitive=True),
        "el_x",
    )
    assert element.value is None
    assert element.value_omitted_for_safety is True
    assert "SENTINEL" not in element.model_dump_json()


def test_a_safe_value_is_kept() -> None:
    element = normalize_element(_record(value="earbuds"), "el_y")
    assert element.value == "earbuds"
    assert element.value_omitted_for_safety is False


def test_a_value_the_page_leaked_is_still_dropped_by_type() -> None:
    """The page script refuses first; classification is the independent second barrier."""
    element = normalize_element(
        _record(type="password", value="SENTINEL-LEAKED", sensitive=False), "el_z"
    )
    assert element.value is None
    assert element.value_omitted_for_safety is True
