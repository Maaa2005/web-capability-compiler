"""The ``Driver`` interface every browser backend implements (specification 4.1).

The runtime talks to a browser only through this protocol, so the post-MVP Chrome
extension driver can replace Playwright without the compiler, binder, or executor
changing. Two rules in this module carry most of that weight:

- a driver never accepts a raw selector from outside; it addresses elements by the
  internal ``element_id`` and resolves them through the locator chain of
  specification 7.7;
- a driver reports attempted redirects, downloads, popups, and new pages instead of
  following them, leaving the decision to the runtime policy layer.

Errors carry a stable ``code`` because specification 11.2 requires stale and
blocked outcomes to be distinguishable by an agent, not just readable by a human.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Protocol, runtime_checkable

from wcc.limits import ResourceLimits
from wcc.models.action import ActionObservation, NavigationEvent, PlannedAction
from wcc.models.session import OpenSessionRequest, PageSnapshot, SessionInfo


class DriverError(Exception):
    """Base class for every failure a driver reports to the runtime."""

    code = "driver_error"

    def __init__(self, message: str) -> None:
        super().__init__(message)
        self.message = message


class SessionNotFoundError(DriverError):
    """The named session was never opened, or has already been closed."""

    code = "session_not_found"


class SessionLimitExceededError(DriverError):
    """Opening another session would exceed the configured maximum."""

    code = "session_limit_exceeded"


class NavigationBlockedError(DriverError):
    """A navigation was refused before it was committed (specification 10.2)."""

    code = "navigation_blocked"


class DriverTimeoutError(DriverError):
    """A page load, settle wait, or action exceeded its configured budget."""

    code = "driver_timeout"


class LimitExceededError(DriverError):
    """A resource limit of specification 10.5 was reached.

    Separate from :class:`DriverTimeoutError` because the two ask different things
    of a caller: a timeout may be worth retrying, while a page carrying more
    elements than the limit allows will carry just as many next time.
    """

    code = "limit_exceeded"


class SessionFailedError(DriverError):
    """The session was closed because something happened that must not be survived.

    Specification 10.2's boundary is only a boundary if breaching it ends the
    session. A page that reached an address the policy would have refused, or that
    committed a document the route handler never saw, has already had whatever
    effect it was going to have; continuing to observe it would let a compiled
    capability rest on evidence gathered after the breach.
    """

    code = "session_failed"


class ElementNotResolvedError(DriverError):
    """No locator candidate produced exactly one match (specification 7.7).

    Zero matches and ambiguous matches end here alike: the driver never picks the
    closest element.
    """

    code = "element_not_resolved"


class DriverNotReadyError(DriverError):
    """A driver capability exists in the interface but not yet in this build.

    Phase 0 established the convention that unimplemented surface names the task
    that will implement it rather than returning something that looks like success.
    """

    code = "driver_not_ready"


@dataclass(frozen=True)
class DriverSettings:
    """What the driver is allowed to do, and how much of it.

    Everything a driver may spend now comes from :class:`ResourceLimits`, which is
    the single home specification 10.5 asks for. What remains here is the one
    setting that is a permission rather than a budget.
    """

    allow_private_network: bool = False
    """The developer-only allowance of specification 10.2.

    Never derived from a request. It exists so the loopback fixture server is
    reachable in development, and it is unsafe to enable for untrusted agents.
    """

    limits: ResourceLimits = field(default_factory=ResourceLimits)


@dataclass(frozen=True)
class DocumentIdentity:
    """Who and when an observation is about, before anything is extracted from it.

    Specification 7.2 splits two lifetimes that are easy to conflate:
    ``document_id`` survives same-document DOM updates and changes only after a
    committed navigation to a new document, while ``snapshot_id`` is minted for
    every observation. Assembling a :class:`PageSnapshot` needs the element
    normalization of task T4 and the fingerprints of task T7; identity is the part
    the driver can report on its own, and specification 4.2 keeps IR construction
    in the runtime rather than in the driver.
    """

    session_id: str
    document_id: str
    snapshot_id: str
    captured_at: datetime
    url: str
    origin: str
    normalized_route_pattern: str
    title: str | None = None
    language: str | None = None


@runtime_checkable
class PageSignals(Protocol):
    """The observation duties specification 4.2 assigns to a driver.

    These sit beside :class:`Driver` rather than inside it so the five-method
    interface of specification 4.1 stays exactly as the specification states it.
    """

    async def identify(self, session_id: str) -> DocumentIdentity: ...

    def drain_navigation_events(self, session_id: str) -> list[NavigationEvent]: ...


@runtime_checkable
class NavigationGuard(Protocol):
    """The runtime's decision point for every URL a driver is about to commit to.

    Phase 1 task T3 supplies the real URL policy and redirect revalidation. The
    driver calls this before a navigation is committed and again for each observed
    redirect hop.
    """

    def check(self, url: str, *, allow_private_network: bool) -> None:
        """Raise :class:`NavigationBlockedError` to refuse ``url``."""
        ...


@runtime_checkable
class Driver(Protocol):
    """Specification 4.1. All browser access lives behind these five methods."""

    async def open_session(self, request: OpenSessionRequest) -> SessionInfo: ...

    async def close_session(self, session_id: str) -> None: ...

    async def snapshot(self, session_id: str) -> PageSnapshot: ...

    async def execute_action(
        self,
        session_id: str,
        action: PlannedAction,
    ) -> ActionObservation: ...

    async def wait_settled(self, session_id: str, timeout_ms: int) -> None: ...


__all__ = [
    "DocumentIdentity",
    "Driver",
    "DriverError",
    "DriverNotReadyError",
    "DriverSettings",
    "DriverTimeoutError",
    "ElementNotResolvedError",
    "LimitExceededError",
    "NavigationBlockedError",
    "NavigationGuard",
    "PageSignals",
    "ResourceLimits",
    "SessionFailedError",
    "SessionLimitExceededError",
    "SessionNotFoundError",
]
