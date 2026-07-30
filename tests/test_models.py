"""Phase 0 models: the rules the specification states in prose, pinned as tests."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
from pydantic import ValidationError

from wcc.models import (
    ActionType,
    LocatorCandidate,
    LocatorChain,
    LocatorKind,
    OpenSessionRequest,
    PlannedAction,
    SessionInfo,
    SessionStatus,
)


def test_no_state_relative_toggle_action() -> None:
    """Specification 10.4 lists six actions and deliberately omits ``toggle``."""
    assert {a.value for a in ActionType} == {
        "focus",
        "set_value",
        "select_option",
        "set_checked",
        "activate",
        "scroll_into_view",
    }


def test_set_checked_requires_explicit_state() -> None:
    with pytest.raises(ValidationError):
        PlannedAction(action=ActionType.SET_CHECKED, element_id="el_1")

    action = PlannedAction(action=ActionType.SET_CHECKED, element_id="el_1", checked=False)
    assert action.checked is False


def test_action_rejects_payload_it_does_not_use() -> None:
    """A value on ``activate`` means the caller misunderstood the action."""
    with pytest.raises(ValidationError):
        PlannedAction(action=ActionType.ACTIVATE, element_id="el_1", value="text")


def test_models_reject_unknown_fields() -> None:
    """Public models mirror ``additionalProperties: false`` (specification 11.1)."""
    with pytest.raises(ValidationError):
        OpenSessionRequest(url="https://example.com", selector="#search")  # type: ignore[call-arg]


def test_private_network_is_off_by_default() -> None:
    assert OpenSessionRequest(url="https://example.com").allow_private_network is False


def test_locator_chain_resolves_in_priority_order() -> None:
    """Specification 7.6 fixes the candidate priority; ``ordered`` must follow it."""
    structural = LocatorCandidate(kind=LocatorKind.STRUCTURAL_PATH, key="form>input[1]")
    by_name = LocatorCandidate(kind=LocatorKind.NAME_WITH_FORM, key="keyword", form_id="f_1")
    by_id = LocatorCandidate(kind=LocatorKind.ID_ATTRIBUTE, key="keyword")

    chain = LocatorChain(primary=by_id, candidates=[structural, by_name, by_id])
    assert [c.kind for c in chain.ordered()] == [
        LocatorKind.ID_ATTRIBUTE,
        LocatorKind.NAME_WITH_FORM,
        LocatorKind.STRUCTURAL_PATH,
    ]


def test_locator_chain_needs_at_least_one_candidate() -> None:
    with pytest.raises(ValidationError):
        LocatorChain(
            primary=LocatorCandidate(kind=LocatorKind.ID_ATTRIBUTE, key="keyword"),
            candidates=[],
        )


def test_session_starts_without_document_or_snapshot() -> None:
    """Document and snapshot identity are separate and only known after observation."""
    session = SessionInfo(
        session_id="sess_1",
        created_at=datetime(2026, 7, 31, 4, 0, tzinfo=UTC),
        initial_url="https://example.com/products",
    )
    assert session.status is SessionStatus.ACTIVE
    assert session.current_document_id is None
    assert session.current_snapshot_id is None
