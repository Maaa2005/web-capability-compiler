"""Which sessions are live, which is the only thing that makes a binding usable.

A :class:`~wcc.models.capability.BoundCapability` read back from SQLite looks
exactly like one created a second ago. The difference is not in the record — it is
whether the browser session it names still exists in this process.

So the question "may this be executed?" is never answered by where the record came
from. It is answered here, against live state. A record whose session this process
does not hold is stale, whether it was loaded from disk, restored after a crash, or
held by a caller who kept it too long.
"""

from __future__ import annotations

from wcc.models.capability import BindingStatus, BoundCapability


class SessionRegistry:
    """The live sessions of one process.

    Deliberately in memory and deliberately not persisted. Persisting it would
    recreate the confusion it exists to remove: a restarted process would find
    "active" sessions whose browsers died with the previous one.
    """

    def __init__(self) -> None:
        self._live: set[str] = set()

    def opened(self, session_id: str) -> None:
        self._live.add(session_id)

    def closed(self, session_id: str) -> None:
        self._live.discard(session_id)

    def is_live(self, session_id: str) -> bool:
        return session_id in self._live

    def status_of(self, binding: BoundCapability) -> BindingStatus:
        """Report what the binding's status actually is right now.

        The stored ``status`` records what it was when written. This is the reading
        that matters, and it can only downgrade: a binding already marked stale or
        closed does not become active again because its session happens to be live.
        """
        if binding.status is not BindingStatus.ACTIVE:
            return binding.status
        return BindingStatus.ACTIVE if self.is_live(binding.session_id) else BindingStatus.STALE

    def executable(self, binding: BoundCapability) -> bool:
        """Whether this binding may be handed to an executor at all."""
        return self.status_of(binding) is BindingStatus.ACTIVE
