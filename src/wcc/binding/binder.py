"""Template lookup and binding (specification 7.10).

The binder is where "we have a saved template" becomes "this template works on the
page in front of us", and the whole module is arranged around refusing to be
half-sure about that.

Specification 7.10 step 6 says binding must not partially succeed. That is not a
rule a caller can be trusted to follow, because the natural way to write the loop —
resolve a target, record it, resolve the next — has already recorded something by
the time the third one fails. So every target is resolved into a local dictionary
first, the full set is compared against what the template requires, and only then
is a :class:`BoundCapability` constructed. Nothing is persisted on any other path.
"""

from __future__ import annotations

from collections.abc import Sequence
from datetime import UTC, datetime
from typing import Protocol, runtime_checkable

from wcc.driver.base import DriverError, ElementNotResolvedError
from wcc.identifiers import new_id
from wcc.models.capability import BoundCapability, CapabilityTemplate, Risk, risk_order
from wcc.models.locator import LocatorChain
from wcc.models.session import PageSnapshot

#: How many candidate templates are tried before the binder gives up and asks for
#: a recompile. A page class with more than a few saved templates is a sign the
#: compiler is producing near-duplicates, and trying all of them would turn one
#: slow bind into an unbounded one.
MAX_BIND_ATTEMPTS = 3


class BindingRefused(DriverError):
    """No template could be bound, so the caller must compile a new one.

    A refusal, not a failure: specification 7.10 step 6 makes recompilation the
    defined next move, and the caller needs to tell that apart from a driver that
    broke.
    """

    code = "binding_refused"

    def __init__(self, message: str, reasons: dict[str, str] | None = None) -> None:
        super().__init__(message)
        #: Each candidate's own reason, by template ID. The message joins them for
        #: a log; a caller reporting one template needs its reason without the
        #: wrapping, which would otherwise spend a short display budget on itself.
        self.reasons: dict[str, str] = reasons or {}


@runtime_checkable
class ElementResolver(Protocol):
    """Re-resolve a stored locator chain against the live page.

    Narrower than the driver on purpose. The binder needs one question answered and
    should not be handed a whole driver to answer it — and specification 4.1's
    five-method ``Driver`` protocol stays as the specification states it, with this
    sitting beside it rather than growing it.
    """

    async def resolve_chain(
        self, session_id: str, chain: LocatorChain, *, described_as: str = ...
    ) -> str: ...


@runtime_checkable
class RiskClassifier(Protocol):
    """Re-classify a template's risk against the page in front of us.

    Specification 7.10 step 4 requires this at binding time, while the real
    classifier is Phase 3 work. Injecting it keeps the refusal path — step 5's "risk
    has not increased" — built and tested now, so Phase 3 supplies rules rather than
    control flow.
    """

    def classify(self, template: CapabilityTemplate, snapshot: PageSnapshot) -> Risk: ...


def risk_increased(before: Risk, after: Risk) -> bool:
    """Whether re-classification made the capability more dangerous.

    Only the level is compared. The other fields describe *why* a level was chosen,
    and a template whose side effect is newly irreversible will have moved level
    too — if it has not, the classifier disagrees with itself and Phase 3 owns that.
    """
    return risk_order(after.level) > risk_order(before.level)


def order_candidates(
    templates: Sequence[CapabilityTemplate], page_signature: str
) -> list[CapabilityTemplate]:
    """Put the templates most likely to bind first, in a total order.

    Specification 7.10 step 2 prefers an exact ``page_signature`` match and says
    nothing about ties. Leaving ties unordered would make binding depend on the
    order SQLite happened to return rows in, so the remaining keys make the order
    total: newest first, then the ID as a final tiebreak that can never tie.
    """
    return sorted(
        templates,
        key=lambda template: (
            0 if template.page_signature == page_signature else 1,
            -template.created_at.timestamp(),
            template.template_id,
        ),
    )


class Binder:
    """Turn a saved template into something executable, or refuse."""

    def __init__(
        self,
        resolver: ElementResolver,
        risk_classifier: RiskClassifier,
        *,
        max_attempts: int = MAX_BIND_ATTEMPTS,
    ) -> None:
        self._resolver = resolver
        self._risk_classifier = risk_classifier
        self._max_attempts = max_attempts

    async def bind(
        self,
        session_id: str,
        snapshot: PageSnapshot,
        candidates: Sequence[CapabilityTemplate],
    ) -> tuple[BoundCapability, CapabilityTemplate]:
        """Bind the first candidate that fully resolves, or raise :class:`BindingRefused`.

        Trying the next candidate after one fails is not the partial binding
        specification 7.10 forbids. That rule is about a single template's targets;
        two templates for the same page class are two separate all-or-nothing
        attempts, and stopping at the first failure would throw away a template that
        works because an older one did not.
        """
        ordered = order_candidates(candidates, snapshot.page_signature)[: self._max_attempts]
        if not ordered:
            raise BindingRefused(
                f"no saved template for {snapshot.origin}{snapshot.normalized_route_pattern}"
            )

        reasons: dict[str, str] = {}
        for template in ordered:
            try:
                binding = await self._bind_one(session_id, snapshot, template)
            except BindingRefused as refused:
                reasons[template.template_id] = refused.message
                continue
            return binding, template

        joined = "; ".join(f"{template_id}: {reason}" for template_id, reason in reasons.items())
        raise BindingRefused(
            f"none of {len(ordered)} candidate templates bound ({joined})", reasons
        )

    async def _bind_one(
        self,
        session_id: str,
        snapshot: PageSnapshot,
        template: CapabilityTemplate,
    ) -> BoundCapability:
        if template.origin != snapshot.origin:
            raise BindingRefused(
                f"template origin {template.origin} is not the page's {snapshot.origin}"
            )

        reclassified = self._risk_classifier.classify(template, snapshot)
        if risk_increased(template.risk, reclassified):
            raise BindingRefused(
                f"risk rose from {template.risk.level} to {reclassified.level} on this page"
            )

        # Resolved locally and completely before anything is built. A dictionary
        # that ends up short is discarded with the attempt; nothing outside this
        # frame ever sees a partial result.
        resolved: dict[str, str] = {}
        for name, chain in template.target_locators.items():
            try:
                resolved[name] = await self._resolver.resolve_chain(
                    session_id, chain, described_as=f"{template.name}.{name}"
                )
            except ElementNotResolvedError as unresolved:
                raise BindingRefused(str(unresolved)) from unresolved

        required = template.required_targets()
        if set(resolved) != set(required):
            # Unreachable while the loop above covers every key, and checked anyway:
            # this is the invariant specification 7.10 step 6 actually names, and a
            # future edit to the loop must fail here rather than bind partially.
            raise BindingRefused(
                f"resolved {sorted(resolved)} but the template requires {sorted(required)}"
            )

        return BoundCapability(
            capability_id=new_id("cap"),
            template_id=template.template_id,
            session_id=session_id,
            document_id=snapshot.document_id,
            bound_snapshot_id=snapshot.snapshot_id,
            resolved_targets=resolved,
            bound_at=datetime.now(UTC),
        )
