"""Turning a plan step into the element it will act on (specification 7.11, 11.2).

Most steps are simple: the template has a locator chain, the driver re-resolves it,
and that is the target. The interesting ones are the shared navigation capabilities,
whose target is a *container* and whose argument chooses a member inside it — an
``entity_id`` naming a result, or a ``page`` naming a numbered link.

Both go through the same two gates, and the order is the point:

1. the member is identified from evidence, never from the argument's text — an
   entity by the record it was read from, a page number by the pagination controls
   the extractor already marked as such;
2. the member is required to sit inside the container the template named.

Skipping the second gate would quietly widen every navigation capability to the
whole document. An ``entity_id`` whose record had moved into a "recently viewed"
strip, or a page number that also appears in a footer link, would resolve uniquely
and be clicked, and the template's own target would have had no say in it.
"""

from __future__ import annotations

from typing import Protocol

from wcc.models.element import CollectionRecord
from wcc.models.ir import Entity, PageIR
from wcc.models.locator import LocatorChain
from wcc.models.session import PageSnapshot

#: Parameters that select a member inside a container target, and how.
ENTITY_PARAMETER = "entity_id"
PAGE_PARAMETER = "page"

MEMBER_PARAMETERS = frozenset({ENTITY_PARAMETER, PAGE_PARAMETER})


class TargetUnavailable(Exception):
    """The step's element cannot be identified on the page as it now is.

    Specification 11.2 calls this stale and requires the execution to stop before
    any action runs. It is an exception rather than a return value because every
    caller must stop: a target that could not be identified has no safe default,
    and the one thing a runtime must never do here is proceed with a different
    element.
    """

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class ChainResolver(Protocol):
    """The two driver questions this module asks, and no others."""

    async def resolve_chain(
        self, session_id: str, chain: LocatorChain, *, described_as: str = ...
    ) -> str: ...

    async def contains(self, session_id: str, container_id: str, member_id: str) -> bool: ...


def entity_of(ir: PageIR, entity_id: str) -> Entity:
    """The entity this session issued, or a refusal.

    Specification 7.11 resolves ``entity_id`` "only from entities issued in the
    current session and snapshot lineage". Entity IDs are minted per observation,
    so an ID from an earlier inspection simply is not in the current reading —
    which is the lineage check, enforced by the IDs rather than alongside them.
    """
    for entity in ir.entities:
        if entity.entity_id == entity_id:
            return entity
    raise TargetUnavailable(
        f"{entity_id} is not an entity of the current snapshot; specification 7.11 "
        "resolves an entity reference only within the session and snapshot that issued it"
    )


def record_of(snapshot: PageSnapshot, entity: Entity) -> CollectionRecord:
    """The observed record an entity is a reading of."""
    for collection in snapshot.collections:
        if collection.collection_id != entity.source_collection_id:
            continue
        for record in collection.records:
            if record.record_id == entity.source_record_id:
                return record
    raise TargetUnavailable(
        f"the record behind {entity.entity_id} is not in the snapshot it was read from"
    )


def page_link_chain(snapshot: PageSnapshot, page: int) -> LocatorChain:
    """The pagination control for one page number.

    Matched against the controls the extractor marked as pagination, not against
    every element whose text is a number: a price, a quantity, and a result count
    are all numbers, and a page number is a claim about a control's role rather
    than about its text.

    More than one match is refused rather than resolved. Two controls labelled "2"
    inside one pagination landmark is the page contradicting itself, and picking
    either is the guess specification 7.7 forbids.
    """
    wanted = str(page)
    matches = [
        element
        for element in snapshot.elements
        if element.is_pagination_control
        and element.pagination_rel is None
        and (element.resolved_label or "").strip() == wanted
    ]
    if len(matches) != 1:
        raise TargetUnavailable(
            f"the page offers {len(matches)} controls for page {page}; "
            "specification 7.7 does not choose between them"
        )
    return matches[0].locator_chain


async def resolve_member(
    resolver: ChainResolver,
    session_id: str,
    container_id: str,
    chain: LocatorChain,
    described_as: str,
) -> str:
    """Resolve a member chain and prove it belongs to the container."""
    member_id = await resolver.resolve_chain(session_id, chain, described_as=described_as)
    if not await resolver.contains(session_id, container_id, member_id):
        raise TargetUnavailable(
            f"{described_as} resolved outside the container this capability targets"
        )
    return member_id
