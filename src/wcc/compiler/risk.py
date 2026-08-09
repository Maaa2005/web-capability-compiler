"""Deterministic risk classification (specification 10.1).

One sentence carries the whole module: risk is decided by side effect, not by
mechanism. The same click is harmless on a search form and irreversible on a
"close account" button, so what the agent *does* tells you nothing — what the page
does in response is the only thing worth classifying.

Everything here is a closed rule over observable page signals. No heuristic that
"usually works", no provider-specific exception, and no LLM: specification 10.1
says uncertainty always raises the level and never lowers it, and a guess that
lowers risk is the one mistake with no recovery.

The rules, in the order they are applied:

``POST`` is high, always
    Specification 10.1 states it outright and adds that there is no fixture-only or
    provider-specific exception for a "read-only POST search". A site may well
    implement search over POST; WCC will decline to run it rather than decide from
    outside which POSTs are safe.

A GET submission that only reads is low
    A GET search or filter, a query-parameter navigation, a pagination or sorting
    link. These are the deterministic low signals the specification lists.

Anything else is medium at least
    Not because it is known to be dangerous, but because it is not known to be
    safe, and that is the direction uncertainty moves.
"""

from __future__ import annotations

import re

from wcc.models.capability import (
    RISK_OF_SIDE_EFFECT,
    CapabilityCategory,
    PlanStep,
    Risk,
    RiskLevel,
    SideEffect,
)
from wcc.models.common import ActionType, ControlType, FormActionKind
from wcc.models.element import NormalizedElement

#: Actions that change nothing on the server. ``SET_VALUE`` is here because typing
#: into a field is local until something submits it, and the submission is a
#: separate step this classifier sees separately.
_READ_ONLY_ACTIONS = frozenset(
    {
        ActionType.FOCUS,
        ActionType.SCROLL_INTO_VIEW,
        ActionType.SET_VALUE,
        ActionType.SELECT_OPTION,
        ActionType.SET_CHECKED,
    }
)

#: Words that name a consequence no read-only reading survives. Matched against a
#: submit's own label, and used only to *raise*: a button saying "delete" on a form
#: that looks like a search is not a search, while a form with no such word is not
#: thereby proved safe.
#:
#: "Subscribe" is deliberately absent. Subscribing creates persistent state rather
#: than destroying anything, and the POST rule below already refuses it — naming it
#: destructive would report the wrong reason for the right refusal, and the reason
#: is what a person reads when deciding whether the refusal was correct.
_DESTRUCTIVE_WORDS = re.compile(
    r"(delete|remove|cancel|close\s+account|deactivate|unsubscribe|"
    r"pay|purchase|checkout|order|buy|donate|transfer|withdraw|"
    r"削除|退会|解約|購入|注文|決済|支払|送金)",
    re.IGNORECASE,
)


def _reason(text: str) -> str:
    return text


def classify_plan(
    category: CapabilityCategory | None,
    plan: list[PlanStep],
    targets: dict[str, NormalizedElement],
) -> tuple[Risk, str]:
    """Classify a capability candidate, returning the risk and why.

    ``category`` may be ``None``, and that case is not a gap. A form the compiler
    declined to categorize still deserves a classification — specification 8.3 has
    it reported rather than compiled, and a refusal without a reason invites the
    agent to find another route to the same button. An uncategorized candidate
    simply never reaches the read-only reading below, because the deterministic
    low signals of 10.1 are all statements about a known category.

    The reason is returned rather than logged because a refusal an agent cannot
    explain is a refusal a user will work around. Specification 16 asks for the
    signals behind a decision to be observable, and "medium" on its own is not.
    """
    submits = [
        targets[step.target_ref]
        for step in plan
        if step.action is ActionType.ACTIVATE and step.target_ref in targets
    ]

    # Nothing is activated at all: the plan only fills fields and reads back.
    if not submits:
        if all(step.action in _READ_ONLY_ACTIONS for step in plan):
            return (
                _risk(SideEffect.LOCAL),
                _reason("the plan only sets local control state and submits nothing"),
            )
        return (
            _risk(SideEffect.PERSISTENT),
            _reason("the plan activates something this classifier could not identify"),
        )

    for submit in submits:
        label = submit.resolved_label or ""
        if _DESTRUCTIVE_WORDS.search(label):
            return (
                _risk(SideEffect.DESTRUCTIVE_OR_FINANCIAL),
                _reason(f"the control labelled {label!r} names a destructive or financial act"),
            )

    for submit in submits:
        method = (submit.form_method or "get").lower()
        if submit.submits_form and method != "get":
            return (
                _risk(SideEffect.PERSISTENT),
                _reason(
                    f"the form submits by {method.upper()}; specification 10.1 makes every "
                    "POST submission high and WCC does not execute one"
                ),
            )

    for submit in submits:
        if submit.form_action_kind is FormActionKind.CROSS_ORIGIN:
            return (
                _risk(SideEffect.PERSISTENT),
                _reason("the form submits to another origin, so its effect cannot be observed"),
            )

    if category is None:
        return (
            _risk(SideEffect.PERSISTENT),
            _reason(
                "the candidate resolved to no category, and every deterministic "
                "read-only signal in specification 10.1 presupposes one"
            ),
        )

    for submit in submits:
        if not _is_read_only_activation(submit):
            return (
                _risk(SideEffect.PERSISTENT),
                _reason(
                    "the activated control is neither a GET submission nor a "
                    "navigation link, so its effect is unknown"
                ),
            )

    return (
        _risk(SideEffect.NONE),
        _reason("a GET submission or navigation link, which reads without changing state"),
    )


def _is_read_only_activation(element: NormalizedElement) -> bool:
    """Whether activating this control is one of specification 10.1's low signals.

    A GET form submission, or a link — including a pagination link, which is a link
    whose destination differs only by a page number.
    """
    if element.submits_form:
        return (element.form_method or "get").lower() == "get"
    return element.control_type is ControlType.LINK


def _risk(side_effect: SideEffect) -> Risk:
    """Build the risk for a side effect, at exactly the level 10.1's table gives.

    The level is never chosen independently of the side effect. Deriving it from
    the one table is what stops a caller naming a consequence and then disagreeing
    with the specification about how serious it is.
    """
    level = RISK_OF_SIDE_EFFECT[side_effect]
    return Risk(
        level=level,
        side_effect=side_effect,
        reversible=side_effect in {SideEffect.NONE, SideEffect.LOCAL},
        requires_confirmation=level is not RiskLevel.LOW,
    )
