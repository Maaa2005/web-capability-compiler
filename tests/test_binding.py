"""Template lookup and binding (specification 7.10), without a browser."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest

from factories import a_chain, a_snapshot, a_template
from wcc.binding import Binder, BindingRefused, SessionRegistry, order_candidates, risk_increased
from wcc.driver.base import ElementNotResolvedError
from wcc.models.capability import (
    BindingStatus,
    CapabilityTemplate,
    Risk,
    RiskLevel,
    SideEffect,
)
from wcc.models.locator import LocatorChain
from wcc.models.session import PageSnapshot


class Resolver:
    """A resolver that answers from a fixed set of keys the page 'has'."""

    def __init__(self, present: set[str], *, ambiguous: set[str] | None = None) -> None:
        self.present = present
        self.ambiguous = ambiguous or set()
        self.asked: list[str] = []

    async def resolve_chain(
        self, session_id: str, chain: LocatorChain, *, described_as: str = "the target"
    ) -> str:
        del session_id
        key = chain.primary.key
        self.asked.append(key)
        if key in self.present and key not in self.ambiguous:
            return f"marker::{key}"
        raise ElementNotResolvedError(f"{described_as} is stale: no candidate matched once")


class FixedRisk:
    def __init__(self, risk: Risk | None = None) -> None:
        self.risk = risk

    def classify(self, template: CapabilityTemplate, snapshot: PageSnapshot) -> Risk:
        del snapshot
        return self.risk or template.risk


_HIGH = Risk(
    level=RiskLevel.HIGH,
    side_effect=SideEffect.PERSISTENT,
    reversible=False,
    requires_confirmation=True,
)


# -- ordering ------------------------------------------------------------------


def test_an_exact_signature_match_is_preferred() -> None:
    other = a_template(template_id="tmpl_other", page_signature="sha256:different")
    exact = a_template(template_id="tmpl_exact")
    ordered = order_candidates([other, exact], "sha256:page-class-one")
    assert [t.template_id for t in ordered] == ["tmpl_exact", "tmpl_other"]


def test_ties_are_broken_the_same_way_every_time() -> None:
    """Otherwise binding depends on the order SQLite happened to return rows in."""
    same_time = datetime(2026, 8, 1, tzinfo=UTC)
    a = a_template(template_id="tmpl_a", created_at=same_time)
    b = a_template(template_id="tmpl_b", created_at=same_time)
    assert order_candidates([b, a], "sha256:page-class-one") == order_candidates(
        [a, b], "sha256:page-class-one"
    )


def test_the_newest_template_is_tried_first() -> None:
    old = a_template(template_id="tmpl_old", created_at=datetime(2026, 1, 1, tzinfo=UTC))
    new = a_template(template_id="tmpl_new", created_at=datetime(2026, 8, 1, tzinfo=UTC))
    assert [t.template_id for t in order_candidates([old, new], "sha256:page-class-one")] == [
        "tmpl_new",
        "tmpl_old",
    ]


# -- risk ----------------------------------------------------------------------


def test_risk_is_only_increased_by_a_higher_level() -> None:
    low = a_template().risk
    assert risk_increased(low, _HIGH) is True
    assert risk_increased(_HIGH, low) is False
    assert risk_increased(low, low) is False


async def test_a_capability_that_became_dangerous_is_not_bound() -> None:
    """Specification 7.10 step 5: risk is re-checked against the page in front of us."""
    binder = Binder(Resolver({"keyword", "search-button"}), FixedRisk(_HIGH))
    with pytest.raises(BindingRefused) as refused:
        await binder.bind("sess_1", a_snapshot(), [a_template()])
    assert "risk rose" in str(refused.value)


# -- binding -------------------------------------------------------------------


async def test_a_saved_template_binds_against_a_new_session() -> None:
    """Phase 2 acceptance: yesterday's template, today's page, a different term.

    The template holds locator chains, never element IDs, so nothing here depends
    on the session that compiled it having ever existed.
    """
    binder = Binder(Resolver({"keyword", "search-button"}), FixedRisk())
    snapshot = a_snapshot(session_id="sess_tomorrow", url="https://shop.example/search?keyword=usb")

    binding, template = await binder.bind("sess_tomorrow", snapshot, [a_template()])

    assert binding.session_id == "sess_tomorrow"
    assert binding.template_id == template.template_id
    assert set(binding.resolved_targets) == set(template.required_targets())
    assert binding.status is BindingStatus.ACTIVE


async def test_one_unresolvable_target_binds_nothing() -> None:
    """Specification 7.10 step 6 forbids a partial binding, not just a bad one."""
    resolver = Resolver({"keyword"})  # the submit button is gone
    binder = Binder(resolver, FixedRisk())

    with pytest.raises(BindingRefused):
        await binder.bind("sess_1", a_snapshot(), [a_template()])


async def test_an_ambiguous_target_refuses_rather_than_choosing() -> None:
    """Two matches is not one match; the closest element is never picked."""
    resolver = Resolver({"keyword", "search-button"}, ambiguous={"search-button"})
    binder = Binder(resolver, FixedRisk())

    with pytest.raises(BindingRefused):
        await binder.bind("sess_1", a_snapshot(), [a_template()])


async def test_an_incompatible_locator_structure_falls_through_to_recompilation() -> None:
    """Phase 2 acceptance: refusing to bind is what asks the compiler for a new one."""
    binder = Binder(Resolver(set()), FixedRisk())
    with pytest.raises(BindingRefused) as refused:
        await binder.bind("sess_1", a_snapshot(), [a_template()])
    assert "none of 1 candidate templates bound" in str(refused.value)


async def test_a_later_candidate_is_tried_when_an_earlier_one_fails() -> None:
    """Two templates are two all-or-nothing attempts, not one partial one."""
    stale = a_template(
        template_id="tmpl_stale",
        created_at=datetime(2026, 8, 1, tzinfo=UTC),
        target_locators={"search_input": a_chain("old-id"), "search_submit": a_chain("old-button")},
    )
    working = a_template(template_id="tmpl_working", created_at=datetime(2026, 1, 1, tzinfo=UTC))
    binder = Binder(Resolver({"keyword", "search-button"}), FixedRisk())

    binding, chosen = await binder.bind("sess_1", a_snapshot(), [stale, working])

    assert chosen.template_id == "tmpl_working"
    assert binding.template_id == "tmpl_working"


async def test_the_number_of_attempts_is_bounded() -> None:
    """A page class with many near-duplicates must not turn one bind into many."""
    candidates = [
        a_template(
            template_id=f"tmpl_{index}",
            created_at=datetime(2026, 8, index + 1, tzinfo=UTC),
            target_locators={"search_input": a_chain("missing"), "search_submit": a_chain("gone")},
        )
        for index in range(6)
    ]
    resolver = Resolver(set())
    binder = Binder(resolver, FixedRisk(), max_attempts=3)

    with pytest.raises(BindingRefused):
        await binder.bind("sess_1", a_snapshot(), candidates)

    # Three templates attempted, and each stopped at its first unresolvable target.
    assert len(resolver.asked) == 3


async def test_no_saved_template_is_refused_with_the_page_class_named() -> None:
    binder = Binder(Resolver(set()), FixedRisk())
    with pytest.raises(BindingRefused) as refused:
        await binder.bind("sess_1", a_snapshot(), [])
    assert "https://shop.example/search" in str(refused.value)


async def test_a_template_from_another_origin_is_never_bound() -> None:
    binder = Binder(Resolver({"keyword", "search-button"}), FixedRisk())
    elsewhere = a_template(origin="https://other.example")

    with pytest.raises(BindingRefused):
        await binder.bind("sess_1", a_snapshot(), [elsewhere])


# -- what makes a binding usable ------------------------------------------------


async def test_a_binding_is_usable_only_while_its_session_lives() -> None:
    binder = Binder(Resolver({"keyword", "search-button"}), FixedRisk())
    binding, _ = await binder.bind("sess_1", a_snapshot(), [a_template()])

    registry = SessionRegistry()
    assert registry.executable(binding) is False

    registry.opened("sess_1")
    assert registry.executable(binding) is True

    registry.closed("sess_1")
    assert registry.status_of(binding) is BindingStatus.STALE


def test_a_stale_binding_does_not_come_back_to_life() -> None:
    """Status can only downgrade; a reused session ID must not revive a record."""
    from factories import a_binding

    registry = SessionRegistry()
    registry.opened("sess_1")
    closed = a_binding(status=BindingStatus.CLOSED)
    assert registry.status_of(closed) is BindingStatus.CLOSED
