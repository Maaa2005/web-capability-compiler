"""Template lookup and binding (specification 7.10).

Kept out of ``driver/`` because binding decides things: it re-resolves targets,
re-checks risk, and refuses on either. Specification 4.2 leaves the driver with
observation and allowlisted action, and nothing that decides.
"""

from wcc.binding.binder import (
    MAX_BIND_ATTEMPTS,
    Binder,
    BindingRefused,
    ElementResolver,
    RiskClassifier,
    order_candidates,
    risk_increased,
)
from wcc.binding.registry import SessionRegistry

__all__ = [
    "MAX_BIND_ATTEMPTS",
    "Binder",
    "BindingRefused",
    "ElementResolver",
    "RiskClassifier",
    "SessionRegistry",
    "order_candidates",
    "risk_increased",
]
