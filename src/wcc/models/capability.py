"""Capability templates and their session-bound form (specification 7.8, 7.9).

The two models exist to keep one rule enforceable: a template is portable and
inert, and only a binding makes anything executable. Specification 7.8 states it
directly — a template "must not contain a session ID or live element handle" — and
section 17 repeats it as a persistence rule.

Splitting them is what makes the rule checkable rather than merely stated. A
single model carrying both the reusable plan and the live resolution would be
executable the moment it was loaded from disk, and every caller would have to
remember not to. Two models mean the executable one cannot be loaded from disk at
all without a live session to name.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import Field, model_validator

from wcc.models.common import SCHEMA_VERSION, ActionType, Confidence, WCCModel
from wcc.models.ir import Evidence
from wcc.models.locator import LocatorChain


class CapabilityCategory(StrEnum):
    """The categories v0.1 compiles (specification 3.1)."""

    SEARCH = "search"
    FILTER = "filter"
    NAVIGATE = "navigate"
    PAGINATE = "paginate"
    READ = "read"


class RiskLevel(StrEnum):
    """Specification 10.1. Only ``LOW`` is ever executable.

    ``CRITICAL`` exists even though nothing may run at that level, and the reason
    is that a level nobody can name cannot be reported. An agent asking what a page
    offers deserves to be told that the delete button was seen and refused, rather
    than to find it silently absent.
    """

    LOW = "low"
    MEDIUM = "medium"
    HIGH = "high"
    CRITICAL = "critical"


class SideEffect(StrEnum):
    """What running a capability does to the world (specification 10.1).

    The five values are the specification's, and the ordering between them is the
    whole risk model: risk is decided by consequence, never by mechanism. The same
    click is ``NONE`` on a search form and ``DESTRUCTIVE_OR_FINANCIAL`` on a
    "close account" button, so the click tells you nothing.
    """

    NONE = "none"
    """Idempotent and read-only on the server."""

    LOCAL = "local"
    """Client-local or trivially reversible view state."""

    REVERSIBLE_ACCOUNT = "reversible_account"
    """Reversible account state: a cart, a saved preference."""

    PERSISTENT = "persistent"
    """Persistent server state: a message, a registration, an order."""

    DESTRUCTIVE_OR_FINANCIAL = "destructive_or_financial"
    """Payment, deletion, transfer, or a legal or security consequence."""


#: Specification 10.1's table, as the single mapping everything else defers to.
#: Written here rather than in the classifier so that a capability constructed by
#: hand, imported from a file, or read from an old database is checked against the
#: same table the classifier uses.
RISK_OF_SIDE_EFFECT: dict[SideEffect, RiskLevel] = {
    SideEffect.NONE: RiskLevel.LOW,
    SideEffect.LOCAL: RiskLevel.LOW,
    SideEffect.REVERSIBLE_ACCOUNT: RiskLevel.MEDIUM,
    SideEffect.PERSISTENT: RiskLevel.HIGH,
    SideEffect.DESTRUCTIVE_OR_FINANCIAL: RiskLevel.CRITICAL,
}


#: Comparable order for the levels. Defined once; the binder's "did risk increase"
#: check and the model's own floor check must not disagree about what higher means.
_RISK_ORDER: dict[RiskLevel, int] = {
    RiskLevel.LOW: 0,
    RiskLevel.MEDIUM: 1,
    RiskLevel.HIGH: 2,
    RiskLevel.CRITICAL: 3,
}


def risk_order(level: RiskLevel) -> int:
    """Where ``level`` sits, for comparisons that must agree everywhere."""
    return _RISK_ORDER[level]


class Risk(WCCModel):
    """Why a capability is or is not safe to run (specification 10.1).

    Classified by side effect rather than by mechanism: the same click is harmless
    on a search form and irreversible on a delete button, so the button is not
    what decides.
    """

    level: RiskLevel
    side_effect: SideEffect
    reversible: bool
    requires_confirmation: bool

    @model_validator(mode="after")
    def _level_is_never_below_the_side_effect(self) -> Risk:
        """Let a level be raised above its side effect, never lowered below it.

        Specification 10.1 says uncertainty always raises and never lowers. A
        classifier that has a further reason for concern may return a higher level
        than the table alone implies; one that returns a lower level has talked
        itself out of the consequence it just named, which is the exact move the
        rule exists to forbid.
        """
        floor = RISK_OF_SIDE_EFFECT[self.side_effect]
        if _RISK_ORDER[self.level] < _RISK_ORDER[floor]:
            raise ValueError(
                f"a {self.side_effect} side effect is at least {floor} risk, "
                f"but this capability claims {self.level}"
            )
        return self


class ExpectedEffect(WCCModel):
    """What must be observed for an execution to count as success (11.3)."""

    type: str
    signals: list[str] = Field(min_length=1)


class PlanStep(WCCModel):
    """One step of an execution plan, written against a target name.

    ``target_ref`` is a name inside the template, never an element ID. Element IDs
    belong to one snapshot of one session; a template outlives both.
    """

    action: ActionType
    target_ref: str
    parameter: str | None = None
    value: str | None = None


class CapabilityTemplate(WCCModel):
    """A reusable, portable, non-executable capability (specification 7.8).

    Two invariants are enforced here rather than left to the compiler, because a
    template violating either would be persisted, exported, and only fail much
    later at binding time:

    Every step's ``target_ref`` has a locator chain. A plan referring to a target
    the template cannot locate is a plan that cannot run, and discovering that at
    execution time would mean a partially performed sequence.

    Every locator chain is referred to by a step. An unused chain is either a
    compiler mistake or a step that went missing, and both are worth failing on
    while the template is still being built.
    """

    schema_version: str = SCHEMA_VERSION
    template_id: str
    origin: str
    normalized_route_pattern: str
    page_signature: str
    name: str
    category: CapabilityCategory
    description: str
    parameters: dict[str, object] = Field(default_factory=dict)
    execution_plan_template: list[PlanStep] = Field(min_length=1)
    target_locators: dict[str, LocatorChain] = Field(min_length=1)
    expected_effect: ExpectedEffect
    risk: Risk
    evidence: list[Evidence] = Field(min_length=1)
    confidence: Confidence = Confidence.LOW
    created_at: datetime
    provenance: str = "compiled"
    """``compiled`` locally, or ``imported`` (specification 17)."""

    @model_validator(mode="after")
    def _evidence_outlives_its_snapshot(self) -> CapabilityTemplate:
        """Refuse an element reference that no longer says which page it was on.

        A template is portable and long-lived; a snapshot is neither. An element ID
        alone is meaningful only inside the observation that minted it, so carrying
        one unqualified into a template turns a record of what was seen into a
        pointer at nothing. Qualified, it stays evidence — and still cannot reach a
        live page, which is what specification 7.8 actually forbids.
        """
        dangling = [
            item.element_id
            for item in self.evidence
            if item.element_id is not None and item.snapshot_id is None
        ]
        if dangling:
            raise ValueError(
                f"template evidence names {dangling} without saying which snapshot "
                "they came from; an element ID outlives nothing on its own"
            )
        return self

    @model_validator(mode="after")
    def _plan_and_locators_agree(self) -> CapabilityTemplate:
        referenced = {step.target_ref for step in self.execution_plan_template}
        located = set(self.target_locators)
        if referenced != located:
            missing = sorted(referenced - located)
            unused = sorted(located - referenced)
            raise ValueError(
                "every plan step must have a locator chain and every chain must be "
                f"used: missing locators for {missing}, unused locators for {unused}"
            )
        return self

    def required_targets(self) -> frozenset[str]:
        """The targets that must all resolve before this template may be bound.

        Every target is required in v0.1. Optional targets would need a plan step
        that may be skipped, and specification 10.4's allowlisted, stepwise
        execution has no such step.
        """
        return frozenset(self.target_locators)


class BindingStatus(StrEnum):
    ACTIVE = "active"
    STALE = "stale"
    CLOSED = "closed"


class BoundCapability(WCCModel):
    """A template resolved against one live document (specification 7.9).

    Holding this model is not permission to execute. It records that every target
    resolved uniquely at ``bound_at`` in ``session_id``; whether that is still true
    is a question about the live session, and :mod:`wcc.binding.registry` answers
    it. A row read back from SQLite is a record of a binding that happened, not a
    binding that holds.
    """

    schema_version: str = SCHEMA_VERSION
    capability_id: str
    template_id: str
    session_id: str
    document_id: str
    bound_snapshot_id: str
    resolved_targets: dict[str, str] = Field(min_length=1)
    bound_at: datetime
    status: BindingStatus = BindingStatus.ACTIVE
