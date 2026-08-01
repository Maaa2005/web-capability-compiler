"""The URL and network boundary of specification 10.2.

The resolver is faked throughout so the rules are tested, not the machine's DNS.
"""

from __future__ import annotations

from collections.abc import Sequence

import pytest

from wcc.driver.base import NavigationBlockedError
from wcc.policy.url_policy import UrlPolicy


def _policy(answer: Sequence[str] = ("93.184.216.34",)) -> UrlPolicy:
    return UrlPolicy(resolver=lambda hostname: list(answer))


@pytest.mark.parametrize(
    "url",
    [
        "file:///etc/passwd",
        "data:text/html,<h1>x</h1>",
        "javascript:alert(1)",
        "chrome://settings",
        "about:blank",
        "wcc-custom://open",
        "ftp://example.com/x",
        "/products",
    ],
)
def test_only_http_and_https_may_be_navigated_to(url: str) -> None:
    with pytest.raises(NavigationBlockedError):
        _policy().check(url)


def test_a_public_https_url_is_allowed() -> None:
    _policy().check("https://example.com/products")


def test_credentials_in_a_url_are_refused() -> None:
    with pytest.raises(NavigationBlockedError, match="credentials"):
        _policy().check("https://user:secret@example.com/")


@pytest.mark.parametrize(
    ("address", "category"),
    [
        ("127.0.0.1", "loopback"),
        ("::1", "loopback"),
        ("10.0.0.5", "private"),
        ("192.168.1.10", "private"),
        ("172.16.0.9", "private"),
        ("169.254.10.10", "link-local"),
        ("fe80::1", "link-local"),
        ("224.0.0.1", "multicast"),
        ("0.0.0.0", "unspecified"),
        ("fc00::1", "private"),
    ],
)
def test_restricted_address_ranges_are_refused(address: str, category: str) -> None:
    with pytest.raises(NavigationBlockedError, match=category):
        _policy((address,)).check("https://internal.example/")


@pytest.mark.parametrize(
    "address",
    ["169.254.169.254", "100.100.100.200", "fd00:ec2::254"],
)
def test_cloud_metadata_stays_refused_even_with_the_private_allowance(address: str) -> None:
    """The developer flag exists for fixtures, not for credential endpoints."""
    with pytest.raises(NavigationBlockedError, match="metadata"):
        _policy((address,)).check("https://metadata.example/", allow_private_network=True)


@pytest.mark.parametrize(
    "hostname",
    ["metadata.google.internal", "METADATA.GOOGLE.INTERNAL", "metadata.google.internal."],
)
def test_metadata_hostnames_are_refused_before_resolution(hostname: str) -> None:
    def _explode(_: str) -> Sequence[str]:
        raise AssertionError("the hostname must be refused before it is resolved")

    with pytest.raises(NavigationBlockedError, match="metadata"):
        UrlPolicy(resolver=_explode).check(f"https://{hostname}/computeMetadata/v1/")


def test_the_private_allowance_opens_loopback_only_when_asked() -> None:
    policy = _policy(("127.0.0.1",))
    with pytest.raises(NavigationBlockedError):
        policy.check("http://localhost:8931/search-page/")
    policy.check("http://localhost:8931/search-page/", allow_private_network=True)


def test_every_resolved_address_is_checked_not_only_the_first() -> None:
    """A name answering with one public and one private address is still refused."""
    policy = _policy(("93.184.216.34", "10.1.2.3"))
    with pytest.raises(NavigationBlockedError, match="private"):
        policy.check("https://split-horizon.example/")


def test_a_name_that_does_not_resolve_is_refused() -> None:
    def _fail(hostname: str) -> Sequence[str]:
        raise NavigationBlockedError(f"{hostname!r} does not resolve")

    with pytest.raises(NavigationBlockedError):
        UrlPolicy(resolver=_fail).check("https://nowhere.invalid/")


def test_resolution_is_repeated_on_every_check() -> None:
    """Caching a resolution would reopen the DNS-rebinding window 10.2 closes."""
    calls: list[str] = []

    def _count(hostname: str) -> Sequence[str]:
        calls.append(hostname)
        return ["93.184.216.34"]

    policy = UrlPolicy(resolver=_count)
    policy.check("https://example.com/a")
    policy.check("https://example.com/b")
    assert calls == ["example.com", "example.com"]


def test_a_rebound_name_is_refused_on_the_second_navigation() -> None:
    answers = iter([["93.184.216.34"], ["127.0.0.1"]])
    policy = UrlPolicy(resolver=lambda _: next(answers))

    policy.check("https://rebind.example/first")
    with pytest.raises(NavigationBlockedError, match="loopback"):
        policy.check("https://rebind.example/second")
