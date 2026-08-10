"""The execution protocol of specification 11.1, against a page that is a value.

Every gate here is one a real browser would also enforce, and none of them needs
one. Driving these through Chromium would make the tests slower and no sharper,
and would hide the thing worth asserting: that the refusal happens *before* the
driver is asked to do anything. A fake driver can be asked whether it was.
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from factories import ORIGIN, ROUTE, a_chain, a_snapshot, a_submit, a_template, an_element
from wcc.driver.base import ElementNotResolvedError
from wcc.models.action import ActionObservation, PlannedAction
from wcc.models.capability import CapabilityTemplate, ExpectedEffect
from wcc.models.common import ActionType, ControlType
from wcc.models.execution import ExecutionStatus
from wcc.models.locator import LocatorChain
from wcc.models.session import OpenSessionRequest, PageSnapshot, SessionInfo
from wcc.runtime.coordinator import Coordinator
from wcc.store.store import Store


class FakeDriver:
    """A page that answers questions, and remembers what it was asked to do."""

    def __init__(self, snapshots: list[PageSnapshot]) -> None:
        self._snapshots = snapshots
        self.performed: list[PlannedAction] = []
        self.unresolvable: set[str] = set()
        self.failing: set[str] = set()
        self.settled = True

    async def open_session(self, request: OpenSessionRequest) -> SessionInfo:
        from datetime import UTC, datetime

        return SessionInfo(
            session_id="sess_1",
            created_at=datetime(2026, 8, 10, tzinfo=UTC),
            initial_url=request.url,
            current_document_id="doc_1",
        )

    async def close_session(self, session_id: str) -> None:
        return None

    async def snapshot(self, session_id: str) -> PageSnapshot:
        """Return the next queued observation, repeating the last one forever.

        Repeating rather than exhausting: a test about arguments should not have to
        predict how many times the coordinator looks at the page.
        """
        if len(self._snapshots) > 1:
            return self._snapshots.pop(0)
        return self._snapshots[0]

    async def wait_settled(self, session_id: str, timeout_ms: int) -> None:
        return None

    async def resolve_chain(
        self, session_id: str, chain: LocatorChain, *, described_as: str = "the target"
    ) -> str:
        key = chain.primary.key
        if key in self.unresolvable:
            raise ElementNotResolvedError(f"{described_as} is stale")
        return f"res_{key}"

    async def contains(self, session_id: str, container_id: str, member_id: str) -> bool:
        return True

    async def execute_resolved_action(
        self, session_id: str, handle_id: str, action: PlannedAction
    ) -> ActionObservation:
        from datetime import UTC, datetime

        self.performed.append(action)
        failed = handle_id in self.failing
        return ActionObservation(
            action=action,
            succeeded=not failed,
            observed_at=datetime(2026, 8, 10, tzinfo=UTC),
            document_id="doc_1",
            settled=self.settled,
            error_code="action_failed" if failed else None,
            error_message="the control did not accept the value" if failed else None,
        )


def a_page(**overrides: object) -> PageSnapshot:
    """A snapshot whose elements are the two targets the sample template names."""
    elements = [
        an_element("el_input", locator_chain=a_chain("keyword")),
        a_submit("el_submit", locator_chain=a_chain("search-button")),
    ]
    return a_snapshot(elements=elements, **overrides)


@pytest.fixture
def store(tmp_path: Path) -> Iterator[Store]:
    with Store(tmp_path / "wcc.db") as opened:
        yield opened


async def _prepared(
    store: Store, driver: FakeDriver, template: CapabilityTemplate | None = None
) -> tuple[Coordinator, str]:
    """Open a session and offer the sample capability on it."""
    store.save_template(template or a_template())
    coordinator = Coordinator(driver, store)
    session = await coordinator.open_session(OpenSessionRequest(url=f"{ORIGIN}{ROUTE}"))
    inspection = await coordinator.inspect_page(session.session_id)
    assert inspection.reused, "a stored template for this page class must be reused"
    return coordinator, inspection.capabilities[0].capability_id


async def test_a_wrong_argument_type_is_refused_before_the_driver_is_touched(
    store: Store,
) -> None:
    """Phase 4 acceptance: wrong argument types fail before driver action."""
    driver = FakeDriver([a_page()])
    coordinator, capability_id = await _prepared(store, driver)

    result = await coordinator.execute("sess_1", capability_id, {"keyword": 7})

    assert result.status is ExecutionStatus.REFUSED
    assert result.error is not None
    assert result.error.code == "invalid_arguments"
    assert driver.performed == [], "nothing may reach the page once the schema has refused"


async def test_an_undeclared_argument_is_refused(store: Store) -> None:
    driver = FakeDriver([a_page()])
    coordinator, capability_id = await _prepared(store, driver)

    result = await coordinator.execute(
        "sess_1", capability_id, {"keyword": "earbuds", "sort": "price"}
    )

    assert result.status is ExecutionStatus.REFUSED
    assert driver.performed == []


async def test_a_target_that_no_longer_resolves_is_stale_and_runs_nothing(
    store: Store,
) -> None:
    """Phase 4 acceptance: a removed or ambiguous target returns ``stale``."""
    driver = FakeDriver([a_page()])
    coordinator, capability_id = await _prepared(store, driver)
    driver.unresolvable.add("search-button")

    result = await coordinator.execute("sess_1", capability_id, {"keyword": "earbuds"})

    assert result.status is ExecutionStatus.STALE
    assert result.error is not None
    assert result.error.code == "target_unavailable"
    assert driver.performed == [], "the input must not be filled when the submit is gone"


async def test_a_stale_execution_retires_the_binding_so_a_retry_asks_for_inspection(
    store: Store,
) -> None:
    """Specification 11.2 step 4: never guess a replacement during the failed run."""
    driver = FakeDriver([a_page()])
    coordinator, capability_id = await _prepared(store, driver)
    driver.unresolvable.add("search-button")

    await coordinator.execute("sess_1", capability_id, {"keyword": "earbuds"})
    again = await coordinator.execute("sess_1", capability_id, {"keyword": "earbuds"})

    assert again.status is ExecutionStatus.STALE
    assert again.error is not None
    assert again.error.code == "capability_not_bound"


async def test_a_page_that_has_become_a_post_blocks_execution(store: Store) -> None:
    """Phase 4 acceptance: a risk increase blocks execution.

    The template was compiled against a GET form and rated low. The page in front
    of the runtime now submits by POST, which specification 10.1 makes high with no
    exception, so the gate refuses before the first field is filled.
    """
    risen = a_snapshot(
        elements=[
            an_element("el_input", locator_chain=a_chain("keyword"), form_method="post"),
            a_submit("el_submit", locator_chain=a_chain("search-button"), form_method="post"),
        ]
    )
    driver = FakeDriver([a_page(), risen])
    coordinator, capability_id = await _prepared(store, driver)

    result = await coordinator.execute("sess_1", capability_id, {"keyword": "earbuds"})

    assert result.status is ExecutionStatus.REFUSED
    assert result.error is not None
    assert result.error.code == "risk_not_low"
    assert driver.performed == []


async def test_every_step_is_recorded_including_the_ones_that_did_not_run(
    store: Store,
) -> None:
    """Phase 4 acceptance: each action and observation is recorded."""
    driver = FakeDriver([a_page()])
    coordinator, capability_id = await _prepared(store, driver)
    driver.failing.add("res_keyword")

    result = await coordinator.execute("sess_1", capability_id, {"keyword": "earbuds"})

    assert result.status is ExecutionStatus.FAILED
    assert [step.index for step in result.steps] == [0, 1]
    assert result.steps[0].observation is not None
    assert result.steps[1].observation is None, "the submit must not run after the fill failed"
    assert result.steps[1].error is not None
    assert result.steps[1].error.code == "not_attempted"
    assert len(driver.performed) == 1


async def test_an_execution_whose_page_did_not_change_is_not_a_success(store: Store) -> None:
    """Specification 11.3: no observed effect, no success."""
    unchanged = a_page()
    driver = FakeDriver([unchanged])
    coordinator, capability_id = await _prepared(store, driver)

    result = await coordinator.execute("sess_1", capability_id, {"keyword": "earbuds"})

    assert result.status is ExecutionStatus.FAILED
    assert result.error is not None
    assert result.error.code == "effect_not_observed"
    assert result.verification is not None
    assert not result.verification.passed


async def test_an_observed_url_change_makes_the_execution_succeed(store: Store) -> None:
    # Three observations: the inspection, the execution's baseline, and the page
    # the search left behind.
    after = a_page(url=f"{ORIGIN}{ROUTE}?keyword=cables")
    driver = FakeDriver([a_page(), a_page(), after])
    coordinator, capability_id = await _prepared(store, driver)

    result = await coordinator.execute("sess_1", capability_id, {"keyword": "earbuds"})

    assert result.status is ExecutionStatus.SUCCEEDED
    assert result.verification is not None
    assert result.verification.passed
    assert result.snapshot_id_after is not None
    usage = store.usage_of("tmpl_1")
    assert usage is not None
    assert usage["last_successful_use_at"] is not None


async def test_an_optional_parameter_left_out_leaves_the_control_alone(store: Store) -> None:
    """An omitted optional argument is a control the caller chose not to touch."""
    template = a_template(
        parameters={
            "type": "object",
            "properties": {"keyword": {"type": "string"}},
            "required": [],
            "additionalProperties": False,
        }
    )
    driver = FakeDriver([a_page(), a_page(title="Results")])
    coordinator, capability_id = await _prepared(store, driver, template)

    result = await coordinator.execute("sess_1", capability_id, {})

    assert [action.action for action in driver.performed] == [ActionType.ACTIVATE]
    assert result.steps[0].observation is None
    assert result.steps[0].error is None, "a skipped optional step is not a failure"


async def test_a_result_is_retrievable_only_by_the_session_that_produced_it(
    store: Store,
) -> None:
    driver = FakeDriver([a_page(), a_page(title="Results")])
    coordinator, capability_id = await _prepared(store, driver)

    result = await coordinator.execute("sess_1", capability_id, {"keyword": "earbuds"})

    assert coordinator.execution_result("sess_1", result.execution_id) is not None
    assert coordinator.execution_result("sess_other", result.execution_id) is None


async def test_a_closed_session_cannot_execute_what_it_bound(store: Store) -> None:
    driver = FakeDriver([a_page()])
    coordinator, capability_id = await _prepared(store, driver)
    await coordinator.close_session("sess_1")

    result = await coordinator.execute("sess_1", capability_id, {"keyword": "earbuds"})

    assert result.status is ExecutionStatus.STALE
    assert driver.performed == []


async def test_a_page_class_with_no_stored_template_compiles_and_then_binds(
    store: Store,
) -> None:
    """The cold path: nothing to reuse, so the snapshot is compiled."""
    page = a_snapshot(
        elements=[
            an_element("el_input", control_type=ControlType.SEARCH),
            a_submit("el_submit"),
        ]
    )
    driver = FakeDriver([page])
    coordinator = Coordinator(driver, store)
    await coordinator.open_session(OpenSessionRequest(url=f"{ORIGIN}{ROUTE}"))

    inspection = await coordinator.inspect_page("sess_1")

    assert not inspection.reused
    assert inspection.report is not None
    assert inspection.capabilities, "a compiled template that binds must be offered"


async def test_a_template_naming_an_uncomputable_signal_never_verifies(store: Store) -> None:
    """An unimplemented check is reported as missing, never as satisfied."""
    template = a_template(
        expected_effect=ExpectedEffect(type="page_state_change", signals=["telepathy"])
    )
    driver = FakeDriver([a_page(), a_page(url="https://shop.example/search?x=1")])
    coordinator, capability_id = await _prepared(store, driver, template)

    result = await coordinator.execute("sess_1", capability_id, {"keyword": "earbuds"})

    assert result.status is ExecutionStatus.FAILED
    assert result.verification is not None
    assert result.verification.missing == ["telepathy"]
