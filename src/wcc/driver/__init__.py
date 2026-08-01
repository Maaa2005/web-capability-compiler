"""Browser drivers.

Importing :mod:`wcc.driver.playwright_driver` pulls in Playwright, so it is not
re-exported here: a caller that only needs the interface or the error taxonomy
should not pay for a browser binding.
"""

from wcc.driver.base import (
    DocumentIdentity,
    Driver,
    DriverError,
    DriverNotReadyError,
    DriverSettings,
    DriverTimeoutError,
    ElementNotResolvedError,
    NavigationBlockedError,
    NavigationGuard,
    PageSignals,
    SessionLimitExceededError,
    SessionNotFoundError,
)

__all__ = [
    "DocumentIdentity",
    "Driver",
    "DriverError",
    "DriverNotReadyError",
    "DriverSettings",
    "DriverTimeoutError",
    "ElementNotResolvedError",
    "NavigationBlockedError",
    "NavigationGuard",
    "PageSignals",
    "SessionLimitExceededError",
    "SessionNotFoundError",
]
