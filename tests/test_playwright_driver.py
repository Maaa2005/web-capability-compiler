"""Playwright driver behaviour against the loopback fixture server.

Reaching ``127.0.0.1`` is the explicit developer private-network allowance of
specification 10.2, so every request here sets ``allow_private_network=True``.

These tests require the Playwright Chromium build and fail loudly without it. A
skip would hide the fact that the driver went unverified; installing the browser
is a documented setup step that ``wcc install-browser`` (task T9) will automate.
"""

from __future__ import annotations

import asyncio
from collections.abc import AsyncIterator

import pytest
from playwright.async_api import Error as PlaywrightError

from wcc.driver.base import (
    DriverNotReadyError,
    DriverSettings,
    DriverTimeoutError,
    ElementNotResolvedError,
    NavigationBlockedError,
    SessionLimitExceededError,
    SessionNotFoundError,
)
from wcc.driver.playwright_driver import PlaywrightDriver
from wcc.models.action import PlannedAction
from wcc.models.common import ActionType, Confidence, ControlType, LabelSource, SessionStatus
from wcc.models.session import OpenSessionRequest
from wcc.policy.url_policy import UrlPolicy

pytestmark = pytest.mark.asyncio


@pytest.fixture
async def driver() -> AsyncIterator[PlaywrightDriver]:
    instance = PlaywrightDriver(
        UrlPolicy(),
        DriverSettings(max_active_sessions=2, allow_private_network=True),
    )
    try:
        yield instance
    finally:
        await instance.shutdown()


async def _open(driver: PlaywrightDriver, url: str) -> str:
    info = await driver.open_session(OpenSessionRequest(url=url))
    return info.session_id


async def test_open_session_reports_an_active_session(
    driver: PlaywrightDriver, fixture_server: str
) -> None:
    info = await driver.open_session(OpenSessionRequest(url=f"{fixture_server}/search-page/"))
    assert info.session_id.startswith("sess_")
    assert info.status is SessionStatus.ACTIVE
    assert info.created_at.tzinfo is not None
    await driver.close_session(info.session_id)


async def test_closing_twice_reports_the_session_as_gone(
    driver: PlaywrightDriver, fixture_server: str
) -> None:
    session_id = await _open(driver, f"{fixture_server}/search-page/")
    await driver.close_session(session_id)
    with pytest.raises(SessionNotFoundError):
        await driver.close_session(session_id)


async def test_a_disallowed_scheme_is_refused_before_a_browser_starts(
    driver: PlaywrightDriver,
) -> None:
    with pytest.raises(NavigationBlockedError):
        await driver.open_session(OpenSessionRequest(url="file:///etc/passwd"))


async def test_loopback_needs_the_explicit_private_network_allowance(
    fixture_server: str,
) -> None:
    """The allowance is configuration, not something a request may ask for."""
    assert "allow_private_network" not in OpenSessionRequest.model_fields

    strict = PlaywrightDriver(UrlPolicy(), DriverSettings())
    try:
        with pytest.raises(NavigationBlockedError):
            await strict.open_session(OpenSessionRequest(url=f"{fixture_server}/search-page/"))
    finally:
        await strict.shutdown()


async def test_the_active_session_limit_is_enforced(
    driver: PlaywrightDriver, fixture_server: str
) -> None:
    url = f"{fixture_server}/search-page/"
    first = await _open(driver, url)
    second = await _open(driver, url)
    with pytest.raises(SessionLimitExceededError):
        await driver.open_session(OpenSessionRequest(url=url))
    await driver.close_session(first)
    await driver.close_session(second)


async def test_settling_a_static_fixture_succeeds(
    driver: PlaywrightDriver, fixture_server: str
) -> None:
    session_id = await _open(driver, f"{fixture_server}/search-page/")
    await driver.wait_settled(session_id, timeout_ms=5_000)
    assert driver.drain_navigation_events(session_id) == []


async def test_open_session_reports_the_first_document_and_snapshot(
    driver: PlaywrightDriver, fixture_server: str
) -> None:
    info = await driver.open_session(OpenSessionRequest(url=f"{fixture_server}/search-page/"))
    assert info.current_document_id is not None
    assert info.current_document_id.startswith("doc_")
    assert info.current_snapshot_id is not None
    assert info.current_snapshot_id.startswith("snap_")


async def test_identity_describes_the_page(driver: PlaywrightDriver, fixture_server: str) -> None:
    session_id = await _open(driver, f"{fixture_server}/search-page/")
    identity = await driver.identify(session_id)
    assert identity.session_id == session_id
    assert identity.origin == fixture_server
    assert identity.normalized_route_pattern == "/search-page/"
    assert identity.title == "Fixture: Product Search"
    assert identity.language == "en"
    assert identity.captured_at.tzinfo is not None


async def test_every_observation_is_a_new_snapshot_of_the_same_document(
    driver: PlaywrightDriver, fixture_server: str
) -> None:
    session_id = await _open(driver, f"{fixture_server}/search-page/")
    first = await driver.identify(session_id)
    second = await driver.identify(session_id)
    assert first.document_id == second.document_id
    assert first.snapshot_id != second.snapshot_id


async def test_a_same_document_history_update_keeps_the_document_id(
    driver: PlaywrightDriver, fixture_server: str
) -> None:
    """Specification 7.2: ``pushState`` is an update, not a new document.

    Reaching for the page directly stands in for the user gesture that task T5
    will be able to perform through ``execute_action``.
    """
    session_id = await _open(driver, f"{fixture_server}/search-page/")
    before = await driver.identify(session_id)
    page = driver._session(session_id).page  # noqa: SLF001 - stands in for task T5
    await page.evaluate("() => history.pushState({}, '', '?keyword=earbuds')")
    after = await driver.identify(session_id)

    assert after.document_id == before.document_id
    assert after.snapshot_id != before.snapshot_id
    assert after.normalized_route_pattern == "/search-page/?keyword="


async def test_a_committed_navigation_starts_a_new_document(
    driver: PlaywrightDriver, fixture_server: str
) -> None:
    session_id = await _open(driver, f"{fixture_server}/search-page/")
    before = await driver.identify(session_id)
    page = driver._session(session_id).page  # noqa: SLF001 - stands in for task T5
    await page.goto(f"{fixture_server}/product-list/")
    after = await driver.identify(session_id)

    assert after.document_id != before.document_id
    assert after.normalized_route_pattern == "/product-list/"


async def test_a_reload_replaces_the_document(
    driver: PlaywrightDriver, fixture_server: str
) -> None:
    session_id = await _open(driver, f"{fixture_server}/search-page/")
    before = await driver.identify(session_id)
    page = driver._session(session_id).page  # noqa: SLF001 - stands in for task T5
    await page.reload()
    after = await driver.identify(session_id)

    assert after.document_id != before.document_id
    assert after.url == before.url


async def test_a_popup_is_refused_and_closed(driver: PlaywrightDriver, fixture_server: str) -> None:
    """Specification 10.2 rejects popups, tabs, and windows by default."""
    session_id = await _open(driver, f"{fixture_server}/search-page/")
    session = driver._session(session_id)  # noqa: SLF001
    async with session.context.expect_event("page"):
        await session.page.evaluate("() => window.open('/product-list/', '_blank')")
    await driver.drain_cleanups()

    events = driver.drain_navigation_events(session_id)
    assert [event.kind for event in events] == ["new_page"]
    assert events[0].blocked is True
    assert len(driver._session(session_id).context.pages) == 1  # noqa: SLF001


async def test_a_download_is_refused(driver: PlaywrightDriver, fixture_server: str) -> None:
    session_id = await _open(driver, f"{fixture_server}/search-page/")
    page = driver._session(session_id).page  # noqa: SLF001 - stands in for task T5
    async with page.expect_download():
        await page.evaluate(
            """() => {
                const a = document.createElement('a');
                a.href = 'data:text/plain,hello';
                a.download = 'hello.txt';
                document.body.append(a);
                a.click();
            }"""
        )
    await driver.drain_cleanups()
    events = driver.drain_navigation_events(session_id)
    assert any(event.kind == "download" and event.blocked for event in events)


async def test_a_refused_download_never_reaches_the_filesystem(
    driver: PlaywrightDriver, fixture_server: str
) -> None:
    """Recording the refusal is not enough; the bytes must not be written.

    ``accept_downloads`` defaults to true, which saves the file first and lets a
    handler cancel afterwards — a race a small payload wins.
    """
    session_id = await _open(driver, f"{fixture_server}/search-page/")
    page = driver._session(session_id).page  # noqa: SLF001 - stands in for task T5

    async with page.expect_download() as info:
        await page.evaluate(
            """() => {
                const a = document.createElement('a');
                a.href = 'data:text/plain,hello';
                a.download = 'hello.txt';
                document.body.append(a);
                a.click();
            }"""
        )
    download = await info.value
    await driver.drain_cleanups()

    with pytest.raises(PlaywrightError):
        await download.path()


async def test_concurrent_opens_cannot_exceed_the_session_limit(
    driver: PlaywrightDriver, fixture_server: str
) -> None:
    """Counting sessions before the awaits that create one lets two callers in."""
    url = f"{fixture_server}/search-page/"
    results = await asyncio.gather(
        *(driver.open_session(OpenSessionRequest(url=url)) for _ in range(4)),
        return_exceptions=True,
    )

    opened = [r for r in results if not isinstance(r, BaseException)]
    refused = [r for r in results if isinstance(r, SessionLimitExceededError)]
    assert len(opened) == 2
    assert len(refused) == 2
    assert len(driver._sessions) == 2  # noqa: SLF001


async def test_a_failed_open_leaves_no_session_behind(fixture_server: str) -> None:
    """A context whose ID never reached the caller can never be closed by them."""
    impatient = PlaywrightDriver(
        UrlPolicy(),
        DriverSettings(page_load_timeout_ms=1, allow_private_network=True),
    )
    try:
        with pytest.raises(DriverTimeoutError):
            await impatient.open_session(OpenSessionRequest(url=f"{fixture_server}/search-page/"))
        assert impatient._sessions == {}  # noqa: SLF001
        assert impatient._reserved == set()  # noqa: SLF001
    finally:
        await impatient.shutdown()


async def test_snapshot_still_names_the_tasks_that_will_deliver_it(
    driver: PlaywrightDriver, fixture_server: str
) -> None:
    """Phase 0's convention: never let missing behaviour look like success."""
    session_id = await _open(driver, f"{fixture_server}/search-page/")

    with pytest.raises(DriverNotReadyError) as snapshot_error:
        await driver.snapshot(session_id)
    assert "T7" in str(snapshot_error.value) or "T4" in str(snapshot_error.value)


async def test_a_subresource_to_a_refused_destination_is_blocked(
    driver: PlaywrightDriver, fixture_server: str
) -> None:
    """Guarding documents alone secures the address bar, not the browser context.

    The driver is configured to allow loopback, so the fixture page loads; a
    subresource aimed at a link-local address is still refused, because that
    address is out of bounds under any allowance.
    """
    session_id = await _open(driver, f"{fixture_server}/search-page/")
    page = driver._session(session_id).page  # noqa: SLF001 - stands in for task T5

    reached = await page.evaluate(
        """async () => {
            try {
                await fetch('http://169.254.169.254/latest/meta-data/', {mode: 'no-cors'});
                return true;
            } catch (e) {
                return false;
            }
        }"""
    )

    assert reached is False
    events = driver.drain_navigation_events(session_id)
    assert any(event.kind == "subresource" and event.blocked for event in events)


SENTINELS = (
    "SENTINEL-CARD-4111",
    "SENTINEL-CVC-737",
    "SENTINEL-PASSWORD-HUNTER2",
    "SENTINEL-CSRF-TOKEN",
    "SENTINEL-SESSION-ID",
)


async def test_no_sensitive_sentinel_survives_extraction(
    driver: PlaywrightDriver, fixture_server: str
) -> None:
    """Acceptance criterion 2, tested by absence rather than by inspection.

    Each sensitive control in the fixture carries a distinct sentinel. Serializing
    every extracted element and searching the whole payload means a leak through
    any field — value, label, locator key — fails the test and names the sentinel.
    """
    session_id = await _open(driver, f"{fixture_server}/sensitive-form/")
    elements = await driver.extract_elements(session_id)
    payload = "[" + ",".join(element.model_dump_json() for element in elements) + "]"

    leaked = [sentinel for sentinel in SENTINELS if sentinel in payload]
    assert leaked == []
    assert "PUBLIC-CARDHOLDER-OK" in payload


async def test_sensitive_controls_are_marked_rather_than_dropped(
    driver: PlaywrightDriver, fixture_server: str
) -> None:
    """An omitted value must be visible as a deliberate omission (specification 7.2)."""
    session_id = await _open(driver, f"{fixture_server}/sensitive-form/")
    by_label = {
        element.resolved_label: element for element in await driver.extract_elements(session_id)
    }

    password = by_label["Password"]
    assert password.control_type is ControlType.PASSWORD
    assert password.value is None
    assert password.value_omitted_for_safety is True

    card = by_label["Card number"]
    assert card.value is None
    assert card.value_omitted_for_safety is True


async def test_hidden_inputs_are_not_extracted_at_all(
    driver: PlaywrightDriver, fixture_server: str
) -> None:
    session_id = await _open(driver, f"{fixture_server}/sensitive-form/")
    elements = await driver.extract_elements(session_id)
    assert all(element.control_type is not ControlType.HIDDEN for element in elements)


async def test_the_search_fixture_labels_every_control_from_a_named_source(
    driver: PlaywrightDriver, fixture_server: str
) -> None:
    """Acceptance criterion 1 asks for correct labels and sources, not just labels."""
    session_id = await _open(driver, f"{fixture_server}/search-page/")
    elements = await driver.extract_elements(session_id)
    labelled = [e for e in elements if e.resolved_label and e.label_source]

    assert len(labelled) / len(elements) >= 0.95
    keyword = next(e for e in elements if e.locator_chain.primary.key == "keyword")
    assert keyword.resolved_label == "Search products"
    assert keyword.label_source is LabelSource.LABEL_FOR
    assert keyword.control_type is ControlType.SEARCH
    assert keyword.confidence is Confidence.HIGH


async def test_an_element_re_resolves_after_a_reload(
    driver: PlaywrightDriver, fixture_server: str
) -> None:
    """Acceptance criterion 8: every executable target survives a reload."""
    session_id = await _open(driver, f"{fixture_server}/search-page/")
    elements = await driver.extract_elements(session_id)

    page = driver._session(session_id).page  # noqa: SLF001
    await page.reload()
    await driver.extract_elements(session_id)

    for element in elements:
        assert await driver.resolve(session_id, element.element_id)


async def test_an_ambiguous_candidate_is_skipped_rather_than_guessed(
    driver: PlaywrightDriver, fixture_server: str
) -> None:
    """Two controls share a name and a label; only the id tells them apart."""
    session_id = await _open(driver, f"{fixture_server}/ambiguous-controls/")
    elements = await driver.extract_elements(session_id)
    left = next(e for e in elements if e.locator_chain.primary.key == "left-city")

    marker = await driver.resolve(session_id, left.element_id)
    page = driver._session(session_id).page  # noqa: SLF001
    resolved_id = await page.get_attribute(f"[data-wcc-resolved='{marker}']", "id")
    assert resolved_id == "left-city"


async def test_an_unknown_element_is_reported_as_unresolvable(
    driver: PlaywrightDriver, fixture_server: str
) -> None:
    session_id = await _open(driver, f"{fixture_server}/search-page/")
    with pytest.raises(ElementNotResolvedError):
        await driver.resolve(session_id, "el_never_observed")


async def test_a_removed_element_becomes_stale(
    driver: PlaywrightDriver, fixture_server: str
) -> None:
    """Specification 11.2: a target that no longer resolves is stale, not retried blind."""
    session_id = await _open(driver, f"{fixture_server}/search-page/")
    elements = await driver.extract_elements(session_id)
    keyword = next(e for e in elements if e.locator_chain.primary.key == "keyword")

    page = driver._session(session_id).page  # noqa: SLF001
    await page.evaluate("() => document.getElementById('product-search').remove()")

    with pytest.raises(ElementNotResolvedError, match="stale"):
        await driver.resolve(session_id, keyword.element_id)


async def test_setting_a_value_is_observed(driver: PlaywrightDriver, fixture_server: str) -> None:
    session_id = await _open(driver, f"{fixture_server}/search-page/")
    elements = await driver.extract_elements(session_id)
    keyword = next(e for e in elements if e.locator_chain.primary.key == "keyword")

    observation = await driver.execute_action(
        session_id,
        PlannedAction(action=ActionType.SET_VALUE, element_id=keyword.element_id, value="earbuds"),
    )

    assert observation.succeeded is True
    assert observation.document_changed is False
    refreshed = await driver.extract_elements(session_id)
    assert next(e for e in refreshed if e.locator_chain.primary.key == "keyword").value == "earbuds"


async def test_checking_a_box_requires_the_desired_state(
    driver: PlaywrightDriver, fixture_server: str
) -> None:
    """Specification 10.4 has no state-relative toggle, so the caller states the target."""
    session_id = await _open(driver, f"{fixture_server}/search-page/")
    elements = await driver.extract_elements(session_id)
    in_stock = next(e for e in elements if e.locator_chain.primary.key == "in-stock")

    observation = await driver.execute_action(
        session_id,
        PlannedAction(action=ActionType.SET_CHECKED, element_id=in_stock.element_id, checked=True),
    )
    assert observation.succeeded is True

    refreshed = await driver.extract_elements(session_id)
    assert next(e for e in refreshed if e.locator_chain.primary.key == "in-stock").checked is True


async def test_activating_a_search_button_commits_a_new_document(
    driver: PlaywrightDriver, fixture_server: str
) -> None:
    session_id = await _open(driver, f"{fixture_server}/search-page/")
    elements = await driver.extract_elements(session_id)
    submit = next(
        e for e in elements if e.resolved_label == "Search" and e.control_type is ControlType.BUTTON
    )

    observation = await driver.execute_action(
        session_id, PlannedAction(action=ActionType.ACTIVATE, element_id=submit.element_id)
    )

    assert observation.succeeded is True
    assert observation.document_changed is True
