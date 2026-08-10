"""Collection records and ``collection_hash`` (specification 7.3, 8.4).

``collection_hash`` answers one question for the verifier: did the result set
change? Specification 7.3 wants it insensitive to irrelevant rendering detail but
sensitive to record identity, to order when order is meaningful, and to a change in
how many records there are.

Those three sensitivities are what the identity projection encodes. A record's
identity is built from its fields, not from its position, so a reordered list of
the same records still changes the hash only when order is declared meaningful —
and a list that gained a record changes it either way, because the count is part
of what is hashed.
"""

from __future__ import annotations

import hashlib
import json
from typing import Any

from wcc.identifiers import new_id
from wcc.models.common import CollectionKind, Confidence
from wcc.models.element import CollectionRecord, CollectionSnapshot
from wcc.models.locator import LocatorCandidate, LocatorChain, LocatorKind

#: Long free text is display data, not identity. Truncating it keeps an unrelated
#: description edit from reading as a changed result set.
_MAX_FIELD_LENGTH = 200


def _digest(payload: str) -> str:
    return "sha256:" + hashlib.sha256(payload.encode("utf-8")).hexdigest()


def record_identity(fields: dict[str, str]) -> str:
    """Project one record onto the deterministic string that identifies it.

    Keys are sorted so DOM order cannot change the projection, and values are
    whitespace-normalized so a reflow is not mistaken for a different record.
    """
    normalized = {
        key: " ".join(str(value).split())[:_MAX_FIELD_LENGTH]
        for key, value in sorted(fields.items())
    }
    return json.dumps(normalized, ensure_ascii=False, separators=(",", ":"), sort_keys=True)


def collection_hash(identities: list[str], *, order_is_meaningful: bool) -> str:
    """Hash a collection's records, honouring whether their order carries meaning.

    The record count is hashed alongside the identities so that a result set which
    grew or shrank is always a change, even in the unlikely case that sorting
    hides it.
    """
    ordered = identities if order_is_meaningful else sorted(identities)
    payload = json.dumps(
        {"count": len(identities), "records": ordered},
        ensure_ascii=False,
        separators=(",", ":"),
    )
    return _digest(payload)


def _activation_locator(path: Any) -> LocatorChain | None:
    """The chain that re-resolves a record's own link, if the record had one.

    Only the structural path, and only one candidate. The other locator kinds
    identify a control by something it declares about itself — an id, a name, an
    accessible name — and a result link declares none of those consistently. Its
    text is the product's name, which is content: a chain built on it would stop
    resolving the moment the page it came from was re-rendered with a longer title.
    """
    if not isinstance(path, str) or not path:
        return None
    candidate = LocatorCandidate(kind=LocatorKind.STRUCTURAL_PATH, key=path)
    return LocatorChain(primary=candidate, candidates=[candidate])


def build_collection(raw: dict[str, Any]) -> CollectionSnapshot:
    """Turn one raw container report into a :class:`CollectionSnapshot`."""
    kind = CollectionKind(raw.get("kind", CollectionKind.LIST))
    order_is_meaningful = bool(raw.get("order_is_meaningful", True))

    records: list[CollectionRecord] = []
    identities: list[str] = []
    for entry in raw.get("records", []):
        fields = {
            str(key): str(value)
            for key, value in dict(entry.get("fields", {})).items()
            if str(value).strip()
        }
        identity = record_identity(fields)
        identities.append(identity)
        records.append(
            CollectionRecord(
                record_id=new_id("rec"),
                identity=identity,
                fields=fields,
                activation_locator=_activation_locator(entry.get("activation_path")),
            )
        )

    return CollectionSnapshot(
        collection_id=new_id("col"),
        kind=kind,
        label=raw.get("label"),
        records=records,
        record_count=len(records),
        order_is_meaningful=order_is_meaningful,
        collection_hash=collection_hash(identities, order_is_meaningful=order_is_meaningful),
        confidence=Confidence.HIGH if kind is CollectionKind.TABLE else Confidence.MEDIUM,
    )


def parse_structured_data(blocks: list[str]) -> list[dict[str, object]]:
    """Parse JSON-LD blocks, discarding any that are not valid JSON objects.

    A malformed block is page content, and page content is untrusted data
    (specification 10.3). It is skipped rather than allowed to fail the snapshot.
    """
    parsed: list[dict[str, object]] = []
    for block in blocks:
        try:
            value = json.loads(block)
        except (ValueError, TypeError):
            continue
        for item in value if isinstance(value, list) else [value]:
            if isinstance(item, dict):
                parsed.append(item)
    return parsed
