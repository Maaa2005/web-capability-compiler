"""Postcondition verification (specification 11.3)."""

from __future__ import annotations

from datetime import UTC, datetime

from factories import a_chain, a_collection, a_snapshot, an_element
from wcc.models.action import ActionObservation, PlannedAction
from wcc.models.capability import ExpectedEffect
from wcc.models.common import ActionType
from wcc.runtime.verify import (
    COLLECTION_COUNT_CHANGE,
    ELEMENT_APPEARED,
    LOADING_SETTLED,
    RESULTS_COLLECTION_CHANGE,
    URL_CHANGE,
    observed_signals,
    verify,
)


def an_observation(*, settled: bool = True) -> ActionObservation:
    return ActionObservation(
        action=PlannedAction(action=ActionType.ACTIVATE, element_id="res_1"),
        succeeded=True,
        observed_at=datetime(2026, 8, 10, tzinfo=UTC),
        document_id="doc_1",
        settled=settled,
    )


def test_an_unchanged_page_shows_no_change_signal() -> None:
    page = a_snapshot()
    assert observed_signals(page, page, [an_observation()]) == [LOADING_SETTLED]


def test_a_changed_url_is_observed() -> None:
    before = a_snapshot()
    after = a_snapshot(url="https://shop.example/search?keyword=cables")
    assert URL_CHANGE in observed_signals(before, after, [])


def test_a_changed_result_set_is_observed() -> None:
    """Phase 4 acceptance: the verifier detects collection changes."""
    before = a_snapshot(collections=[a_collection([("rec_1", {"title": "A"}, [])])])
    after = a_snapshot(
        collections=[
            a_collection(
                [("rec_1", {"title": "A"}, []), ("rec_2", {"title": "B"}, [])],
                collection_id="coll_1",
            ).model_copy(update={"collection_hash": "sha256:collection-two"})
        ]
    )
    signals = observed_signals(before, after, [])
    assert RESULTS_COLLECTION_CHANGE in signals
    assert COLLECTION_COUNT_CHANGE in signals


def test_a_new_control_is_observed_without_comparing_element_ids() -> None:
    """Element IDs are minted per observation, so identity comes from the locator."""
    before = a_snapshot(elements=[an_element("el_1", locator_chain=a_chain("keyword"))])
    after = a_snapshot(
        elements=[
            an_element("el_9", locator_chain=a_chain("keyword")),
            an_element("el_10", locator_chain=a_chain("sort")),
        ]
    )
    signals = observed_signals(before, after, [])
    assert ELEMENT_APPEARED in signals
    assert "element_disappeared" not in signals


def test_one_matched_signal_of_two_is_enough_and_the_ratio_is_reported() -> None:
    effect = ExpectedEffect(
        type="page_state_change", signals=[URL_CHANGE, RESULTS_COLLECTION_CHANGE]
    )
    before = a_snapshot()
    after = a_snapshot(url="https://shop.example/search?keyword=cables")

    verification, observed = verify(effect, before, after, [])

    assert verification.passed
    assert (verification.signals_matched, verification.signals_expected) == (1, 2)
    assert verification.missing == [RESULTS_COLLECTION_CHANGE]
    assert URL_CHANGE in observed


def test_no_matched_signal_is_a_failure() -> None:
    effect = ExpectedEffect(type="page_state_change", signals=[URL_CHANGE])
    page = a_snapshot()

    verification, _ = verify(effect, page, page, [])

    assert not verification.passed


def test_only_the_last_step_decides_whether_the_page_settled() -> None:
    page = a_snapshot()
    assert LOADING_SETTLED not in observed_signals(
        page, page, [an_observation(), an_observation(settled=False)]
    )
