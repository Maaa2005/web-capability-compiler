"""The three fingerprints of specification 7.3.

They answer different questions and are never interchangeable:

``structural_hash``
    Did this page change in a way that makes a previous inspection stale? It covers
    tags, roles, labels, control types, form and landmark structure, and collection
    sizes as coarse buckets. It deliberately ignores ordinary text, styling, and
    ad-like regions, so a rotating banner or a new result count does not read as a
    structural change.

``page_signature``
    Is this the same *kind* of page as one we already compiled a template for? It
    covers origin, route pattern, the control fingerprint, and landmark structure —
    and nothing that varies between two visits to the same page class. This is what
    lets one saved search template serve every search term.

``collection_hash``
    Did the result set change? Implemented in :mod:`wcc.compiler.collections`, since
    it is a property of one collection rather than of the page.

The difference between the first two is the whole design: a structural change does
not invalidate a capability on its own, and specification 7.3 says so outright.
Binding is decided by locator re-resolution, not by a hash comparison.
"""

from __future__ import annotations

import hashlib
import json

from wcc.models.common import ControlType
from wcc.models.element import CollectionSnapshot, NormalizedElement

#: Item counts enter ``structural_hash`` as buckets, not as numbers. A search that
#: returned nine results yesterday and eleven today is the same page structure;
#: crossing from "a handful" to "a page full" is not.
_COUNT_BUCKETS: tuple[tuple[int, str], ...] = ((0, "0"), (1, "1"), (10, "2-10"), (50, "11-50"))


def count_bucket(count: int) -> str:
    """Return the specification 7.3 bucket for ``count``."""
    for ceiling, label in _COUNT_BUCKETS:
        if count <= ceiling:
            return label
    return "50+"


def _digest(payload: object) -> str:
    encoded = json.dumps(payload, ensure_ascii=False, separators=(",", ":"), sort_keys=False)
    return "sha256:" + hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _control_entry(element: NormalizedElement) -> list[str | bool | None]:
    """The part of an element that describes structure rather than content."""
    return [
        element.tag_name,
        element.role,
        str(element.control_type),
        element.resolved_label,
        element.form_id,
        element.required,
    ]


def control_fingerprint(elements: list[NormalizedElement]) -> list[list[str | bool | None]]:
    """Describe the page's controls, sorted so DOM order cannot perturb it.

    Sorting matters for ``page_signature``: two renderings of the same form that
    differ only in the order the server emitted two fields are the same page class.
    """
    return sorted(
        (_control_entry(element) for element in elements),
        key=lambda entry: json.dumps(entry, ensure_ascii=False, sort_keys=True),
    )


def structural_hash(
    elements: list[NormalizedElement],
    collections: list[CollectionSnapshot],
    landmarks: list[str],
) -> str:
    """Fingerprint the current normalized structure (specification 7.3).

    Element order is preserved here, unlike in :func:`page_signature`: moving a
    control from the header into the main form *is* a structural change worth
    re-inspecting, even though it leaves the page class alone.
    """
    return _digest(
        {
            "controls": [_control_entry(element) for element in elements],
            "landmarks": landmarks,
            "collections": [
                [str(collection.kind), count_bucket(collection.record_count)]
                for collection in collections
            ],
        }
    )


def page_signature(
    origin: str,
    normalized_route_pattern: str,
    elements: list[NormalizedElement],
    landmarks: list[str],
) -> str:
    """Fingerprint the reusable page class (specification 7.3).

    Buttons and links are excluded from the control fingerprint. Their labels track
    content — a result card's link text, a paging button's number — so including
    them would give every search term its own page class and defeat template reuse.
    """
    controls = [
        element
        for element in elements
        if element.control_type not in {ControlType.BUTTON, ControlType.LINK}
    ]
    return _digest(
        {
            "origin": origin,
            "route": normalized_route_pattern,
            "controls": control_fingerprint(controls),
            "landmarks": sorted(landmarks),
        }
    )
