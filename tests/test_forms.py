"""Form compilation: what a form is for, and what the capability promises (8.3, 7.4).

The tests that matter most here are the refusals. A compiler that finds a search
on every page is easy to write and useless, so each case below names a page shape
where the honest answer is "not this one" and checks the compiler gives it.
"""

from __future__ import annotations

import pytest

from factories import a_snapshot, a_submit, an_element
from wcc.compiler.deterministic import GENERIC_DATA_ENTRY, LOW_CONFIDENCE, compile_snapshot
from wcc.compiler.forms import (
    FormRefusal,
    compile_form,
    group_forms,
    parameter_name,
    read_form,
)
from wcc.models.capability import CapabilityCategory, RiskLevel, SideEffect
from wcc.models.common import ActionType, Confidence, ControlType, LabelSource


def _read(elements):
    groups = group_forms(elements)
    assert len(groups) == 1
    return read_form(groups[0])


def test_a_search_button_makes_the_form_a_search() -> None:
    reading = _read([an_element(control_type=ControlType.SEARCH), a_submit(label="Search")])
    assert reading.category is CapabilityCategory.SEARCH


def test_a_filter_button_beats_a_search_landmark() -> None:
    """The landmark describes the region; the button describes the act.

    A ``role="search"`` panel whose only button says "Filter" narrows results that
    are already on the page. Reading the landmark first would label every such
    panel a search and give agents two capabilities that claim the same thing.
    """
    reading = _read(
        [
            an_element(control_type=ControlType.SEARCH, in_search_landmark=True),
            a_submit(label="Filter", in_search_landmark=True),
        ]
    )
    assert reading.category is CapabilityCategory.FILTER


def test_japanese_keywords_are_recognised() -> None:
    """Specification 8.3 asks for a multilingual list, so it has to actually work."""
    assert _read([an_element(), a_submit(label="検索")]).category is CapabilityCategory.SEARCH
    assert _read([an_element(), a_submit(label="絞り込み")]).category is CapabilityCategory.FILTER


def test_research_is_not_a_search() -> None:
    """The ASCII keywords are bounded, or every research page compiles as a search."""
    reading = _read([an_element(), a_submit(label="Research this topic")])
    assert reading.category is None


def test_a_contact_form_resolves_to_no_category() -> None:
    reading = _read(
        [
            an_element("el_name", control_name="name", resolved_label="Your name"),
            an_element("el_msg", control_type=ControlType.TEXTAREA, control_name="message"),
            a_submit(label="Send message"),
        ]
    )
    assert reading.category is None


def test_a_form_with_two_submits_is_refused_rather_than_guessed() -> None:
    """Specification 7.7 forbids choosing the closest; the same holds before binding."""
    reading = _read(
        [
            an_element(control_type=ControlType.SEARCH),
            a_submit("el_a", label="Search"),
            a_submit("el_b", label="Search everything"),
        ]
    )
    with pytest.raises(FormRefusal) as raised:
        compile_form(reading)
    assert raised.value.code == "ambiguous_submission_mechanism"


def test_a_required_control_that_cannot_be_typed_refuses_the_whole_form() -> None:
    """Compiling around it would submit the form incomplete and call that success."""
    reading = _read(
        [
            an_element(control_type=ControlType.SEARCH),
            an_element("el_r", control_type=ControlType.RADIO, required=True),
            a_submit(label="Search"),
        ]
    )
    with pytest.raises(FormRefusal) as raised:
        compile_form(reading)
    assert raised.value.code == "required_control_not_expressible"


def test_a_password_is_never_a_parameter() -> None:
    """Specification 7.2 forbids reading the value; supplying one is the same breach."""
    reading = _read(
        [
            an_element(control_type=ControlType.SEARCH),
            an_element(
                "el_p",
                control_type=ControlType.PASSWORD,
                control_name="password",
                value_omitted_for_safety=True,
            ),
            a_submit(label="Search"),
        ]
    )
    compiled = compile_form(reading)
    properties = compiled.parameters["properties"]
    assert isinstance(properties, dict)
    assert "password" not in properties


def test_the_generated_schema_names_controls_the_way_the_form_does() -> None:
    reading = _read(
        [
            an_element(control_type=ControlType.SEARCH, control_name="q", resolved_label="Query"),
            an_element(
                "el_c",
                control_type=ControlType.CHECKBOX,
                control_name="in_stock",
                resolved_label="In stock only",
            ),
            an_element(
                "el_s",
                control_type=ControlType.SELECT,
                control_name="category",
                resolved_label="Category",
                required=True,
            ),
            a_submit(label="Search"),
        ]
    )
    compiled = compile_form(reading)
    assert compiled.parameters == {
        "type": "object",
        "properties": {
            "q": {"type": "string", "description": "Query"},
            "in_stock": {"type": "boolean", "description": "In stock only"},
            "category": {"type": "string", "description": "Category"},
        },
        "required": ["category"],
        "additionalProperties": False,
    }
    assert [(s.action, s.target_ref) for s in compiled.plan] == [
        (ActionType.SET_VALUE, "q"),
        (ActionType.SET_CHECKED, "in_stock"),
        (ActionType.SELECT_OPTION, "category"),
        (ActionType.ACTIVATE, "submit"),
    ]


def test_a_post_search_form_compiles_high_and_is_not_executable() -> None:
    """Specification 10.1 has no read-only exception for a POST search."""
    reading = _read(
        [
            an_element(control_type=ControlType.SEARCH, form_method="post"),
            a_submit(label="Search", form_method="post"),
        ]
    )
    compiled = compile_form(reading)
    assert compiled.risk.level is RiskLevel.HIGH
    assert compiled.risk.side_effect is SideEffect.PERSISTENT
    assert "POST" in compiled.risk_reason


def test_colliding_parameter_names_are_suffixed_not_dropped() -> None:
    """A dropped parameter is a control the capability silently leaves at default."""
    taken = {"keyword"}
    assert parameter_name(an_element(control_name="keyword"), taken) == "keyword_2"


def test_a_placeholder_only_form_is_low_confidence_and_never_a_template() -> None:
    """Specification 7.4 puts placeholder-only inference at low, which is not a template."""
    elements = [
        an_element(
            control_type=ControlType.TEXT,
            resolved_label=None,
            label_source=None,
            control_name=None,
        ),
        a_submit(label="Search", label_source=LabelSource.PLACEHOLDER),
    ]
    reading = _read(elements)
    assert reading.category is CapabilityCategory.SEARCH
    assert reading.confidence is Confidence.LOW

    report = compile_snapshot(a_snapshot(elements=elements))
    assert report.templates == []
    assert [w.code for w in report.warnings] == [LOW_CONFIDENCE]


def test_a_refused_form_is_listed_with_its_risk() -> None:
    """Specification 8.3 says report it; Phase 3 says the report carries the risk."""
    elements = [
        an_element(control_name="email", resolved_label="Email address", form_method="post"),
        a_submit(label="Subscribe", form_method="post"),
    ]
    report = compile_snapshot(a_snapshot(elements=elements))
    assert report.templates == []
    assert len(report.rejected) == 1
    rejected = report.rejected[0]
    assert rejected.code == GENERIC_DATA_ENTRY
    assert rejected.risk is not None
    assert rejected.risk.level is RiskLevel.HIGH


def test_two_unnamed_forms_do_not_merge_into_one_capability() -> None:
    """Grouping by ``form_id`` alone would fill one form and submit the other."""
    elements = [
        an_element("el_1", form_id=None, form_key="form:0", control_type=ControlType.SEARCH),
        a_submit("el_2", form_id=None, form_key="form:0", label="Search"),
        an_element("el_3", form_id=None, form_key="form:1", control_type=ControlType.SEARCH),
        a_submit("el_4", form_id=None, form_key="form:1", label="Filter"),
    ]
    groups = group_forms(elements)
    assert [g.key for g in groups] == ["form:0", "form:1"]
    categories = [read_form(g).category for g in groups]
    assert categories == [CapabilityCategory.SEARCH, CapabilityCategory.FILTER]
