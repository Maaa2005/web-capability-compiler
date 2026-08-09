"""What "the same reading" means, as something a test can check.

Phase 2's acceptance asks that inspecting an unchanged page twice produce
semantically equivalent IR. Plain equality cannot express that: every IR carries
fresh IDs and a fresh timestamp, so two correct readings of one page are never
equal.

The projection here drops what is incidental and keeps what is meant. The
interesting part is what it does with IDs. Dropping them entirely would be the easy
move and the wrong one — evidence points at elements by ID, so an IR whose evidence
had come unhooked from its entities would project identically to a correct one.
Instead, IDs are renumbered in a stable order. Two readings then agree only if
their *reference structure* agrees, not merely their contents.
"""

from __future__ import annotations

import json
from typing import Any

from wcc.models.ir import Entity, Evidence, IRWarning, PageIR


def _entity_sort_key(entity: Entity) -> tuple[str, str, str]:
    """Order entities by what they are, never by the order they were found.

    Two readings of one page may enumerate a result list in either order when the
    order is not meaningful, so a stable key has to come from content.
    """
    return (entity.type, entity.label or "", json.dumps(entity.properties, sort_keys=True))


class _Aliases:
    """Stable stand-ins for the IDs an observation mints afresh each time.

    Element and collection IDs are both per-observation, so both have to be
    renamed — and renamed by the same rule, or a reading would compare equal to one
    that had shuffled which collection an entity came from.
    """

    def __init__(self) -> None:
        self._by_id: dict[str, str] = {}

    def of(self, raw: str | None, prefix: str) -> str | None:
        if raw is None:
            return None
        if raw not in self._by_id:
            self._by_id[raw] = (
                f"{prefix}#{sum(1 for v in self._by_id.values() if v.startswith(prefix))}"
            )
        return self._by_id[raw]


def _project_evidence(evidence: Evidence, aliases: _Aliases) -> dict[str, Any]:
    return {
        "kind": evidence.kind,
        "element": aliases.of(evidence.element_id, "el"),
        "collection": aliases.of(evidence.collection_id, "col"),
        "attribute": evidence.attribute,
        "detail": evidence.detail,
    }


def _build_aliases(entities: list[Entity], warnings: list[IRWarning]) -> _Aliases:
    """Number every referenced ID by where it first appears in canonical order.

    Order comes from content, never from the order things were found, so the same
    page yields the same numbering. An ID cited twice keeps one alias, which is
    what makes a lost reference visible: a reading whose evidence has come unhooked
    produces a different alias where the correct one repeats.
    """
    aliases = _Aliases()
    for entity in sorted(entities, key=_entity_sort_key):
        for evidence in entity.evidence:
            aliases.of(evidence.element_id, "el")
            aliases.of(evidence.collection_id, "col")
    for warning in warnings:
        aliases.of(warning.element_id, "el")
        aliases.of(warning.collection_id, "col")
    return aliases


def _project_warning(warning: IRWarning, aliases: _Aliases) -> dict[str, Any]:
    # The code, not the message: prose is written for a person and improving its
    # wording must not read as the page having changed.
    return {
        "code": warning.code,
        "element": aliases.of(warning.element_id, "el"),
        "collection": aliases.of(warning.collection_id, "col"),
    }


def canonical_projection(ir: PageIR) -> dict[str, Any]:
    """Reduce an IR to what two equivalent readings must share.

    Kept: the page class, the fingerprints, and every entity's meaning together
    with the reference structure of its evidence.

    Dropped: the IR, session, document, and snapshot IDs, and the build time. These
    say *which* reading this is, and the question here is what it says.
    """
    aliases = _build_aliases(ir.entities, ir.warnings)
    return {
        "origin": ir.origin,
        "normalized_route_pattern": ir.normalized_route_pattern,
        "page_signature": ir.page_signature,
        "structural_hash": ir.structural_hash,
        "title": ir.title,
        "language": ir.language,
        "entities": [
            {
                "type": entity.type,
                "label": entity.label,
                "properties": dict(sorted(entity.properties.items())),
                "confidence": str(entity.confidence),
                # Order preserved: evidence is listed in the order it was gathered,
                # and a reading that cites the same facts in a different order has
                # read the page differently.
                "evidence": [_project_evidence(item, aliases) for item in entity.evidence],
            }
            for entity in sorted(ir.entities, key=_entity_sort_key)
        ],
        "warnings": sorted(
            (_project_warning(warning, aliases) for warning in ir.warnings),
            key=lambda projected: json.dumps(projected, sort_keys=True),
        ),
    }


def semantically_equivalent(one: PageIR, other: PageIR) -> bool:
    """Whether two readings say the same thing about the same page."""
    return canonical_projection(one) == canonical_projection(other)
