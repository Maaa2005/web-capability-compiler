"""Public WCC models.

Phase 0 defined the models the driver interface needs; Phase 2 adds the Agent Page
IR, capability templates, and bound capabilities.
"""

from wcc.models.action import ActionObservation, NavigationEvent, PlannedAction
from wcc.models.capability import (
    BindingStatus,
    BoundCapability,
    CapabilityCategory,
    CapabilityTemplate,
    ExpectedEffect,
    PlanStep,
    Risk,
    RiskLevel,
    SideEffect,
)
from wcc.models.common import (
    SCHEMA_VERSION,
    ActionType,
    BoundingBox,
    CollectionKind,
    Confidence,
    ControlType,
    LabelSource,
    SessionStatus,
    WCCModel,
)
from wcc.models.element import CollectionRecord, CollectionSnapshot, NormalizedElement
from wcc.models.ir import Entity, Evidence, IRWarning, PageIR
from wcc.models.locator import LocatorCandidate, LocatorChain, LocatorKind
from wcc.models.session import (
    OpenSessionRequest,
    PageSnapshot,
    SessionInfo,
    StructuredDataBlock,
)

__all__ = [
    "ActionObservation",
    "ActionType",
    "BindingStatus",
    "BoundCapability",
    "BoundingBox",
    "CapabilityCategory",
    "CapabilityTemplate",
    "CollectionKind",
    "CollectionRecord",
    "CollectionSnapshot",
    "Confidence",
    "ControlType",
    "Entity",
    "Evidence",
    "ExpectedEffect",
    "IRWarning",
    "LabelSource",
    "LocatorCandidate",
    "LocatorChain",
    "LocatorKind",
    "NavigationEvent",
    "NormalizedElement",
    "OpenSessionRequest",
    "PageIR",
    "PageSnapshot",
    "PlanStep",
    "PlannedAction",
    "Risk",
    "RiskLevel",
    "SCHEMA_VERSION",
    "SessionInfo",
    "SessionStatus",
    "SideEffect",
    "StructuredDataBlock",
    "WCCModel",
]
