"""The URL and network boundary (specification 10.2).

Every navigation passes through :meth:`UrlPolicy.check`, including each redirect
hop, because a redirect is how an allowed origin hands control to one that was
never inspected.

Two decisions are worth stating up front:

- **Resolution happens on every check and is never cached.** Specification 10.2
  requires the *resolved address* to be validated at navigation time, so a name
  that answered with a public address a moment ago must be resolved again. A cache
  would reintroduce exactly the DNS-rebinding window the rule exists to close.
- **The private-network allowance never opens cloud metadata.** The developer flag
  exists so the loopback fixture server can be reached; a metadata endpoint is a
  credential source, not a fixture, so it stays refused even when the flag is on.
"""

from __future__ import annotations

import ipaddress
import socket
from collections.abc import Callable, Sequence
from dataclasses import dataclass, field
from urllib.parse import urlsplit

from wcc.driver.base import NavigationBlockedError

ALLOWED_SCHEMES = frozenset({"http", "https"})

# Reachable by address or by name on several clouds. Always refused: unlike a
# loopback fixture, there is no development workflow that needs these.
_METADATA_ADDRESSES = frozenset(
    {
        ipaddress.ip_address("169.254.169.254"),
        ipaddress.ip_address("100.100.100.200"),
        ipaddress.ip_address("fd00:ec2::254"),
    }
)
_METADATA_HOSTNAMES = frozenset(
    {
        "metadata.google.internal",
        "metadata.goog",
        "instance-data",
    }
)

Resolver = Callable[[str], Sequence[str]]


def resolve_addresses(hostname: str) -> Sequence[str]:
    """Return every address ``hostname`` currently resolves to.

    All of them are checked, not just the first: a name that answers with one
    public and one private address must not be admitted on the strength of the
    address that happened to sort first.
    """
    try:
        infos = socket.getaddrinfo(hostname, None, proto=socket.IPPROTO_TCP)
    except OSError as exc:
        raise NavigationBlockedError(f"{hostname!r} does not resolve: {exc}") from exc
    return [str(info[4][0]) for info in infos]


@dataclass(frozen=True)
class UrlPolicy:
    """The default policy of specification 10.2.

    ``resolver`` is injectable so tests can describe a hostname's answer without
    depending on the machine's DNS.
    """

    resolver: Resolver = field(default=resolve_addresses)

    def check(self, url: str, *, allow_private_network: bool = False) -> None:
        """Raise :class:`NavigationBlockedError` unless ``url`` may be navigated to."""
        parts = urlsplit(url)

        if parts.scheme.lower() not in ALLOWED_SCHEMES:
            raise NavigationBlockedError(
                f"scheme {parts.scheme or '(none)'!r} is not allowed; "
                f"only {sorted(ALLOWED_SCHEMES)} may be navigated to"
            )

        if parts.username is not None or parts.password is not None:
            raise NavigationBlockedError("a URL carrying credentials is refused")

        hostname = parts.hostname
        if not hostname:
            raise NavigationBlockedError(f"{url!r} has no host")

        if hostname.lower().rstrip(".") in _METADATA_HOSTNAMES:
            raise NavigationBlockedError(f"{hostname!r} is a cloud-metadata endpoint")

        for raw in self.resolver(hostname):
            self._check_address(hostname, raw, allow_private_network=allow_private_network)

    def _check_address(self, hostname: str, raw: str, *, allow_private_network: bool) -> None:
        try:
            address = ipaddress.ip_address(raw.split("%")[0])
        except ValueError as exc:
            raise NavigationBlockedError(
                f"{hostname!r} resolved to {raw!r}, which is not an IP address"
            ) from exc

        if address in _METADATA_ADDRESSES:
            raise NavigationBlockedError(
                f"{hostname!r} resolves to the cloud-metadata address {address}"
            )

        category = _restricted_category(address)
        if category is None:
            return
        if allow_private_network and category in {"loopback", "private"}:
            return
        raise NavigationBlockedError(
            f"{hostname!r} resolves to {address}, which is a {category} address"
        )


def _restricted_category(address: ipaddress.IPv4Address | ipaddress.IPv6Address) -> str | None:
    """Name the reason an address is out of bounds, or ``None`` if it is fine.

    Order matters: loopback and link-local are also reported as private by
    :mod:`ipaddress`, and the more specific name is the one worth showing.
    """
    if address.is_unspecified:
        return "unspecified"
    if address.is_loopback:
        return "loopback"
    if address.is_link_local:
        return "link-local"
    if address.is_multicast:
        return "multicast"
    if address.is_private:
        return "private"
    if address.is_reserved:
        return "reserved"
    return None
