"""Browser drivers.

Importing :mod:`wcc.driver.playwright_driver` pulls in Playwright, so it is not
re-exported here: a caller that only needs the interface or the error taxonomy
should not pay for a browser binding.
"""

from wcc.driver.base import (
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

__all__ = [
    "Driver",
    "DriverError",
    "DriverNotReadyError",
    "DriverSettings",
    "DriverTimeoutError",
    "ElementNotResolvedError",
    "NavigationBlockedError",
    "NavigationGuard",
    "SessionLimitExceededError",
    "SessionNotFoundError",
]
