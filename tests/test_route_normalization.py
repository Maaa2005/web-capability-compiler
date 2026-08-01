"""The deterministic route normalization specification 7.3 requires tests for."""

from __future__ import annotations

from urllib.parse import urlsplit

import pytest

from wcc.compiler.route import normalize_route_pattern, origin_of


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://example.com/products", "https://example.com"),
        ("https://example.com:443/products", "https://example.com"),
        ("http://example.com:80/", "http://example.com"),
        ("http://127.0.0.1:8931/search-page/", "http://127.0.0.1:8931"),
        ("https://Example.COM/x", "https://example.com"),
    ],
)
def test_origin_drops_the_default_port_and_lowercases_the_host(url: str, expected: str) -> None:
    assert origin_of(url) == expected


def test_a_url_without_an_origin_is_rejected() -> None:
    with pytest.raises(ValueError):
        origin_of("/products")


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://example.com/products", "/products"),
        ("https://example.com/products/", "/products/"),
        ("https://example.com/", "/"),
        ("https://example.com", "/"),
        ("https://example.com/products/1234", "/products/{id}"),
        (
            "https://example.com/orders/3f0c8a1e-7b2d-4c5e-9a10-8d7e6f5c4b3a",
            "/orders/{uuid}",
        ),
        ("https://example.com/o/01ARZ3NDEKTSV4RRFFQ69G5FAV", "/o/{ulid}"),
        ("https://example.com/a/deadbeefcafe01", "/a/{hash}"),
        ("https://example.com/api/v2/top-10", "/api/v2/top-10"),
        ("https://example.com/products#reviews", "/products"),
        ("https://example.com/search?keyword=earbuds", "/search?keyword="),
        (
            "https://example.com/search?keyword=earbuds&category=audio",
            "/search?category=&keyword=",
        ),
        (
            "https://example.com/search?category=audio&keyword=earbuds",
            "/search?category=&keyword=",
        ),
        ("https://example.com/search?keyword=", "/search?keyword="),
    ],
)
def test_route_patterns_are_normalized_as_specified(url: str, expected: str) -> None:
    assert normalize_route_pattern(url) == expected


def test_the_pattern_survives_a_different_search_term() -> None:
    """Acceptance criterion 6 rests on this: the term changes, the page class does not."""
    first = normalize_route_pattern("https://example.com/search?keyword=earbuds&page=2")
    second = normalize_route_pattern("https://example.com/search?keyword=laptop&page=7")
    assert first == second


def test_a_different_parameter_set_is_a_different_page_class() -> None:
    assert normalize_route_pattern(
        "https://example.com/search?keyword=x"
    ) != normalize_route_pattern("https://example.com/search?category=x")


def test_normalization_is_deterministic_across_repeated_calls() -> None:
    url = "https://example.com/orders/3f0c8a1e-7b2d-4c5e-9a10-8d7e6f5c4b3a?b=2&a=1"
    assert len({normalize_route_pattern(url) for _ in range(5)}) == 1


@pytest.mark.parametrize(
    ("url", "expected"),
    [
        ("https://[2606:4700::1]/x", "https://[2606:4700::1]"),
        ("https://[2606:4700::1]:8443/x", "https://[2606:4700::1]:8443"),
        ("http://[::1]:8931/", "http://[::1]:8931"),
    ],
)
def test_an_ipv6_origin_keeps_the_brackets_it_needs(url: str, expected: str) -> None:
    """A bare IPv6 origin cannot be parsed back, so a signature built on it breaks."""
    assert origin_of(url) == expected
    assert urlsplit(origin_of(url)).hostname == urlsplit(url).hostname
