"""The driver contract that holds without a browser (specification 4.1, 11.2)."""

from __future__ import annotations

import pytest

from wcc.driver import (
    Driver,
    DriverError,
    DriverNotReadyError,
    DriverSettings,
    DriverTimeoutError,
    ElementNotResolvedError,
    NavigationBlockedError,
    NavigationGuard,
    SessionLimitExceededError,
    SessionNotFoundError,
)
from wcc.driver.playwright_driver import PlaywrightDriver
from wcc.identifiers import new_id


class _AllowAll:
    def check(self, url: str, *, allow_private_network: bool) -> None:
        """Accept every URL. Task T3 supplies the real policy."""


def test_playwright_driver_satisfies_the_driver_protocol() -> None:
    assert isinstance(PlaywrightDriver(_AllowAll()), Driver)


def test_a_permissive_stub_still_satisfies_the_navigation_guard_protocol() -> None:
    assert isinstance(_AllowAll(), NavigationGuard)


def test_error_codes_are_distinct_and_stable() -> None:
    """Specification 11.2 asks an agent to tell blocked from stale from missing."""
    errors: list[type[DriverError]] = [
        DriverError,
        SessionNotFoundError,
        SessionLimitExceededError,
        NavigationBlockedError,
        DriverTimeoutError,
        ElementNotResolvedError,
        DriverNotReadyError,
    ]
    codes = [error.code for error in errors]
    assert len(set(codes)) == len(codes)
    assert all(isinstance(error("x"), DriverError) for error in errors)


def test_driver_settings_default_to_bounded_budgets() -> None:
    limits = DriverSettings().limits
    assert limits.max_active_sessions > 0
    assert limits.settle_timeout_ms <= limits.page_load_timeout_ms
    assert limits.settle_quiet_period_ms < limits.settle_timeout_ms


def test_unknown_session_is_rejected_before_any_browser_work() -> None:
    driver = PlaywrightDriver(_AllowAll())
    with pytest.raises(SessionNotFoundError):
        driver.drain_navigation_events(new_id("sess"))


def test_identifiers_are_prefixed_and_unique() -> None:
    assert new_id("sess").startswith("sess_")
    assert new_id("sess") != new_id("sess")
    with pytest.raises(ValueError):
        new_id("")
