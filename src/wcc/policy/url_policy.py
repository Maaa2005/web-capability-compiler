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
    nat64_prefixes: tuple[ipaddress.IPv6Network, ...] = ()
    """Operator-specific NAT64 /96 prefixes (RFC 6052), besides ``64:ff9b::/96``.

    A DNS64 network synthesises addresses under its own prefix, and nothing in an
    address says it is one: ``2001:db8::1`` is an ordinary host. Naming the prefix
    is the only safe way to judge the IPv4 address inside it.
    """

    def check(self, url: str, *, allow_private_network: bool = False) -> None:
        """Raise :class:`NavigationBlockedError` unless ``url`` may be navigated to."""
        try:
            parts = urlsplit(url)
            # ``port`` parses lazily and is where an out-of-range or non-numeric port
            # raises; reading it here means a URL the parser cannot make sense of is
            # refused with a reason instead of failing later inside the browser.
            parts.port  # noqa: B018
        except ValueError as exc:
            raise NavigationBlockedError(f"{url!r} is not a valid URL: {exc}") from exc

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

        addresses = list(self.resolver(hostname))
        if not addresses:
            raise NavigationBlockedError(
                f"{hostname!r} resolved to no addresses; a destination that cannot be "
                "checked is not a destination that may be navigated to"
            )
        for raw in addresses:
            self._check_address(hostname, raw, allow_private_network=allow_private_network)

    def _check_address(self, hostname: str, raw: str, *, allow_private_network: bool) -> None:
        try:
            address = _normalize_address(raw, self.nat64_prefixes)
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


#: IPv6 forms that carry an IPv4 destination in some of their bits. A NAT64 gateway
#: (RFC 6052) and a 6to4 relay (RFC 3056) forward to that IPv4 address, so
#: ``64:ff9b::7f00:1`` is loopback with extra steps. ``ipaddress.is_global`` calls
#: the first one global, which is how it got past the earlier version of this check.
_NAT64_WELL_KNOWN = ipaddress.ip_network("64:ff9b::/96")
_SIX_TO_FOUR = ipaddress.ip_network("2002::/16")
#: Deprecated IPv4-compatible space (RFC 4291 2.5.5.1). Nothing legitimate lives
#: here and ``ipaddress`` reports it as global.
_V4_COMPATIBLE = ipaddress.ip_network("::/96")
#: Deprecated 6to4 relay anycast (RFC 7526). Reported as global by ``ipaddress``.
_SIX_TO_FOUR_RELAY = ipaddress.ip_network("192.88.99.0/24")


def _normalize_address(
    raw: str, nat64_prefixes: tuple[ipaddress.IPv6Network, ...] = ()
) -> ipaddress.IPv4Address | ipaddress.IPv6Address:
    """Parse ``raw`` and unwrap the IPv6 forms that carry an IPv4 destination.

    ``::ffff:169.254.169.254`` and ``169.254.169.254`` are the same destination, so
    they must be classified the same way. Without unwrapping, a mapped metadata
    address fails the identity comparison against the IPv4 members of
    ``_METADATA_ADDRESSES`` and slips through under a laxer category. The same
    goes for the NAT64 well-known prefix and 6to4, whose embedded IPv4 address is
    where the traffic ends up.
    """
    address = ipaddress.ip_address(raw.split("%")[0])
    if isinstance(address, ipaddress.IPv6Address):
        if address.ipv4_mapped is not None:
            return address.ipv4_mapped
        if address in _NAT64_WELL_KNOWN or any(address in net for net in nat64_prefixes):
            return ipaddress.IPv4Address(int(address) & 0xFFFFFFFF)
        if address in _SIX_TO_FOUR:
            return ipaddress.IPv4Address((int(address) >> 80) & 0xFFFFFFFF)
    return address


# Deprecated IPv6 site-local space. Still routable on some networks, and
# ``ipaddress`` reports it as global, so it needs naming outright.
_SITE_LOCAL_V6 = ipaddress.ip_network("fec0::/10")


def _restricted_category(address: ipaddress.IPv4Address | ipaddress.IPv6Address) -> str | None:
    """Name the reason an address is out of bounds, or ``None`` if it is fine.

    Order matters: loopback and link-local are also reported as private by
    :mod:`ipaddress`, and the more specific name is the one worth showing.

    The final rule is deny-unless-global rather than a list of known-bad ranges.
    Enumerating bad ranges fails open on whatever the enumeration missed: shared
    CGNAT space (``100.64.0.0/10``) is neither private nor reserved by
    :mod:`ipaddress`, so a list-based check admits it silently.
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
    if isinstance(address, ipaddress.IPv6Address) and address in _SITE_LOCAL_V6:
        return "site-local"
    if isinstance(address, ipaddress.IPv6Address) and address in _V4_COMPATIBLE:
        return "reserved"
    if isinstance(address, ipaddress.IPv4Address) and address in _SIX_TO_FOUR_RELAY:
        return "reserved"
    if not address.is_global:
        return "non-global"
    return None
