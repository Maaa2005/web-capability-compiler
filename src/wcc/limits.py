"""The resource limits of specification 10.5.

They live in the policy layer rather than in the driver because specification 4.2
keeps policy out of the driver, and because a limit is only meaningful if the
component that enforces it cannot also raise it. A driver reads these; it never
writes them.

Specification 10.5 states one rule about what exceeding a limit may do, and the
whole module is shaped by it:

    Exceeding a limit must return a structured error, not silently truncate
    executable evidence. Non-critical display data may be truncated only when the
    truncation is explicitly reported in IR warnings.

So the limits divide in two. A limit guarding something an agent will later *act
on* — the elements it may address, the actions a plan may contain, the redirect
chain a navigation may take — raises :class:`LimitExceededError`. A limit guarding
something an agent only *reads* — how many records of a collection are kept, how
long a stored display string may be — truncates and says so in the snapshot's
warnings. Truncating executable evidence would produce a capability that looks
complete and is not, which is the failure the rule exists to prevent.

This module sits above both packages that need it and imports nothing from either.
It cannot live in ``policy/``: ``policy.url_policy`` already imports
``driver.base`` for its error type, so a ``driver.base`` importing anything from
``policy`` closes the loop through the package ``__init__`` files. The error
raised on exceeding a limit lives with the other driver errors in
:mod:`wcc.driver.base`, which keeps the numbers here free of every import.
"""

from __future__ import annotations

from dataclasses import dataclass


@dataclass(frozen=True)
class ResourceLimits:
    """Every limit specification 10.5 names, with conservative defaults.

    The defaults are sized for the fixtures and for ordinary pages, not for the
    largest page imaginable. A limit that is never reached documents nothing and
    protects nothing.
    """

    max_active_sessions: int = 8
    max_redirects_per_navigation: int = 10
    max_elements_per_snapshot: int = 500
    max_collections_per_snapshot: int = 50
    max_records_per_collection: int = 200
    max_stored_string_length: int = 4_096
    max_actions_per_plan: int = 20

    page_load_timeout_ms: int = 20_000
    settle_timeout_ms: int = 5_000
    execution_timeout_ms: int = 10_000

    #: Phase 2 owns persistence, so nothing reads this yet. It is declared here so
    #: the specification 10.5 set is complete in one place and the persistence work
    #: does not invent a second home for it.
    max_database_bytes: int = 512 * 1024 * 1024

    #: How long the page must be quiet before :meth:`wait_settled` calls it settled.
    #: Not a specification 10.5 limit — it is the resolution of the settle
    #: definition, and it belongs beside the timeout that bounds it.
    settle_quiet_period_ms: int = 400


DEFAULT_LIMITS = ResourceLimits()
