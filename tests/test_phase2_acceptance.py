"""Phase 2 acceptance, end to end against the fixture server.

The unit tests elsewhere prove each part in isolation with a stub resolver. This
file proves the part that cannot be stubbed: that a template compiled from one
browser session, written to SQLite, and read back in another session still finds
its targets on a real page.
"""

from __future__ import annotations

from collections.abc import AsyncIterator, Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest
from pydantic import ValidationError

from wcc.binding import Binder, BindingRefused, SessionRegistry
from wcc.driver.base import DriverSettings
from wcc.driver.playwright_driver import PlaywrightDriver
from wcc.identifiers import new_id
from wcc.ir import build_page_ir, semantically_equivalent
from wcc.models.capability import (
    CapabilityCategory,
    CapabilityTemplate,
    ExpectedEffect,
    PlanStep,
    Risk,
    RiskLevel,
    SideEffect,
)
from wcc.models.common import ActionType, Confidence
from wcc.models.ir import Evidence
from wcc.models.session import OpenSessionRequest, PageSnapshot
from wcc.policy.url_policy import UrlPolicy
from wcc.store import Store


@pytest.fixture
async def driver() -> AsyncIterator[PlaywrightDriver]:
    instance = PlaywrightDriver(UrlPolicy(), DriverSettings(allow_private_network=True))
    try:
        yield instance
    finally:
        await instance.shutdown()


@pytest.fixture
def store(tmp_path: Path) -> Iterator[Store]:
    with Store(tmp_path / "wcc.sqlite3") as opened:
        yield opened


class KeepsRisk:
    """Phase 3 owns classification; Phase 2 owns the control flow around it."""

    def classify(self, template: CapabilityTemplate, snapshot: PageSnapshot) -> Risk:
        del snapshot
        return template.risk


def _compile_search_template(snapshot: PageSnapshot) -> CapabilityTemplate:
    """Stand in for the Phase 3 compiler, using only what the snapshot observed.

    Deliberately hand-built. Phase 2 is accepted on persistence and binding, and
    generating this from the page is exactly the Phase 3 work that must not be
    smuggled in to make a Phase 2 test pass.
    """
    keyword = next(
        element for element in snapshot.elements if element.resolved_label == "Search products"
    )
    submit = next(
        element
        for element in snapshot.elements
        if element.submits_form and element.form_id == "product-search"
    )

    return CapabilityTemplate(
        template_id=new_id("tmpl"),
        origin=snapshot.origin,
        normalized_route_pattern=snapshot.normalized_route_pattern,
        page_signature=snapshot.page_signature,
        name="search_products",
        category=CapabilityCategory.SEARCH,
        description="Search products by keyword",
        parameters={
            "type": "object",
            "properties": {"keyword": {"type": "string"}},
            "required": ["keyword"],
            "additionalProperties": False,
        },
        execution_plan_template=[
            PlanStep(action=ActionType.SET_VALUE, target_ref="search_input", parameter="keyword"),
            PlanStep(action=ActionType.ACTIVATE, target_ref="search_submit"),
        ],
        target_locators={
            "search_input": keyword.locator_chain,
            "search_submit": submit.locator_chain,
        },
        expected_effect=ExpectedEffect(type="page_state_change", signals=["url_change"]),
        risk=Risk(
            level=RiskLevel.LOW,
            side_effect=SideEffect.NONE,
            reversible=True,
            requires_confirmation=False,
        ),
        evidence=[
            Evidence(
                kind="dom",
                element_id=keyword.element_id,
                snapshot_id=snapshot.snapshot_id,
                attribute="label",
            ),
            Evidence(
                kind="dom",
                element_id=submit.element_id,
                snapshot_id=snapshot.snapshot_id,
                attribute="form_action_route",
            ),
        ],
        confidence=Confidence.HIGH,
        created_at=datetime.now(UTC),
    )


async def test_a_saved_template_binds_in_a_new_session_with_a_different_term(
    driver: PlaywrightDriver, fixture_server: str, store: Store
) -> None:
    """Phase 2 acceptance criterion 4, with a real browser on both sides.

    The first session compiles and stores; it is then closed, so nothing it held
    can help the second one. The second session finds the template by page class
    alone and re-resolves every target from the stored locator chains.
    """
    registry = SessionRegistry()
    binder = Binder(driver, KeepsRisk())

    # Both visits carry the same query key, because the route pattern keeps keys:
    # ``?keyword=`` and no query at all are different page classes by design, and
    # the point here is a different *term*, not a different page.
    first = await driver.open_session(
        OpenSessionRequest(url=f"{fixture_server}/search-page/?keyword=earbuds")
    )
    registry.opened(first.session_id)
    store.save_session(first)
    compiled_from = await driver.snapshot(first.session_id)
    store.save_snapshot(compiled_from)
    template = store.save_template(_compile_search_template(compiled_from))
    await driver.close_session(first.session_id)
    registry.closed(first.session_id)

    second = await driver.open_session(
        OpenSessionRequest(url=f"{fixture_server}/search-page/?keyword=usb-c")
    )
    registry.opened(second.session_id)
    store.save_session(second)
    today = await driver.snapshot(second.session_id)

    candidates = store.find_templates(today.origin, today.normalized_route_pattern)
    assert [candidate.template_id for candidate in candidates] == [template.template_id]

    binding, bound_template = await binder.bind(second.session_id, today, candidates)
    store.save_binding(binding)
    store.record_bind_outcome(bound_template.template_id, succeeded=True)

    assert binding.session_id == second.session_id
    assert binding.session_id != first.session_id
    assert set(binding.resolved_targets) == {"search_input", "search_submit"}
    assert registry.executable(binding) is True

    usage = store.usage_of(bound_template.template_id)
    assert usage is not None and usage["success_count"] == 1


async def test_an_incompatible_page_refuses_to_bind(
    driver: PlaywrightDriver, fixture_server: str, store: Store
) -> None:
    """Phase 2 acceptance criterion 5: refusal is what asks for a recompile."""
    binder = Binder(driver, KeepsRisk())

    session = await driver.open_session(OpenSessionRequest(url=f"{fixture_server}/search-page/"))
    store.save_session(session)
    snapshot = await driver.snapshot(session.session_id)
    template = store.save_template(_compile_search_template(snapshot))

    # The page changes underneath the saved template: the search field it names is
    # gone. Nothing about the template is wrong — it simply no longer applies.
    page = driver._session(session.session_id).page  # noqa: SLF001
    await page.evaluate("() => document.getElementById('keyword').remove()")
    changed = await driver.snapshot(session.session_id)

    with pytest.raises(BindingRefused):
        await binder.bind(session.session_id, changed, [template])

    store.record_bind_outcome(template.template_id, succeeded=False)
    usage = store.usage_of(template.template_id)
    assert usage is not None and usage["failure_count"] == 1


async def test_persisted_templates_are_not_executable_on_their_own(
    driver: PlaywrightDriver, fixture_server: str, store: Store
) -> None:
    """Phase 2 acceptance criterion 6.

    Reading a template back gives a plan and a set of locator chains, and no way to
    act: there is no element ID, no session, and no marker anywhere in it.
    """
    session = await driver.open_session(OpenSessionRequest(url=f"{fixture_server}/search-page/"))
    store.save_session(session)
    snapshot = await driver.snapshot(session.session_id)
    stored = store.save_template(_compile_search_template(snapshot))

    (reloaded,) = store.find_templates(stored.origin, stored.normalized_route_pattern)
    serialized = reloaded.model_dump_json()

    assert "session_id" not in serialized
    assert snapshot.session_id not in serialized
    # Element references survive as evidence, and every one of them says which
    # snapshot it came from — a record of an observation, not a way to reach a page.
    assert all(item.snapshot_id is not None for item in reloaded.evidence if item.element_id)
    assert "resolved_targets" not in serialized


async def test_reinspecting_an_unchanged_page_reads_it_the_same_way(
    driver: PlaywrightDriver, fixture_server: str
) -> None:
    """Phase 2 acceptance criterion 3, against a real page rather than a fixture model."""
    session = await driver.open_session(OpenSessionRequest(url=f"{fixture_server}/product-list/"))
    first = build_page_ir(await driver.snapshot(session.session_id))
    second = build_page_ir(await driver.snapshot(session.session_id))

    assert first.snapshot_id != second.snapshot_id
    assert semantically_equivalent(first, second)


async def test_an_invalid_driver_payload_is_rejected_with_an_actionable_error() -> None:
    """Phase 2 acceptance criterion 2.

    The message has to name the field and say what was wrong with it; "validation
    error" alone leaves a caller with a rejected payload and no next move.
    """
    with pytest.raises(ValidationError) as rejected:
        PageSnapshot.model_validate(
            {
                "session_id": "sess_1",
                "document_id": "doc_1",
                "snapshot_id": "snap_1",
                "captured_at": "not-a-timestamp",
                "url": "https://shop.example/search",
                "origin": "https://shop.example",
                "normalized_route_pattern": "/search",
                "structural_hash": "sha256:one",
                "page_signature": "sha256:two",
            }
        )
    message = str(rejected.value)
    assert "captured_at" in message
    assert "not-a-timestamp" in message


async def test_an_unknown_field_from_a_driver_is_refused() -> None:
    """``extra="forbid"`` is what turns a renamed field into an error, not a silent loss."""
    with pytest.raises(ValidationError) as rejected:
        PageSnapshot.model_validate(
            {
                "session_id": "sess_1",
                "document_id": "doc_1",
                "snapshot_id": "snap_1",
                "captured_at": "2026-08-09T00:00:00+00:00",
                "url": "https://shop.example/search",
                "origin": "https://shop.example",
                "normalized_route_pattern": "/search",
                "structural_hash": "sha256:one",
                "page_signature": "sha256:two",
                "pge_signature": "sha256:typo",
            }
        )
    assert "pge_signature" in str(rejected.value)
