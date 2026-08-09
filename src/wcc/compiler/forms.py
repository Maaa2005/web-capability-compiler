"""Compile forms into search and filter capabilities (specification 8.3).

Three things happen here, and they are kept apart on purpose.

*What is this form for* is decided from signals the page states about itself, in a
fixed priority order, and the order is the whole design. The submit control's own
label is consulted first because it is the accessible name of the activation
mechanism — the one piece of text that describes what happens when the capability
runs. Only if that says nothing do weaker signals get a turn: the form's identity,
then where it submits, then its shape. A form that resolves to nothing is a
generic data-entry form, which specification 8.3 says is reported and not
compiled.

*How confident are we* is specification 7.4's table and nothing else. It is
deliberately not a score: a number invites a threshold, a threshold invites
tuning, and tuning a safety boundary against fixtures is how a compiler ends up
confident about the page it was developed on.

*What would running it do* is not decided here at all. It is
:mod:`wcc.compiler.risk`'s answer, computed from the plan this module builds, so
that a form cannot be classified as harmless by the same code that decided it was
a search.

The refusals matter as much as the compilations, so each one has a stable code:
a form with two submits, a required control that cannot become a parameter, and a
form with nothing to submit are all reported rather than compiled around.
"""

from __future__ import annotations

import re
from collections import OrderedDict

from wcc.compiler.risk import classify_plan
from wcc.models.capability import (
    CapabilityCategory,
    ExpectedEffect,
    PlanStep,
    Risk,
)
from wcc.models.common import ActionType, Confidence, ControlType, LabelSource
from wcc.models.element import NormalizedElement
from wcc.models.locator import LocatorChain

#: Controls a person supplies a value to. Everything else in a form is either the
#: activation mechanism or decoration.
_PARAMETER_CONTROLS = frozenset(
    {
        ControlType.TEXT,
        ControlType.SEARCH,
        ControlType.NUMBER,
        ControlType.TEXTAREA,
        ControlType.SELECT,
        ControlType.CHECKBOX,
    }
)

#: The JSON Schema type each control produces, and the action that sets it.
_PARAMETER_SHAPE: dict[ControlType, tuple[str, ActionType]] = {
    ControlType.TEXT: ("string", ActionType.SET_VALUE),
    ControlType.SEARCH: ("string", ActionType.SET_VALUE),
    ControlType.TEXTAREA: ("string", ActionType.SET_VALUE),
    ControlType.NUMBER: ("number", ActionType.SET_VALUE),
    ControlType.SELECT: ("string", ActionType.SELECT_OPTION),
    ControlType.CHECKBOX: ("boolean", ActionType.SET_CHECKED),
}

#: Specification 8.3 asks for a small multilingual keyword list. Small is the
#: point: every word added is a page that gets compiled on the strength of its
#: wording, so the list holds words that name the act and not words that merely
#: co-occur with it. ASCII words are bounded so "research" is not a search.
_SEARCH_WORDS = ("search", "find", "query", "lookup", "keyword", "keywords")
_FILTER_WORDS = ("filter", "refine", "narrow")
_SEARCH_CJK = ("検索", "探す", "さがす", "サーチ")
_FILTER_CJK = ("絞り込", "絞込", "しぼりこ", "フィルタ", "絞り込み")


def _keyword_pattern(ascii_words: tuple[str, ...], cjk_words: tuple[str, ...]) -> re.Pattern[str]:
    ascii_part = r"(?<![a-z])(" + "|".join(ascii_words) + r")(?![a-z])"
    return re.compile("|".join([ascii_part, *(re.escape(w) for w in cjk_words)]), re.IGNORECASE)


_SEARCH_RE = _keyword_pattern(_SEARCH_WORDS, _SEARCH_CJK)
_FILTER_RE = _keyword_pattern(_FILTER_WORDS, _FILTER_CJK)

_NON_IDENTIFIER = re.compile(r"[^0-9a-z]+")


class FormGroup:
    """The eligible controls of one form, in document order."""

    def __init__(self, key: str, elements: list[NormalizedElement]) -> None:
        self.key = key
        self.elements = elements

    @property
    def submits(self) -> list[NormalizedElement]:
        return [e for e in self.elements if e.submits_form]

    @property
    def method(self) -> str:
        for element in self.elements:
            if element.form_method:
                return element.form_method.lower()
        return "get"

    @property
    def action_route(self) -> str | None:
        for element in self.elements:
            if element.form_action_route:
                return element.form_action_route
        return None


def group_forms(elements: list[NormalizedElement]) -> list[FormGroup]:
    """Bucket elements by the form they belong to, preserving document order.

    An element with no ``form_key`` belongs to no form and is not a form
    candidate; it may still become a navigation capability elsewhere.
    """
    grouped: OrderedDict[str, list[NormalizedElement]] = OrderedDict()
    for element in elements:
        if element.form_key is None:
            continue
        grouped.setdefault(element.form_key, []).append(element)
    return [FormGroup(key, members) for key, members in grouped.items()]


class FormReading:
    """What the compiler made of one form, compiled or not."""

    def __init__(
        self,
        group: FormGroup,
        category: CapabilityCategory | None,
        confidence: Confidence,
        reason: str,
    ) -> None:
        self.group = group
        self.category = category
        self.confidence = confidence
        self.reason = reason


def read_form(group: FormGroup) -> FormReading:
    """Resolve what a form is for, and how strongly the page says so."""
    submits = group.submits
    submit_text = " ".join(s.resolved_label or "" for s in submits)
    identity = " ".join(filter(None, [group.key, *(e.control_name or "" for e in submits)]))
    route = group.action_route or ""

    category, source = _categorize(group, submit_text, identity, route)
    if category is None:
        return FormReading(group, None, Confidence.LOW, "no deterministic search or filter reading")

    return FormReading(group, category, _confidence(group, source), source)


def _categorize(
    group: FormGroup, submit_text: str, identity: str, route: str
) -> tuple[CapabilityCategory | None, str]:
    """Apply the signals of specification 8.3 in a fixed order.

    Filter is tested before search at each level. A form marked ``role="search"``
    whose button says "Filter" is a filter: the landmark describes the region, the
    button describes the act, and the act is what the capability performs.
    """
    for text, source in (
        (submit_text, "submit label"),
        (identity, "form identity"),
        (route, "action route"),
    ):
        if not text:
            continue
        if _FILTER_RE.search(text):
            return CapabilityCategory.FILTER, f"{source} names a filter"
        if _SEARCH_RE.search(text):
            return CapabilityCategory.SEARCH, f"{source} names a search"

    # Structural fallback. A free-text control inside a declared search region is a
    # search even when nothing is worded that way; constrained choices alone narrow
    # something that is already on the page, which is a filter.
    parameters = [e for e in group.elements if e.control_type in _PARAMETER_CONTROLS]
    if not parameters:
        return None, "no parameter controls"
    free_text = [e for e in parameters if e.control_type in {ControlType.TEXT, ControlType.SEARCH}]
    searchy = any(
        e.control_type is ControlType.SEARCH or e.in_search_landmark for e in group.elements
    )
    if free_text and searchy:
        return CapabilityCategory.SEARCH, "a free-text control in a declared search region"
    if not free_text and searchy:
        return CapabilityCategory.FILTER, "constrained choices in a declared search region"
    return None, "generic data entry"


def _confidence(group: FormGroup, source: str) -> Confidence:
    """Specification 7.4's table, applied to a form rather than to a control.

    An explicit semantic marker is ``type="search"``, a ``role="search"`` ancestor,
    or an associated ``<label for>`` on a control that becomes a parameter. Nothing
    here is worth a high rating on wording alone: a keyword match plus a form
    structure that agrees with it is the specification's definition of medium, and
    that is what a well-worded button gets.
    """
    explicit = any(
        e.control_type is ControlType.SEARCH
        or e.in_search_landmark
        or (e.control_type in _PARAMETER_CONTROLS and e.label_source is LabelSource.LABEL_FOR)
        for e in group.elements
    )
    if explicit:
        return Confidence.HIGH
    labelled = any(
        e.control_type in _PARAMETER_CONTROLS and e.resolved_label for e in group.elements
    )
    if labelled and source != "generic data entry":
        return Confidence.MEDIUM
    return Confidence.LOW


def parameter_name(element: NormalizedElement, taken: set[str]) -> str:
    """Name a parameter after what the form calls the control.

    The submitted ``name`` first, then the label. Both are the page's own words, so
    an agent reading the schema sees the same vocabulary a person filling the form
    would. A collision is resolved by suffixing rather than by dropping, because a
    dropped parameter is a control the capability silently leaves at its default.
    """
    raw = element.control_name or element.resolved_label or element.element_id
    slug = _NON_IDENTIFIER.sub("_", raw.strip().lower()).strip("_")
    if not slug or slug[0].isdigit():
        slug = f"field_{slug}" if slug else "field"
    candidate = slug
    index = 2
    while candidate in taken:
        candidate = f"{slug}_{index}"
        index += 1
    return candidate


class CompiledForm:
    """A form's generated schema, plan, locators, and risk."""

    def __init__(
        self,
        name: str,
        category: CapabilityCategory,
        confidence: Confidence,
        parameters: dict[str, object],
        plan: list[PlanStep],
        locators: dict[str, LocatorChain],
        targets: dict[str, NormalizedElement],
        risk: Risk,
        risk_reason: str,
        expected_effect: ExpectedEffect,
    ) -> None:
        self.name = name
        self.category = category
        self.confidence = confidence
        self.parameters = parameters
        self.plan = plan
        self.locators = locators
        self.targets = targets
        self.risk = risk
        self.risk_reason = risk_reason
        self.expected_effect = expected_effect


class FormRefusal(Exception):
    """A form that was understood well enough to know it must not be compiled."""

    def __init__(self, code: str, message: str) -> None:
        super().__init__(message)
        self.code = code
        self.message = message


def compile_form(reading: FormReading) -> CompiledForm:
    """Build the executable shape of one categorized form.

    Raises :class:`FormRefusal` rather than returning something partial. A form
    whose submit is ambiguous or whose required control cannot be expressed is not
    a capability with a caveat; it is a capability that would do something other
    than what its schema promises.
    """
    group = reading.group
    if reading.category is None:  # pragma: no cover - callers filter first
        raise FormRefusal("uncategorized_form", "the form resolved to no category")

    submits = group.submits
    if not submits:
        raise FormRefusal(
            "no_submission_mechanism",
            "the form has no identifiable submission control (specification 8.3)",
        )
    if len(submits) > 1:
        labels = sorted(s.resolved_label or s.element_id for s in submits)
        raise FormRefusal(
            "ambiguous_submission_mechanism",
            f"the form has more than one submit ({labels}); choosing one would be a guess",
        )
    submit = submits[0]

    properties: dict[str, object] = {}
    required: list[str] = []
    plan: list[PlanStep] = []
    locators: dict[str, LocatorChain] = {}
    targets: dict[str, NormalizedElement] = {}
    taken: set[str] = set()

    for element in group.elements:
        if element is submit or element.submits_form:
            continue
        if element.control_type not in _PARAMETER_CONTROLS:
            if element.required:
                raise FormRefusal(
                    "required_control_not_expressible",
                    f"a required {element.control_type} control cannot become a typed "
                    "parameter, so any plan would submit the form incomplete",
                )
            continue
        if element.value_omitted_for_safety or element.readonly:
            if element.required:
                raise FormRefusal(
                    "required_control_not_expressible",
                    "a required control holds a value specification 7.2 forbids WCC "
                    "to supply or read",
                )
            continue

        json_type, action = _PARAMETER_SHAPE[element.control_type]
        name = parameter_name(element, taken)
        taken.add(name)
        schema: dict[str, object] = {"type": json_type}
        if element.resolved_label:
            schema["description"] = element.resolved_label
        properties[name] = schema
        if element.required:
            required.append(name)

        plan.append(PlanStep(action=action, target_ref=name, parameter=name))
        locators[name] = element.locator_chain
        targets[name] = element

    if not properties:
        raise FormRefusal(
            "no_eligible_controls",
            "the form has no control a capability could supply (specification 8.3)",
        )

    submit_ref = "submit"
    while submit_ref in locators:
        submit_ref = f"{submit_ref}_"
    plan.append(PlanStep(action=ActionType.ACTIVATE, target_ref=submit_ref))
    locators[submit_ref] = submit.locator_chain
    targets[submit_ref] = submit

    risk, risk_reason = classify_plan(reading.category, plan, targets)

    return CompiledForm(
        name=_fallback_name(reading.category),
        category=reading.category,
        confidence=reading.confidence,
        parameters={
            "type": "object",
            "properties": properties,
            "required": sorted(required),
            "additionalProperties": False,
        },
        plan=plan,
        locators=locators,
        targets=targets,
        risk=risk,
        risk_reason=risk_reason,
        expected_effect=ExpectedEffect(
            type="page_state_change",
            signals=["url_change", "results_collection_change"],
        ),
    )


def refusal_plan(group: FormGroup) -> tuple[list[PlanStep], dict[str, NormalizedElement]] | None:
    """The minimal plan for a form that will not be compiled, so it can be classified.

    A refusal is more useful with a risk attached than without one: "not compiled"
    invites the agent to find another route, while "high, because it submits by
    POST" is an answer. The plan is the one step every form has — activate its
    submit — which is exactly the step whose consequence the risk describes.
    """
    submits = group.submits
    if len(submits) != 1:
        return None
    plan = [PlanStep(action=ActionType.ACTIVATE, target_ref="submit")]
    return plan, {"submit": submits[0]}


def _fallback_name(category: CapabilityCategory) -> str:
    """Specification 9.2's fallback names.

    The preferred names carry a domain noun — ``search_products`` rather than
    ``submit_search_form`` — and none is used here. Deriving that noun from a
    heading or a route would be inventing a capability name from evidence that does
    not establish it, which 9.2 forbids in the same paragraph that recommends the
    name. Phase 6's enricher is where a domain noun may legitimately come from.
    """
    return {
        CapabilityCategory.SEARCH: "submit_search_form",
        CapabilityCategory.FILTER: "apply_filter_form",
        CapabilityCategory.NAVIGATE: "navigate",
    }[category]
