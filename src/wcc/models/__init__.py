"""Public WCC models.

Phase 0 defines the models the driver interface needs (specification 13, Phase 0).
Agent Page IR, capability templates, and bound capabilities arrive in Phase 2.
"""

from wcc.models.action import ActionObservation, NavigationEvent, PlannedAction
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
from wcc.models.locator import LocatorCandidate, LocatorChain, LocatorKind
from wcc.models.session import (
    OpenSessionRequest,
    PageSnapshot,
    SessionInfo,
    StructuredDataBlock,
)

__all__ = [
    "SCHEMA_VERSION",
    "ActionObservation",
    "ActionType",
    "BoundingBox",
    "CollectionKind",
    "CollectionRecord",
    "CollectionSnapshot",
    "Confidence",
    "ControlType",
    "LabelSource",
    "LocatorCandidate",
    "LocatorChain",
    "LocatorKind",
    "NavigationEvent",
    "NormalizedElement",
    "OpenSessionRequest",
    "PageSnapshot",
    "PlannedAction",
    "SessionInfo",
    "SessionStatus",
    "StructuredDataBlock",
    "WCCModel",
]
