"""Shared navigation capabilities (specification 7.11).

The prohibition is the specification's, so the first test states it directly: a
page of results must not become a capability per result. The rest defend the
inference that makes one shared capability possible — finding a repeated
structure without mistaking a header menu for one.
"""

from __future__ import annotations

from typing import Any

from factories import a_path_chain, an_element
from wcc.compiler.navigation import (
    build_pagination_templates,
    build_result_item_template,
    common_prefix,
    find_result_links,
)
from wcc.models.capability import RiskLevel
from wcc.models.common import ActionType, Confidence, ControlType
from wcc.models.element import NormalizedElement


def a_link(element_id: str, path: str, **overrides: Any) -> NormalizedElement:
    return an_element(
        element_id,
        tag_name="a",
        control_type=ControlType.LINK,
        control_name=None,
        form_id=None,
        form_key=None,
        form_method=None,
        form_action_route=None,
        form_action_kind=None,
        locator_chain=a_path_chain(path),
        **overrides,
    )


def _results() -> list[NormalizedElement]:
    return [
        a_link("el_1", "main>ul>li[1]>a", resolved_label="Wireless Earbuds"),
        a_link("el_2", "main>ul>li[2]>a", resolved_label="Bluetooth Speaker"),
        a_link("el_3", "main>ul>li[3]>a", resolved_label="Studio Monitor"),
    ]


def test_three_results_produce_one_capability_not_three() -> None:
    template = build_result_item_template(_results())
    assert template is not None
    assert template.name == "open_result_item"
    assert template.parameters["properties"] == {"entity_id": {"type": "string"}}
    assert len(template.plan) == 1
    assert template.plan[0].parameter == "entity_id"


def test_the_capability_targets_the_container_the_entity_selects_within() -> None:
    """The parameter has to have something to select inside of."""
    template = build_result_item_template(_results())
    assert template is not None
    chain = template.locators["result_collection"]
    assert chain.primary.key == "main>ul"


def test_a_header_menu_is_not_a_result_list() -> None:
    """Two sibling links in a nav have exactly the shape of a two-item result list."""
    menu = [
        a_link("el_1", "nav>a[1]", resolved_label="Home", in_navigation=True),
        a_link("el_2", "nav>a[2]", resolved_label="Products", in_navigation=True),
    ]
    assert find_result_links(menu) == []
    assert build_result_item_template(menu) is None


def test_two_equally_sized_result_lists_are_left_uncompiled() -> None:
    """Picking either would attach the capability to a collection nobody named."""
    two_lists = [
        a_link("el_1", "main>ul[1]>li[1]>a"),
        a_link("el_2", "main>ul[1]>li[2]>a"),
        a_link("el_3", "main>section>ol>li[1]>a"),
        a_link("el_4", "main>section>ol>li[2]>a"),
    ]
    assert find_result_links(two_lists) == []


def test_common_prefix_stops_where_the_siblings_differ() -> None:
    assert common_prefix(["main>ul>li[1]>a", "main>ul>li[2]>a"]) == "main>ul"
    assert common_prefix(["nav>a[1]", "main>ul>li[1]>a"]) is None


def test_a_declared_rel_becomes_a_parameterless_capability() -> None:
    controls = [
        a_link(
            "el_next",
            "nav>a[3]",
            resolved_label="Next",
            in_navigation=True,
            is_pagination_control=True,
            pagination_rel="next",
        )
    ]
    templates = build_pagination_templates(controls)
    assert [t.name for t in templates] == ["navigate_next_page"]
    template = templates[0]
    assert template.parameters["properties"] == {}
    assert template.plan[0].action is ActionType.ACTIVATE
    assert template.plan[0].parameter is None
    # rel is the page declaring a relationship, not the compiler reading a word.
    assert template.confidence is Confidence.HIGH
    assert template.risk.level is RiskLevel.LOW


def test_numbered_links_collapse_into_one_capability_taking_the_number() -> None:
    controls = [
        a_link(
            f"el_{n}",
            f"nav>a[{n}]",
            resolved_label=str(n),
            in_navigation=True,
            is_pagination_control=True,
        )
        for n in (1, 2, 3)
    ]
    templates = build_pagination_templates(controls)
    assert [t.name for t in templates] == ["navigate_to_page"]
    template = templates[0]
    assert template.parameters["properties"] == {"page": {"type": "integer", "minimum": 1}}
    assert template.confidence is Confidence.MEDIUM


def test_the_page_count_is_not_frozen_into_the_schema() -> None:
    """A portable template outlives the snapshot that showed it three pages."""
    controls = [
        a_link(
            f"el_{n}",
            f"nav>a[{n}]",
            resolved_label=str(n),
            in_navigation=True,
            is_pagination_control=True,
        )
        for n in (1, 2)
    ]
    schema = build_pagination_templates(controls)[0].parameters["properties"]
    assert isinstance(schema, dict)
    assert "maximum" not in schema["page"]


def test_two_controls_claiming_next_produce_no_capability() -> None:
    """The page is contradicting itself, and choosing one would be choosing at random."""
    controls = [
        a_link(
            element_id,
            path,
            resolved_label="Next",
            in_navigation=True,
            is_pagination_control=True,
            pagination_rel="next",
        )
        for element_id, path in (("el_a", "nav>a[1]"), ("el_b", "footer>a[1]"))
    ]
    assert build_pagination_templates(controls) == []
