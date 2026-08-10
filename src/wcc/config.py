"""Trusted local configuration, read from the environment (specification 10.2).

One rule shapes this module: nothing here may be set by an agent. The MCP server
takes its instructions from a client over stdio, and everything that arrives that
way is a request. What lives here arrives the other way — from whoever launched
the process — and that is the only reason it is allowed to relax a boundary.

The private-network allowance is the case that matters. Specification 10.2 calls
it developer-only, so it was removed from ``OpenSessionRequest`` in Phase 1; this
is where it legitimately comes from instead.
"""

from __future__ import annotations

import os

from wcc.driver.base import DriverSettings
from wcc.limits import ResourceLimits

ALLOW_PRIVATE_NETWORK_ENV = "WCC_ALLOW_PRIVATE_NETWORK"

#: Values that mean "yes". Anything else — including a typo, an empty string, or
#: the word "false" — means no. A boundary that opened on an unrecognised value
#: would open on a mistake.
_TRUTHY = frozenset({"1", "true", "yes", "on"})


def allow_private_network(environ: dict[str, str] | None = None) -> bool:
    """Whether this process may reach loopback and private addresses."""
    env = os.environ if environ is None else environ
    return env.get(ALLOW_PRIVATE_NETWORK_ENV, "").strip().lower() in _TRUTHY


def driver_settings(environ: dict[str, str] | None = None) -> DriverSettings:
    """The driver's permissions and budgets for this process."""
    return DriverSettings(
        allow_private_network=allow_private_network(environ),
        limits=ResourceLimits(),
    )
