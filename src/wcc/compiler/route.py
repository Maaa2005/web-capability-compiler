"""Deterministic URL normalization (specification 7.3).

``normalized_route_pattern`` is what makes a ``page_signature`` survive a different
search term or a different product ID while still separating genuinely different
page classes. Specification 7.3 requires the algorithm to be deterministic and
covered by tests, so every rule here is a closed, documented case rather than a
heuristic that happens to work on the fixtures.

Query **keys** are kept and their **values** are dropped. Keeping the keys is what
distinguishes ``/search?keyword=`` from ``/search?category=``, which are different
page classes; dropping the values is what lets one saved template serve every
search term.
"""

from __future__ import annotations

import re
from urllib.parse import parse_qsl, urlsplit

_DEFAULT_PORTS = {"http": 80, "https": 443}

_UUID = re.compile(r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}$")
_DIGITS = re.compile(r"^\d+$")
_LONG_HEX = re.compile(r"^[0-9a-fA-F]{12,}$")
_ULID = re.compile(r"^[0-9A-HJKMNP-TV-Z]{26}$")


def origin_of(url: str) -> str:
    """Return ``scheme://host[:port]`` with the scheme's default port removed."""
    parts = urlsplit(url)
    if not parts.scheme or not parts.hostname:
        raise ValueError(f"{url!r} has no origin")
    host = parts.hostname
    if ":" in host:
        # ``urlsplit`` strips the brackets an IPv6 literal needs. Emitting the bare
        # address would produce an origin that cannot be parsed back, and a
        # ``page_signature`` built on it would not survive a round trip.
        host = f"[{host}]"
    origin = f"{parts.scheme}://{host}"
    port = parts.port
    if port is not None and _DEFAULT_PORTS.get(parts.scheme) != port:
        origin = f"{origin}:{port}"
    return origin


def _normalize_segment(segment: str) -> str:
    """Replace a clearly dynamic path segment with a placeholder.

    Only these four shapes are treated as dynamic. A segment that merely contains
    a digit -- ``v2``, ``top-10`` -- stays as it is, because collapsing it would
    merge page classes that a template must keep apart.
    """
    if _DIGITS.match(segment):
        return "{id}"
    if _UUID.match(segment):
        return "{uuid}"
    if _ULID.match(segment):
        return "{ulid}"
    if _LONG_HEX.match(segment):
        return "{hash}"
    return segment


def normalize_route_pattern(url: str) -> str:
    """Return the route pattern of ``url``: path with dynamic segments replaced.

    The fragment is dropped entirely, query values are dropped, and the remaining
    query keys are sorted so that parameter order cannot change the pattern.
    """
    parts = urlsplit(url)
    segments = [segment for segment in parts.path.split("/") if segment]
    path = "/" + "/".join(_normalize_segment(segment) for segment in segments)
    if parts.path.endswith("/") and len(path) > 1:
        path = f"{path}/"

    keys = sorted({key for key, _ in parse_qsl(parts.query, keep_blank_values=True)})
    if not keys:
        return path
    return path + "?" + "&".join(f"{key}=" for key in keys)
