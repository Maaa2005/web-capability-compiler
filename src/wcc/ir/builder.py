"""Turn one snapshot into one reading of the page.

Deterministic and deliberately shallow. Every entity here comes from a collection
record the driver already extracted, so the only judgement being made is which
observations count as an entity and what evidence each one rests on. Detecting
searches and filters, naming capabilities, and classifying risk are Phase 3, and
putting any of them here would make the Phase 2 acceptance about work Phase 2 does
not own.

The rule for evidence is the one worth stating: an entity is created only when
something was observed, and the observation is recorded on the entity rather than
inferred later. An entity that cannot say what it rests on is not built at all.
"""

from __future__ import annotations

from datetime import UTC, datetime

from wcc.identifiers import new_id
from wcc.models.ir import Entity, Evidence, IRWarning, PageIR
from wcc.models.session import PageSnapshot

#: A record with no fields is a container the extractor could not read. It is
#: reported rather than dropped: an agent seeing three results where the page shows
#: five should be told the difference exists.
_EMPTY_RECORD = "record_without_fields"

_TRUNCATED = "collection_truncated"


def _entity_type(collection_kind: str) -> str:
    """Name what a collection's records are, without guessing at meaning.

    Phase 6 may enrich this with a semantic label. Until then the type says where
    the record came from, which is true, rather than what it represents, which
    would be a guess dressed as data.
    """
    return f"{collection_kind}_item"


def build_page_ir(snapshot: PageSnapshot) -> PageIR:
    """Read ``snapshot`` into a :class:`PageIR`."""
    entities: list[Entity] = []
    warnings: list[IRWarning] = []

    for collection in snapshot.collections:
        if collection.truncated:
            warnings.append(
                IRWarning(
                    code=_TRUNCATED,
                    message=(
                        f"collection {collection.collection_id} holds "
                        f"{collection.record_count} records and the snapshot kept "
                        f"{len(collection.records)}"
                    ),
                    collection_id=collection.collection_id,
                )
            )

        for record in collection.records:
            if not record.fields:
                warnings.append(
                    IRWarning(
                        code=_EMPTY_RECORD,
                        message=f"record {record.record_id} carried no readable fields",
                        collection_id=collection.collection_id,
                    )
                )
                continue

            evidence = [
                Evidence(
                    kind="dom",
                    element_id=element_id,
                    collection_id=collection.collection_id,
                    attribute="text_content",
                )
                for element_id in record.evidence_element_ids
            ] or [
                Evidence(
                    kind="collection",
                    collection_id=collection.collection_id,
                    # ``identity``, not ``record_id``: the ID is minted per
                    # observation, so putting it here would make two readings of
                    # one unchanged page disagree. The identity is the
                    # deterministic projection specification 8.4 defines for
                    # exactly this purpose.
                    detail=f"record identity {record.identity}",
                )
            ]

            entities.append(
                Entity(
                    entity_id=new_id("entity"),
                    type=_entity_type(str(collection.kind)),
                    label=_label_of(record.fields),
                    properties=dict(record.fields),
                    evidence=evidence,
                    confidence=collection.confidence,
                )
            )

    for warning in snapshot.warnings:
        warnings.append(IRWarning(code="snapshot_limit", message=warning))

    return PageIR(
        ir_id=new_id("ir"),
        session_id=snapshot.session_id,
        document_id=snapshot.document_id,
        snapshot_id=snapshot.snapshot_id,
        built_at=datetime.now(UTC),
        origin=snapshot.origin,
        normalized_route_pattern=snapshot.normalized_route_pattern,
        page_signature=snapshot.page_signature,
        structural_hash=snapshot.structural_hash,
        title=snapshot.title,
        language=snapshot.language,
        entities=entities,
        warnings=warnings,
    )


def _label_of(fields: dict[str, str]) -> str | None:
    """Pick the field that names the record, by a fixed order of preference.

    Fixed rather than "the longest" or "the first": both of those change with the
    page's content, and an entity whose label moved between two readings of an
    unchanged page would break the determinism this phase is accepted on.
    """
    for key in ("title", "name", "label", "heading", "text"):
        value = fields.get(key)
        if value:
            return value
    return next((value for _, value in sorted(fields.items()) if value), None)
