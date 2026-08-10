"""Agent Page IR: what a page means, as opposed to what it contains.

A :class:`~wcc.models.session.PageSnapshot` is an observation — elements,
collections, fingerprints. A :class:`PageIR` is the reading of that observation:
which entities the page is about, what went wrong while reading it, and what each
claim rests on.

The separation matters for one reason. A snapshot is a fact about a moment and is
never wrong; an IR is an interpretation and can be. Keeping them in different
models means an interpretation can be recomputed, compared, or rejected without
touching the evidence it was built from.

Every entity carries evidence, and specification section 13's Phase 2 acceptance
requires it. Evidence is not decoration: a capability compiled from an entity is
eventually executed against a live page, and the only defensible reason to act is
a recorded observation that something was there.
"""

from __future__ import annotations

from datetime import datetime

from pydantic import Field, model_validator

from wcc.models.common import SCHEMA_VERSION, Confidence, WCCModel


class Evidence(WCCModel):
    """One recorded observation supporting a claim.

    ``element_id`` and ``collection_id`` are snapshot-scoped: they name something
    in the snapshot this IR was built from, not something in the live page. A
    reader that wants the live element must re-resolve it, which is exactly the
    boundary specification 7.7 draws.
    """

    kind: str
    """Where the observation came from: ``dom``, ``collection``, or ``json_ld``."""

    element_id: str | None = None
    collection_id: str | None = None
    snapshot_id: str | None = None
    """Which observation ``element_id`` belongs to.

    Required whenever evidence travels further than the IR it was built in. A
    :class:`~wcc.models.capability.CapabilityTemplate` outlives every snapshot, so
    a bare element ID inside one is a pointer to nothing; qualified by the snapshot
    it came from, the same ID stays a record of what was seen and still cannot be
    used to reach the live page.
    """

    attribute: str | None = None
    detail: str | None = None

    @model_validator(mode="after")
    def _must_point_at_something(self) -> Evidence:
        """Refuse evidence that names no source.

        Evidence with nothing to point at is an assertion wearing the word
        "evidence", and it would satisfy an "every entity has evidence" check
        while supporting nothing.
        """
        if not any((self.element_id, self.collection_id, self.detail)):
            raise ValueError(
                "evidence must name an element, a collection, or a detail; "
                "evidence pointing at nothing supports nothing"
            )
        return self


class Entity(WCCModel):
    """Something the page is about (specification 7.5).

    ``available_actions`` stays empty until Phase 4 binds capabilities to
    entities. It is declared now because specification 7.11 makes entity
    references the way result items are addressed, and a model that gains the
    field later would change the persisted shape after templates exist.
    """

    entity_id: str
    type: str
    label: str | None = None
    properties: dict[str, str] = Field(default_factory=dict)
    evidence: list[Evidence] = Field(min_length=1)
    confidence: Confidence = Confidence.LOW
    available_actions: list[dict[str, object]] = Field(default_factory=list)

    source_collection_id: str | None = None
    source_record_id: str | None = None
    """Which observed record this entity is a reading of.

    Specification 7.11 lets an agent name a result by ``entity_id``, and the
    runtime then has to find the thing that entity is about. Going back through
    the evidence list would work until an entity had two pieces of evidence, at
    which point the runtime would be choosing one — the same "pick the closest"
    move specification 7.7 forbids during resolution.

    Both IDs are snapshot-scoped and neither reaches a live page. They name a
    record inside the snapshot this IR was built from, which is exactly the
    lineage check specification 11.2 requires before an entity argument is
    accepted.
    """


class IRWarning(WCCModel):
    """Something the reading could not do, reported rather than swallowed.

    ``code`` is stable and machine-readable; ``message`` is for a person. An agent
    branching on prose would break the first time the wording improved.
    """

    code: str
    message: str
    element_id: str | None = None
    collection_id: str | None = None


class PageIR(WCCModel):
    """The interpreted page (specification 7.2, Phase 2).

    ``snapshot_id`` ties the reading to the observation it came from. Two IRs of
    the same page class built from different snapshots have different IDs and
    different snapshot references while meaning the same thing — which is why
    semantic comparison goes through
    :func:`wcc.ir.canonical.canonical_projection` rather than through equality.
    """

    schema_version: str = SCHEMA_VERSION
    ir_id: str
    session_id: str
    document_id: str
    snapshot_id: str
    built_at: datetime
    origin: str
    normalized_route_pattern: str
    page_signature: str
    structural_hash: str
    title: str | None = None
    language: str | None = None
    entities: list[Entity] = Field(default_factory=list)
    warnings: list[IRWarning] = Field(default_factory=list)
