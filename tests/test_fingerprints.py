"""Fingerprint roles and their boundaries (specification 7.3), without a browser."""

from __future__ import annotations

import pytest

from wcc.compiler.fingerprints import count_bucket, page_signature, structural_hash
from wcc.models.common import ControlType, FormActionKind
from wcc.models.element import NormalizedElement
from wcc.models.locator import LocatorCandidate, LocatorChain, LocatorKind


def _element(
    element_id: str = "el_1",
    label: str = "Search products",
    control_type: ControlType = ControlType.SEARCH,
) -> NormalizedElement:
    candidate = LocatorCandidate(kind=LocatorKind.ID_ATTRIBUTE, key=element_id)
    return NormalizedElement(
        element_id=element_id,
        tag_name="input",
        resolved_label=label,
        control_type=control_type,
        locator_chain=LocatorChain(primary=candidate, candidates=[candidate]),
    )


def _submit(
    element_id: str = "el_submit",
    label: str = "Search",
    method: str = "get",
    action_route: str = "/search",
    action_kind: FormActionKind = FormActionKind.SAME_ORIGIN,
) -> NormalizedElement:
    candidate = LocatorCandidate(kind=LocatorKind.ID_ATTRIBUTE, key=element_id)
    return NormalizedElement(
        element_id=element_id,
        tag_name="button",
        resolved_label=label,
        control_type=ControlType.BUTTON,
        form_id="search_form",
        form_method=method,
        form_action_route=action_route,
        form_action_kind=action_kind,
        submits_form=True,
        locator_chain=LocatorChain(primary=candidate, candidates=[candidate]),
    )


def _page_link(element_id: str, label: str) -> NormalizedElement:
    candidate = LocatorCandidate(kind=LocatorKind.ID_ATTRIBUTE, key=element_id)
    return NormalizedElement(
        element_id=element_id,
        tag_name="a",
        resolved_label=label,
        control_type=ControlType.LINK,
        is_pagination_control=True,
        locator_chain=LocatorChain(primary=candidate, candidates=[candidate]),
    )


@pytest.mark.parametrize(
    ("count", "bucket"),
    [(0, "0"), (1, "1"), (2, "2-10"), (10, "2-10"), (11, "11-50"), (50, "11-50"), (51, "50+")],
)
def test_counts_enter_the_hash_as_buckets(count: int, bucket: str) -> None:
    assert count_bucket(count) == bucket


def test_the_structural_hash_is_stable_for_identical_input() -> None:
    elements = [_element()]
    assert structural_hash(elements, [], ["0:main"]) == structural_hash(elements, [], ["0:main"])


def test_the_structural_hash_reacts_to_a_new_control() -> None:
    one = structural_hash([_element()], [], ["0:main"])
    two = structural_hash(
        [_element(), _element("el_2", "Colour", ControlType.TEXT)], [], ["0:main"]
    )
    assert one != two


def test_the_structural_hash_reacts_to_control_order() -> None:
    """Moving a control is a structural change even when the page class is unchanged."""
    a = _element()
    b = _element("el_2", "Colour", ControlType.TEXT)
    assert structural_hash([a, b], [], []) != structural_hash([b, a], [], [])


def test_the_page_signature_ignores_control_order() -> None:
    """Two renderings that differ only in field order are the same page class."""
    a = _element()
    b = _element("el_2", "Colour", ControlType.TEXT)
    assert page_signature("https://x", "/s", [a, b], []) == page_signature(
        "https://x", "/s", [b, a], []
    )


def test_the_page_signature_ignores_result_links() -> None:
    """Their labels track content, so counting them would give each term its own class."""
    base = [_element()]
    with_link = [*base, _element("el_2", "Wireless Earbuds", ControlType.LINK)]
    assert page_signature("https://x", "/s", base, []) == page_signature(
        "https://x", "/s", with_link, []
    )


def test_a_changed_result_link_leaves_the_page_signature_alone() -> None:
    """The same search page showing different products is still one page class."""
    yesterday = [_element(), _submit(), _element("el_2", "Wireless Earbuds", ControlType.LINK)]
    today = [
        _element(),
        _submit(),
        _element("el_2", "Noise-cancelling Headphones", ControlType.LINK),
    ]
    assert page_signature("https://x", "/s", yesterday, []) == page_signature(
        "https://x", "/s", today, []
    )


def test_a_removed_submit_moves_the_page_signature() -> None:
    """A form that no longer submits is not the page class the template was built for."""
    with_submit = [_element(), _submit()]
    without_submit = [_element()]
    assert page_signature("https://x", "/s", with_submit, []) != page_signature(
        "https://x", "/s", without_submit, []
    )


def test_a_get_to_post_change_moves_the_page_signature() -> None:
    """The defect this fixes: a submit that keeps its name but changes what it does."""
    searching = [_element(), _submit(method="get", action_route="/search")]
    deleting = [_element(), _submit(method="post", action_route="/search")]
    assert page_signature("https://x", "/s", searching, []) != page_signature(
        "https://x", "/s", deleting, []
    )


def test_a_changed_submit_destination_moves_the_page_signature() -> None:
    """Same id, same label, same form, different target — uniqueness would not catch it."""
    searching = [_submit(action_route="/search")]
    deleting = [_submit(action_route="/account/delete")]
    assert page_signature("https://x", "/s", searching, []) != page_signature(
        "https://x", "/s", deleting, []
    )


def test_a_submit_label_change_leaves_the_page_signature_alone() -> None:
    """A retitled button submitting to the same place is the same page class."""
    english = [_submit(label="Search")]
    japanese = [_submit(label="検索")]
    assert page_signature("https://x", "/s", english, []) == page_signature(
        "https://x", "/s", japanese, []
    )


def test_the_page_signature_ignores_how_many_pages_there_are() -> None:
    """Paging is a property of the class; today's page count is not."""
    five = [_element(), *(_page_link(f"el_p{n}", str(n)) for n in range(1, 6))]
    ten = [_element(), *(_page_link(f"el_p{n}", str(n)) for n in range(1, 11))]
    assert page_signature("https://x", "/s", five, []) == page_signature("https://x", "/s", ten, [])


def test_the_page_signature_notices_that_paging_appeared() -> None:
    """Whether the page pages at all separates a paged result set from a single one."""
    unpaged = [_element()]
    paged = [_element(), _page_link("el_p1", "1")]
    assert page_signature("https://x", "/s", unpaged, []) != page_signature(
        "https://x", "/s", paged, []
    )


def test_the_page_signature_separates_origins_and_routes() -> None:
    elements = [_element()]
    assert page_signature("https://a", "/s", elements, []) != page_signature(
        "https://b", "/s", elements, []
    )
    assert page_signature("https://a", "/s", elements, []) != page_signature(
        "https://a", "/t", elements, []
    )
