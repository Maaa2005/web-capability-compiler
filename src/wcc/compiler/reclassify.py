"""Re-classifying a saved template's risk against the page in front of us.

Specification 7.10 step 4 re-runs deterministic risk classification at binding
time, and step 5 refuses when the risk has increased. Phase 2 built the refusal
path against an injected classifier; this is the classifier.

The work is not the classification — :func:`wcc.compiler.risk.classify_plan`
already does that — but finding out *what the plan now points at*. A template holds
locator chains, and the classifier needs normalized elements, so every target has
to be matched back to something in the current snapshot. Three things can happen,
and all three matter:

matched uniquely
    The ordinary case. The element is handed to the same classifier the compiler
    used, so a form that has become a POST since it was compiled reads as high
    today even though it was low yesterday.

matched as a container
    Specification 7.11's shared navigation capabilities target a container and let
    an argument choose a member. The container is not itself an element the
    extractor reports, so its members are gathered instead — and they are required
    to agree about everything the classifier reads, because a list where one member
    submits a POST is not a list whose risk one member can describe.

not matched
    Reported as high, which is above the only level that executes and so refuses
    the bind. The alternative reading — "it is not there, so it cannot do
    anything, so it is safe" — inverts specification 10.1's rule that uncertainty
    raises.
"""

from __future__ import annotations

from wcc.compiler.risk import classify_plan, names_destructive_act
from wcc.models.capability import CapabilityTemplate, Risk, RiskLevel, SideEffect
from wcc.models.element import NormalizedElement
from wcc.models.locator import LocatorCandidate, LocatorChain, LocatorKind
from wcc.models.session import PageSnapshot

#: What the classifier reads off an element, apart from its label. Members of a
#: container must agree on all of it before one of them speaks for the rest.
#:
#: The label is excluded and then checked separately, because a result list's
#: labels are its content: requiring them to match would mean no list of products
#: with different names ever binds. What the label is actually read for — whether
#: it names a destructive act — is asked of every member instead.
_RISK_SIGNATURE = (
    "control_type",
    "submits_form",
    "form_method",
    "form_action_kind",
)


def _identifies(candidate: LocatorCandidate, element: NormalizedElement) -> bool:
    """Whether ``candidate`` is one of the ways the extractor described ``element``."""
    return any(
        other.kind is candidate.kind
        and other.key == candidate.key
        and other.role == candidate.role
        and other.form_id == candidate.form_id
        for other in element.locator_chain.candidates
    )


def _match_one(chain: LocatorChain, elements: list[NormalizedElement]) -> NormalizedElement | None:
    """The single element this chain describes, in candidate priority order.

    Exactly one, or nothing. An ambiguous candidate is passed over rather than
    resolved by position, for the same reason specification 7.7 gives at
    resolution time: the closest match is how automation acts on the wrong element
    with full confidence.
    """
    for candidate in chain.ordered():
        matches = [element for element in elements if _identifies(candidate, element)]
        if len(matches) == 1:
            return matches[0]
    return None


def _structural_key(chain: LocatorChain) -> str | None:
    for candidate in chain.ordered():
        if candidate.kind is LocatorKind.STRUCTURAL_PATH:
            return candidate.key
    return None


def _signature(element: NormalizedElement) -> tuple[object, ...]:
    return tuple(getattr(element, field) for field in _RISK_SIGNATURE)


def _match_container(
    chain: LocatorChain, elements: list[NormalizedElement]
) -> NormalizedElement | None:
    container = _structural_key(chain)
    if container is None:
        return None
    return container_representative(container, elements)


def container_representative(
    container: str, elements: list[NormalizedElement]
) -> NormalizedElement | None:
    """A representative of the members inside a container target, if they agree.

    Disagreement returns nothing, which refuses the bind. A result list that has
    acquired one member submitting a POST is exactly the page this check exists
    for: the capability would still resolve, still look like the one compiled
    yesterday, and now be able to do something none of its evidence describes.

    The representative is the worst member, not the first. A list of thirty
    products where the twenty-ninth link says "Delete account" is a list whose risk
    is that link's, and choosing by position would rate it by the first product's.

    Public because compilation has to rate the container by these same members.
    The compiler knows the records it chose, and the container path it saves can
    hold more than those: on djangoproject.com the chosen records were the first
    link of each footer column, the path was the footer, and "Donate to Django" sat
    in a column below. Rated by its records the template saved low; re-classified
    by its members it read critical, so it was compiled and then never bound.
    """
    prefix = f"{container}>"
    members = [
        element
        for element in elements
        if (path := _structural_key(element.locator_chain)) is not None and path.startswith(prefix)
    ]
    if not members:
        return None
    first = _signature(members[0])
    if any(_signature(member) != first for member in members[1:]):
        return None
    return next(
        (member for member in members if names_destructive_act(member.resolved_label)),
        members[0],
    )


def current_targets(
    template: CapabilityTemplate, snapshot: PageSnapshot
) -> dict[str, NormalizedElement] | None:
    """Every target of ``template`` as the current snapshot describes it, or ``None``.

    All or nothing, deliberately. A partial mapping would let the classifier judge
    a plan by the steps it could still see, and the step it could not see is the
    one worth knowing about.
    """
    targets: dict[str, NormalizedElement] = {}
    for ref, chain in template.target_locators.items():
        element = _match_one(chain, snapshot.elements) or _match_container(chain, snapshot.elements)
        if element is None:
            return None
        targets[ref] = element
    return targets


class SnapshotRiskClassifier:
    """Specification 7.10 step 4, as the :class:`~wcc.binding.binder.RiskClassifier`."""

    def classify(self, template: CapabilityTemplate, snapshot: PageSnapshot) -> Risk:
        targets = current_targets(template, snapshot)
        if targets is None:
            # The same reading ``classify_plan`` gives an activation it cannot
            # identify, and said in the same words: not known to be dangerous, not
            # known to be safe, and uncertainty raises. Naming it destructive would
            # refuse just as firmly while telling an operator something the
            # evidence does not support.
            return Risk(
                level=RiskLevel.HIGH,
                side_effect=SideEffect.PERSISTENT,
                reversible=False,
                requires_confirmation=True,
            )
        risk, _ = classify_plan(template.category, template.execution_plan_template, targets)
        return risk
