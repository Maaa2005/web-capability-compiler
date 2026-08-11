"""What an MCP client is allowed to see (specification 10.3, 12).

This module exists to be a wall. Internally a capability knows the locator chains
that find its targets, and specification 10.3 says an agent must never receive a
selector — not a CSS one, not a structural path, not an accessible-name key that
happens to identify one element. Returning
:class:`~wcc.models.capability.CapabilityTemplate` directly over MCP would hand
all of that over, and nothing about the code would look wrong.

So nothing crosses the boundary by default. Each view names its fields
explicitly, which turns "did we leak a selector" from a question about every
future edit to every model into a question about this one file. A test asserts
the same thing from the outside, by searching serialized tool output for the
fixture's own selectors.
"""

from __future__ import annotations

from collections.abc import Sequence

from pydantic import Field

from wcc.models.capability import (
    BoundCapability,
    CapabilityCategory,
    CapabilityTemplate,
    Risk,
    RiskLevel,
)
from wcc.models.common import Confidence, WCCModel
from wcc.models.compilation import CompilationReport
from wcc.models.ir import Entity, PageIR
from wcc.models.session import PageSnapshot, SessionInfo


class SessionView(WCCModel):
    """The answer to ``open_session`` (specification 12.1)."""

    session_id: str
    url: str
    origin: str
    normalized_route_pattern: str
    document_id: str
    snapshot_id: str
    title: str | None = None
    language: str | None = None
    status: str = "active"


class EntityView(WCCModel):
    """One thing the page is about, as an agent may refer to it.

    ``entity_id`` is the whole point: specification 7.11 has an agent name a result
    by reference rather than by position or by selector, and this is where that
    reference is issued.
    """

    entity_id: str
    type: str
    label: str | None = None
    properties: dict[str, str] = Field(default_factory=dict)
    confidence: Confidence


class CapabilityView(WCCModel):
    """A bound capability, described by what it does rather than how.

    ``parameters`` is the generated JSON Schema, which is the one piece of internal
    structure an agent genuinely needs — it is how the caller knows what to pass.
    Everything that says *where* the capability acts stays behind.
    """

    capability_id: str
    template_id: str
    name: str
    category: CapabilityCategory
    description: str
    parameters: dict[str, object] = Field(default_factory=dict)
    risk: Risk
    confidence: Confidence
    evidence_count: int
    """How many observations the capability rests on, not the observations.

    Evidence names element IDs from the snapshot that compiled the template.
    They are opaque, but they are also of no use to a caller who cannot address
    elements — and specification 10.3's boundary is easier to hold when the answer
    to "should this cross" is no by default.
    """


class RefusalView(WCCModel):
    """Something the compiler saw and declined to offer (specification 8.3).

    Reported because a refusal an agent cannot see is a refusal it will work
    around. "The contact form is high risk because it submits by POST" ends the
    search; silence invites the agent to look for another way in.
    """

    candidate_key: str
    code: str
    message: str
    risk: Risk | None = None


class InspectionView(WCCModel):
    """The answer to ``inspect_page`` (specification 12.2)."""

    session_id: str
    document_id: str
    snapshot_id: str
    url: str
    origin: str
    normalized_route_pattern: str
    page_signature: str
    title: str | None = None
    reused_templates: bool
    """Whether the capabilities came from storage rather than a fresh compile.

    Specification 7.10's cache is invisible from the outside unless the runtime
    says so, and Phase 7's benchmark measures exactly this distinction.
    """

    entities: EntitySummaryView
    capabilities: list[CapabilityView] = Field(default_factory=list)
    refused: list[RefusalView] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)


class EntitySummaryView(WCCModel):
    """What a page is about, in the size specification 12.2 asks for.

    Section 12.2 says ``inspect_page`` returns an "entity summary", and the first
    implementation returned every entity instead. On a 150-product catalog that
    made an inspection 42,000 characters — nearly three times what a low-level
    baseline snapshot of the same page costs — which quietly inverted the whole
    claim of the project. Measurement found it; reading the sentence again would
    also have found it.

    A sample rather than a page: an agent that wants a specific result gets it
    from the execution that produced it, where the entities are the answer rather
    than the scenery.
    """

    total: int
    by_type: dict[str, int] = Field(default_factory=dict)
    sample: list[EntityView] = Field(default_factory=list)
    truncated: bool = False


class CapabilityListView(WCCModel):
    """The answer to ``list_capabilities`` (specification 12.3)."""

    session_id: str
    capabilities: list[CapabilityView] = Field(default_factory=list)


class ClosedSessionView(WCCModel):
    """The answer to ``close_session`` (specification 12.6)."""

    session_id: str
    status: str
    already_closed: bool = False


def entity_view(entity: Entity) -> EntityView:
    return EntityView(
        entity_id=entity.entity_id,
        type=entity.type,
        label=entity.label,
        properties=dict(entity.properties),
        confidence=entity.confidence,
    )


def capability_view(binding: BoundCapability, template: CapabilityTemplate) -> CapabilityView:
    return CapabilityView(
        capability_id=binding.capability_id,
        template_id=template.template_id,
        name=template.name,
        category=template.category,
        description=template.description,
        parameters=dict(template.parameters),
        risk=template.risk,
        confidence=template.confidence,
        evidence_count=len(template.evidence),
    )


def session_view(session: SessionInfo, snapshot: PageSnapshot) -> SessionView:
    return SessionView(
        session_id=session.session_id,
        url=snapshot.url,
        origin=snapshot.origin,
        normalized_route_pattern=snapshot.normalized_route_pattern,
        document_id=snapshot.document_id,
        snapshot_id=snapshot.snapshot_id,
        title=snapshot.title,
        language=snapshot.language,
    )


def inspection_view(
    snapshot: PageSnapshot,
    ir: PageIR,
    capabilities: list[CapabilityView],
    *,
    reused: bool,
    report: CompilationReport | None,
) -> InspectionView:
    warnings = [f"{warning.code}: {warning.message}" for warning in ir.warnings]
    refused: list[RefusalView] = []
    if report is not None:
        refused = [
            RefusalView(
                candidate_key=candidate.candidate_key,
                code=candidate.code,
                message=candidate.message,
                risk=candidate.risk,
            )
            for candidate in report.rejected
        ]
    return InspectionView(
        session_id=snapshot.session_id,
        document_id=snapshot.document_id,
        snapshot_id=snapshot.snapshot_id,
        url=snapshot.url,
        origin=snapshot.origin,
        normalized_route_pattern=snapshot.normalized_route_pattern,
        page_signature=snapshot.page_signature,
        title=snapshot.title,
        reused_templates=reused,
        entities=entity_summary(ir.entities),
        capabilities=capabilities,
        refused=refused,
        warnings=warnings,
    )


#: How many entities an inspection shows. Enough to tell what kind of page this is
#: and to name a few things on it; not enough to be a copy of the page. An agent
#: that needs a particular one runs a capability and gets it in the result.
SAMPLE_SIZE = 5


def entity_summary(entities: Sequence[Entity]) -> EntitySummaryView:
    """Counts, kinds, and a handful of examples."""
    by_type: dict[str, int] = {}
    for entity in entities:
        by_type[str(entity.type)] = by_type.get(str(entity.type), 0) + 1
    return EntitySummaryView(
        total=len(entities),
        by_type=by_type,
        sample=[entity_view(entity) for entity in entities[:SAMPLE_SIZE]],
        truncated=len(entities) > SAMPLE_SIZE,
    )


def within_risk(capability: CapabilityView, ceiling: RiskLevel) -> bool:
    """Whether a capability is at or below a requested risk ceiling."""
    from wcc.models.capability import risk_order

    return risk_order(capability.risk.level) <= risk_order(ceiling)
