"""Fingerprint roles and their boundaries (specification 7.3), without a browser."""

from __future__ import annotations

import pytest

from wcc.compiler.fingerprints import count_bucket, page_signature, structural_hash
from wcc.models.common import ControlType
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


def test_the_page_signature_ignores_buttons_and_links() -> None:
    """Their labels track content, so counting them would give each term its own class."""
    base = [_element()]
    with_link = [*base, _element("el_2", "Wireless Earbuds", ControlType.LINK)]
    assert page_signature("https://x", "/s", base, []) == page_signature(
        "https://x", "/s", with_link, []
    )


def test_the_page_signature_separates_origins_and_routes() -> None:
    elements = [_element()]
    assert page_signature("https://a", "/s", elements, []) != page_signature(
        "https://b", "/s", elements, []
    )
    assert page_signature("https://a", "/s", elements, []) != page_signature(
        "https://a", "/t", elements, []
    )
