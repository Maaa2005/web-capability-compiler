"""Playwright driver behaviour against the loopback fixture server.

Reaching ``127.0.0.1`` is the explicit developer private-network allowance of
specification 10.2, so every request here sets ``allow_private_network=True``.
The tests skip rather than fail when the Chromium build is absent, because the
browser is installed by ``wcc install-browser`` (Phase 1 task T9) and a missing
browser is an environment fact, not a defect in the driver.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest

from wcc.driver.base import (
    DriverNotReadyError,
    DriverSettings,
    NavigationBlockedError,
    SessionLimitExceededError,
    SessionNotFoundError,
)
from wcc.driver.playwright_driver import PlaywrightDriver
from wcc.models.action import PlannedAction
from wcc.models.common import ActionType, SessionStatus
from wcc.models.session import OpenSessionRequest

pytestmark = pytest.mark.asyncio


class _AllowLoopback:
    """Stand-in for the Phase 1 task T3 URL policy.

    It refuses everything that is not ``http``/``https``, and refuses loopback
    unless the caller asked for the private-network allowance.
    """

    def check(self, url: str, *, allow_private_network: bool) -> None:
        scheme, _, rest = url.partition("://")
        if scheme not in {"http", "https"}:
            raise NavigationBlockedError(f"scheme {scheme!r} is not allowed")
        host = rest.split("/")[0].split(":")[0]
        if host in {"127.0.0.1", "localhost", "::1"} and not allow_private_network:
            raise NavigationBlockedError(f"{host} is a private-network destination")


@pytest.fixture
async def driver() -> AsyncIterator[PlaywrightDriver]:
    instance = PlaywrightDriver(_AllowLoopback(), DriverSettings(max_active_sessions=2))
    try:
        yield instance
    finally:
        await instance.shutdown()


async def _open(driver: PlaywrightDriver, url: str) -> str:
    info = await driver.open_session(OpenSessionRequest(url=url, allow_private_network=True))
    return info.session_id


async def test_open_session_reports_an_active_session(
    driver: PlaywrightDriver, fixture_server: str
) -> None:
    info = await driver.open_session(
        OpenSessionRequest(url=f"{fixture_server}/search-page/", allow_private_network=True)
    )
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
    driver: PlaywrightDriver, fixture_server: str
) -> None:
    with pytest.raises(NavigationBlockedError):
        await driver.open_session(OpenSessionRequest(url=f"{fixture_server}/search-page/"))


async def test_the_active_session_limit_is_enforced(
    driver: PlaywrightDriver, fixture_server: str
) -> None:
    url = f"{fixture_server}/search-page/"
    first = await _open(driver, url)
    second = await _open(driver, url)
    with pytest.raises(SessionLimitExceededError):
        await driver.open_session(OpenSessionRequest(url=url, allow_private_network=True))
    await driver.close_session(first)
    await driver.close_session(second)


async def test_settling_a_static_fixture_succeeds(
    driver: PlaywrightDriver, fixture_server: str
) -> None:
    session_id = await _open(driver, f"{fixture_server}/search-page/")
    await driver.wait_settled(session_id, timeout_ms=5_000)
    assert driver.drain_navigation_events(session_id) == []


async def test_unimplemented_surface_names_the_task_that_will_deliver_it(
    driver: PlaywrightDriver, fixture_server: str
) -> None:
    """Phase 0's convention: never let missing behaviour look like success."""
    session_id = await _open(driver, f"{fixture_server}/search-page/")

    with pytest.raises(DriverNotReadyError) as snapshot_error:
        await driver.snapshot(session_id)
    assert "T2" in str(snapshot_error.value)

    with pytest.raises(DriverNotReadyError) as action_error:
        await driver.execute_action(
            session_id,
            PlannedAction(action=ActionType.FOCUS, element_id="el_unknown"),
        )
    assert "T5" in str(action_error.value)
