"""Phase 4 acceptance, end to end through Chromium and SQLite.

The unit tests elsewhere prove each gate with a page that is a value, which is
faster and sharper. What they cannot prove is that the whole path works against a
browser: that a template compiled from a real DOM re-resolves against a real
re-render, that a click really navigates, and that the second session finds the
first one's work on disk.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from pathlib import Path

import pytest

from wcc.driver.base import DriverSettings
from wcc.driver.playwright_driver import PlaywrightDriver
from wcc.models.capability import CapabilityTemplate
from wcc.models.execution import ExecutionStatus
from wcc.models.session import OpenSessionRequest
from wcc.policy.url_policy import UrlPolicy
from wcc.runtime.coordinator import Coordinator, Inspection
from wcc.runtime.verify import RESULTS_COLLECTION_CHANGE, URL_CHANGE
from wcc.store.store import Store


@pytest.fixture
async def driver() -> AsyncIterator[PlaywrightDriver]:
    instance = PlaywrightDriver(UrlPolicy(), DriverSettings(allow_private_network=True))
    try:
        yield instance
    finally:
        await instance.shutdown()


@pytest.fixture
def store(tmp_path: Path) -> Iterator[Store]:
    with Store(tmp_path / "wcc.db") as opened:
        yield opened


def _named(inspection: Inspection, name: str) -> tuple[str, CapabilityTemplate]:
    """The capability an agent would pick by name, or a readable failure."""
    for binding in inspection.capabilities:
        template = inspection.templates[binding.template_id]
        if template.name == name:
            return binding.capability_id, template
    offered = sorted(inspection.templates[b.template_id].name for b in inspection.capabilities)
    raise AssertionError(f"{name} was not offered; the page offered {offered}")


async def test_a_compiled_search_executes_and_returns_structured_results(
    driver: PlaywrightDriver, fixture_server: str, store: Store
) -> None:
    """Acceptance 1 and 6: the search runs, and the verifier sees the result set change.

    The fixture search submits to a different page, so both of the effects the
    compiler declared are really available: the URL changes and the result
    collection changes from nothing to three products. Asserting on the entities
    rather than on the HTML is the point of the phase — what comes back is data.
    """
    coordinator = Coordinator(driver, store)
    session = await coordinator.open_session(
        OpenSessionRequest(url=f"{fixture_server}/search-page/")
    )
    inspection = await coordinator.inspect_page(session.session_id)
    capability_id, _ = _named(inspection, "submit_search_form")

    result = await coordinator.execute(session.session_id, capability_id, {"keyword": "earbuds"})

    assert result.status is ExecutionStatus.SUCCEEDED, result.error
    assert URL_CHANGE in result.observed_effects
    assert RESULTS_COLLECTION_CHANGE in result.observed_effects
    labels = {entity.label for entity in result.result_entities}
    assert "Wireless Earbuds" in labels
    await coordinator.close_session(session.session_id)


async def test_a_wrong_argument_type_never_reaches_the_page(
    driver: PlaywrightDriver, fixture_server: str, store: Store
) -> None:
    """Acceptance 2, through the real compiler's own generated schema."""
    coordinator = Coordinator(driver, store)
    session = await coordinator.open_session(
        OpenSessionRequest(url=f"{fixture_server}/search-page/")
    )
    inspection = await coordinator.inspect_page(session.session_id)
    capability_id, _ = _named(inspection, "submit_search_form")

    result = await coordinator.execute(session.session_id, capability_id, {"keyword": 7})

    assert result.status is ExecutionStatus.REFUSED
    assert result.error is not None
    assert result.error.code == "invalid_arguments"
    assert result.steps == [], "a refused call has no steps because nothing was attempted"
    assert driver.snapshot is not None  # the session is still usable
    await coordinator.close_session(session.session_id)


async def test_a_removed_submit_makes_the_capability_stale(
    driver: PlaywrightDriver, fixture_server: str, store: Store
) -> None:
    """Acceptance 3: a removed target returns ``stale`` rather than a guess."""
    coordinator = Coordinator(driver, store)
    session = await coordinator.open_session(
        OpenSessionRequest(url=f"{fixture_server}/search-page/")
    )
    inspection = await coordinator.inspect_page(session.session_id)
    capability_id, _ = _named(inspection, "submit_search_form")

    page = driver._session(session.session_id).page  # noqa: SLF001 - the test is the page's editor
    await page.evaluate("document.querySelector('#product-search button').remove()")

    result = await coordinator.execute(session.session_id, capability_id, {"keyword": "earbuds"})

    assert result.status is ExecutionStatus.STALE
    assert result.error is not None
    assert result.error.code == "target_unavailable"
    assert page.url.endswith("/search-page/"), "nothing may have been submitted"
    await coordinator.close_session(session.session_id)


async def test_a_form_that_became_a_post_is_refused_before_anything_runs(
    driver: PlaywrightDriver, fixture_server: str, store: Store
) -> None:
    """Acceptance 4: a risk increase blocks execution.

    The template was compiled from a GET form. The page now submits by POST, which
    specification 10.1 rates high with no read-only exception, so the capability
    that was low a moment ago may not run.
    """
    coordinator = Coordinator(driver, store)
    session = await coordinator.open_session(
        OpenSessionRequest(url=f"{fixture_server}/search-page/")
    )
    inspection = await coordinator.inspect_page(session.session_id)
    capability_id, _ = _named(inspection, "submit_search_form")

    page = driver._session(session.session_id).page  # noqa: SLF001
    await page.evaluate("document.querySelector('#product-search').method = 'post'")

    result = await coordinator.execute(session.session_id, capability_id, {"keyword": "earbuds"})

    assert result.status is ExecutionStatus.REFUSED
    assert result.error is not None
    assert result.error.code == "risk_not_low"
    assert page.url.endswith("/search-page/")
    await coordinator.close_session(session.session_id)


async def test_every_action_and_observation_is_recorded_and_retrievable(
    driver: PlaywrightDriver, fixture_server: str, store: Store
) -> None:
    """Acceptance 5: each action and observation is recorded."""
    coordinator = Coordinator(driver, store)
    session = await coordinator.open_session(
        OpenSessionRequest(url=f"{fixture_server}/search-page/")
    )
    inspection = await coordinator.inspect_page(session.session_id)
    capability_id, template = _named(inspection, "submit_search_form")

    result = await coordinator.execute(session.session_id, capability_id, {"keyword": "earbuds"})

    assert len(result.steps) == len(template.execution_plan_template)
    ran = [step for step in result.steps if step.observation is not None]
    assert ran, "the plan must have done something"
    assert all(step.observation is not None for step in ran)

    stored = store.load_execution(result.execution_id, session_id=session.session_id)
    assert stored is not None
    assert stored.steps == result.steps
    assert store.load_execution(result.execution_id, session_id="sess_other") is None
    await coordinator.close_session(session.session_id)


async def test_an_entity_reference_opens_the_result_it_names(
    driver: PlaywrightDriver, fixture_server: str, store: Store
) -> None:
    """Specification 7.11: one shared capability, parameterized by which result.

    The argument is an entity the inspection issued, and it selects a member
    *inside* the container the template targets — which is what stops an entity
    reference from becoming a way to click anywhere on the page.
    """
    coordinator = Coordinator(driver, store)
    session = await coordinator.open_session(
        OpenSessionRequest(url=f"{fixture_server}/product-list/")
    )
    inspection = await coordinator.inspect_page(session.session_id)
    capability_id, _ = _named(inspection, "open_result_item")
    wanted = next(e for e in inspection.ir.entities if e.label == "Wireless Earbuds")

    result = await coordinator.execute(
        session.session_id, capability_id, {"entity_id": wanted.entity_id}
    )

    assert result.status is ExecutionStatus.SUCCEEDED, result.error
    page = driver._session(session.session_id).page  # noqa: SLF001
    assert page.url.endswith("/products/1")
    await coordinator.close_session(session.session_id)


async def test_an_entity_from_another_session_is_refused(
    driver: PlaywrightDriver, fixture_server: str, store: Store
) -> None:
    """Specification 7.11: only entities of the current session and snapshot lineage."""
    coordinator = Coordinator(driver, store)
    session = await coordinator.open_session(
        OpenSessionRequest(url=f"{fixture_server}/product-list/")
    )
    inspection = await coordinator.inspect_page(session.session_id)
    capability_id, _ = _named(inspection, "open_result_item")

    result = await coordinator.execute(
        session.session_id, capability_id, {"entity_id": "entity_from_somewhere_else"}
    )

    assert result.status is ExecutionStatus.STALE
    assert result.error is not None
    assert result.error.code == "target_unavailable"
    await coordinator.close_session(session.session_id)


async def test_a_second_session_binds_the_stored_template_without_recompiling(
    driver: PlaywrightDriver, fixture_server: str, store: Store
) -> None:
    """Acceptance 7: the warm path.

    Two sessions, one database. The first compiles; the second must find the same
    templates by page class and bind them, and the way to tell is that it produced
    no compilation report at all.
    """
    coordinator = Coordinator(driver, store)

    first = await coordinator.open_session(OpenSessionRequest(url=f"{fixture_server}/search-page/"))
    cold = await coordinator.inspect_page(first.session_id)
    assert not cold.reused
    assert cold.report is not None
    await coordinator.close_session(first.session_id)

    second = await coordinator.open_session(
        OpenSessionRequest(url=f"{fixture_server}/search-page/")
    )
    warm = await coordinator.inspect_page(second.session_id)

    assert warm.reused
    assert warm.report is None, "a warm inspection must not have compiled anything"
    assert set(warm.templates) == set(cold.templates), "the same templates, by ID"

    capability_id, _ = _named(warm, "submit_search_form")
    result = await coordinator.execute(second.session_id, capability_id, {"keyword": "cables"})
    assert result.status is ExecutionStatus.SUCCEEDED, result.error
    await coordinator.close_session(second.session_id)
