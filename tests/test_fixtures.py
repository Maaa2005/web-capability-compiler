"""Phase 0 acceptance: fixtures serve locally over the loopback allowance."""

from __future__ import annotations

import json
import re
from urllib.request import urlopen

import pytest


@pytest.mark.parametrize("path", ["/search-page/", "/product-list/"])
def test_fixture_is_served(fixture_server: str, path: str) -> None:
    with urlopen(f"{fixture_server}{path}", timeout=5) as response:
        assert response.status == 200
        body = response.read().decode("utf-8")
    assert "<title>Fixture:" in body


def test_search_fixture_carries_explicit_search_markers(fixture_server: str) -> None:
    """Specification 8.3 lists these signals; the fixture must actually exercise them."""
    with urlopen(f"{fixture_server}/search-page/", timeout=5) as response:
        body = response.read().decode("utf-8")
    assert 'role="search"' in body
    assert 'type="search"' in body
    assert '<label for="keyword">' in body


def test_search_fixture_includes_controls_that_must_never_be_captured(
    fixture_server: str,
) -> None:
    """Specification 7.2 forbids capturing these values, so Phase 1 needs them present."""
    with urlopen(f"{fixture_server}/search-page/", timeout=5) as response:
        body = response.read().decode("utf-8")
    assert 'type="password"' in body
    assert 'type="file"' in body
    assert 'type="hidden"' in body


def test_product_list_fixture_has_a_parsable_json_ld_block(fixture_server: str) -> None:
    with urlopen(f"{fixture_server}/product-list/", timeout=5) as response:
        body = response.read().decode("utf-8")
    match = re.search(r'<script type="application/ld\+json">(.*?)</script>', body, re.DOTALL)
    assert match is not None
    payload = json.loads(match.group(1))
    assert payload["@type"] == "ItemList"
    assert len(payload["itemListElement"]) == 3
