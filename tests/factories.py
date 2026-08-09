"""Builders for Phase 2 models, so a test says what it is about.

Every builder returns something valid by default and takes overrides for the one
field the test cares about. A test that has to construct a whole template to make a
point about ordering is a test whose point is hard to find.
"""

from __future__ import annotations

from datetime import UTC, datetime
from typing import Any

from wcc.models.capability import (
    BoundCapability,
    CapabilityCategory,
    CapabilityTemplate,
    ExpectedEffect,
    PlanStep,
    Risk,
    RiskLevel,
    SideEffect,
)
from wcc.models.common import ActionType, CollectionKind, Confidence, SessionStatus
from wcc.models.element import CollectionRecord, CollectionSnapshot
from wcc.models.ir import Entity, Evidence, PageIR
from wcc.models.locator import LocatorCandidate, LocatorChain, LocatorKind
from wcc.models.session import PageSnapshot, SessionInfo

ORIGIN = "https://shop.example"
ROUTE = "/search"
SIGNATURE = "sha256:page-class-one"


def a_chain(key: str, kind: LocatorKind = LocatorKind.ID_ATTRIBUTE) -> LocatorChain:
    candidate = LocatorCandidate(kind=kind, key=key)
    return LocatorChain(primary=candidate, candidates=[candidate])


def a_session(session_id: str = "sess_1", **overrides: Any) -> SessionInfo:
    fields: dict[str, Any] = {
        "session_id": session_id,
        "created_at": datetime(2026, 8, 9, tzinfo=UTC),
        "initial_url": f"{ORIGIN}{ROUTE}",
        "status": SessionStatus.ACTIVE,
    }
    fields.update(overrides)
    return SessionInfo(**fields)


def a_snapshot(**overrides: Any) -> PageSnapshot:
    fields: dict[str, Any] = {
        "session_id": "sess_1",
        "document_id": "doc_1",
        "snapshot_id": "snap_1",
        "captured_at": datetime(2026, 8, 9, tzinfo=UTC),
        "url": f"{ORIGIN}{ROUTE}?keyword=earbuds",
        "origin": ORIGIN,
        "normalized_route_pattern": ROUTE,
        "title": "Search",
        "language": "en",
        "structural_hash": "sha256:structure-one",
        "page_signature": SIGNATURE,
    }
    fields.update(overrides)
    return PageSnapshot(**fields)


def a_collection(
    records: list[tuple[str, dict[str, str], list[str]]],
    *,
    collection_id: str = "coll_1",
    truncated: bool = False,
    record_count: int | None = None,
) -> CollectionSnapshot:
    built = [
        CollectionRecord(
            record_id=record_id,
            identity=record_id,
            fields=fields,
            evidence_element_ids=element_ids,
        )
        for record_id, fields, element_ids in records
    ]
    return CollectionSnapshot(
        collection_id=collection_id,
        kind=CollectionKind.LIST,
        records=built,
        record_count=record_count if record_count is not None else len(built),
        collection_hash="sha256:collection-one",
        truncated=truncated,
        confidence=Confidence.HIGH,
    )


def a_template(**overrides: Any) -> CapabilityTemplate:
    fields: dict[str, Any] = {
        "template_id": "tmpl_1",
        "origin": ORIGIN,
        "normalized_route_pattern": ROUTE,
        "page_signature": SIGNATURE,
        "name": "search_products",
        "category": CapabilityCategory.SEARCH,
        "description": "Search products by keyword",
        "parameters": {
            "type": "object",
            "properties": {"keyword": {"type": "string"}},
            "required": ["keyword"],
            "additionalProperties": False,
        },
        "execution_plan_template": [
            PlanStep(action=ActionType.SET_VALUE, target_ref="search_input", parameter="keyword"),
            PlanStep(action=ActionType.ACTIVATE, target_ref="search_submit"),
        ],
        "target_locators": {
            "search_input": a_chain("keyword"),
            "search_submit": a_chain("search-button"),
        },
        "expected_effect": ExpectedEffect(type="page_state_change", signals=["url_change"]),
        "risk": Risk(
            level=RiskLevel.LOW,
            side_effect=SideEffect.NONE,
            reversible=True,
            requires_confirmation=False,
        ),
        "evidence": [
            Evidence(kind="dom", element_id="el_1", snapshot_id="snap_1", attribute="role")
        ],
        "confidence": Confidence.HIGH,
        "created_at": datetime(2026, 8, 9, tzinfo=UTC),
    }
    fields.update(overrides)
    return CapabilityTemplate(**fields)


def a_binding(**overrides: Any) -> BoundCapability:
    fields: dict[str, Any] = {
        "capability_id": "cap_1",
        "template_id": "tmpl_1",
        "session_id": "sess_1",
        "document_id": "doc_1",
        "bound_snapshot_id": "snap_1",
        "resolved_targets": {"search_input": "marker_1", "search_submit": "marker_2"},
        "bound_at": datetime(2026, 8, 9, tzinfo=UTC),
    }
    fields.update(overrides)
    return BoundCapability(**fields)


def an_ir(snapshot: PageSnapshot, **overrides: Any) -> PageIR:
    fields: dict[str, Any] = {
        "ir_id": "ir_1",
        "session_id": snapshot.session_id,
        "document_id": snapshot.document_id,
        "snapshot_id": snapshot.snapshot_id,
        "built_at": datetime(2026, 8, 9, tzinfo=UTC),
        "origin": snapshot.origin,
        "normalized_route_pattern": snapshot.normalized_route_pattern,
        "page_signature": snapshot.page_signature,
        "structural_hash": snapshot.structural_hash,
        "entities": [
            Entity(
                entity_id="entity_1",
                type="list_item",
                label="Wireless Earbuds",
                properties={"price_text": "4,980 yen"},
                evidence=[Evidence(kind="dom", element_id="el_9", attribute="text_content")],
                confidence=Confidence.HIGH,
            )
        ],
    }
    fields.update(overrides)
    return PageIR(**fields)
