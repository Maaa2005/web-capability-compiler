"""Turn raw page signals into normalized elements (specification 7.2, 8.1, 8.2).

The browser reports signals; every judgement happens here. That split is what lets
label priority, eligibility, and confidence be tested without starting Chromium,
and it keeps the page — which is untrusted data under specification 10.3 — out of
the decision.
"""

from __future__ import annotations

import re
from typing import Any

from wcc.compiler.route import normalize_route_pattern, origin_of
from wcc.models.common import (
    NEVER_CAPTURED,
    BoundingBox,
    Confidence,
    ControlType,
    FormActionKind,
    LabelSource,
)
from wcc.models.element import NormalizedElement
from wcc.models.locator import LocatorCandidate, LocatorChain, LocatorKind

#: Specification 8.1, highest priority first. The raw-record key is paired with
#: the source that key represents, so the order is the only thing that encodes
#: priority and a reordering is a one-line, reviewable change.
_LABEL_PRIORITY: tuple[tuple[str, LabelSource], ...] = (
    ("label_for", LabelSource.LABEL_FOR),
    ("wrapping_label", LabelSource.WRAPPING_LABEL),
    ("aria_labelledby", LabelSource.ARIA_LABELLEDBY),
    ("aria_label", LabelSource.ARIA_LABEL),
    ("element_text", LabelSource.BUTTON_TEXT),
    ("name", LabelSource.NAME_ATTRIBUTE),
    ("placeholder", LabelSource.PLACEHOLDER),
    ("legend_or_heading", LabelSource.LEGEND_OR_HEADING),
)

_INPUT_TYPES: dict[str, ControlType] = {
    "text": ControlType.TEXT,
    "search": ControlType.SEARCH,
    "number": ControlType.NUMBER,
    "email": ControlType.TEXT,
    "tel": ControlType.TEXT,
    "url": ControlType.TEXT,
    "checkbox": ControlType.CHECKBOX,
    "radio": ControlType.RADIO,
    "password": ControlType.PASSWORD,
    "file": ControlType.FILE,
    "hidden": ControlType.HIDDEN,
    "submit": ControlType.BUTTON,
    "button": ControlType.BUTTON,
    "reset": ControlType.BUTTON,
    # A submit button drawn as a picture is still a button. Falling through to the
    # text default made it look like a control a capability could type into.
    "image": ControlType.BUTTON,
}

#: Roles that make an element interactive even without a native tag (8.2).
_INTERACTIVE_ROLES = frozenset(
    {"button", "link", "checkbox", "radio", "combobox", "searchbox", "textbox"}
)

#: ``rel`` values that mark a control as paging rather than as a result.
_PAGINATION_RELS = frozenset({"next", "prev", "previous", "first", "last"})

_BARE_NUMBER = re.compile(r"^\d+$")


def resolve_label(record: dict[str, Any]) -> tuple[str | None, LabelSource | None]:
    """Return the label and the source it came from, in specification 8.1 order."""
    for key, source in _LABEL_PRIORITY:
        value = record.get(key)
        if isinstance(value, str) and value.strip():
            return value.strip(), source
    return None, None


def classify_control(record: dict[str, Any]) -> ControlType:
    """Name the control, keeping password, file, and hidden distinguishable."""
    tag = record.get("tag")
    if tag == "select":
        return ControlType.SELECT
    if tag == "textarea":
        return ControlType.TEXTAREA
    if tag == "button":
        return ControlType.BUTTON
    if tag == "a":
        return ControlType.LINK
    if tag == "input":
        return _INPUT_TYPES.get(str(record.get("type") or "text"), ControlType.TEXT)
    role = record.get("role")
    if role == "searchbox":
        return ControlType.SEARCH
    if role in {"button", "link", "checkbox", "radio"}:
        return ControlType(role)
    if role in {"combobox", "textbox"}:
        return ControlType.TEXT
    return ControlType.OTHER


def is_eligible(record: dict[str, Any], control_type: ControlType) -> bool:
    """Apply specification 8.2.

    A hidden input is excluded rather than kept as a "safe semantic value": the
    specification permits keeping one, but nothing in Phase 1 reads it, and an
    element retained without a reader is a value waiting to be recorded by
    accident.
    """
    if record.get("disabled"):
        return False
    if control_type is ControlType.HIDDEN:
        return False
    if not record.get("visible"):
        return False
    native = record.get("tag") in {"input", "select", "textarea", "button", "a"}
    return native or record.get("role") in _INTERACTIVE_ROLES or bool(record.get("form_present"))


def assess_confidence(record: dict[str, Any], source: LabelSource | None) -> Confidence:
    """Specification 7.4, read as a statement about evidence rather than about luck.

    An explicit semantic marker or a `<label for>` is high. A label that exists but
    was inferred from weaker markup is medium. Placeholder-only or no label at all
    is positional inference, which is low and therefore never executable.
    """
    if record.get("search_signal") or source is LabelSource.LABEL_FOR:
        return Confidence.HIGH
    if source in {
        LabelSource.WRAPPING_LABEL,
        LabelSource.ARIA_LABELLEDBY,
        LabelSource.ARIA_LABEL,
        LabelSource.BUTTON_TEXT,
        LabelSource.NAME_ATTRIBUTE,
    }:
        return Confidence.MEDIUM
    return Confidence.LOW


def form_destination(
    record: dict[str, Any], page_url: str | None
) -> tuple[str | None, str | None, FormActionKind | None]:
    """Return the method, normalized action route, and origin relationship of the form.

    The route is normalized here rather than stored raw because a form action may
    carry query values, and specification 7.2's rule about not holding page content
    does not stop at input values. Normalizing at the boundary means no later layer
    has to remember.

    An action that cannot be parsed yields :data:`FormActionKind.UNKNOWN` rather
    than a guess. Two forms whose destinations are both unknown are then treated as
    indistinguishable, which is the conservative direction: it can merge page
    classes that differ, never split one that does not.
    """
    if not record.get("form_present"):
        return None, None, None

    method = str(record.get("form_method") or "get").lower()
    action = record.get("form_action")
    if not isinstance(action, str) or not action:
        return method, None, FormActionKind.UNKNOWN

    try:
        route = normalize_route_pattern(action)
        action_origin = origin_of(action)
    except ValueError:
        return method, None, FormActionKind.UNKNOWN

    if page_url is None:
        return method, route, FormActionKind.UNKNOWN
    try:
        same_origin = origin_of(page_url) == action_origin
    except ValueError:
        return method, route, FormActionKind.UNKNOWN
    return (
        method,
        route,
        (FormActionKind.SAME_ORIGIN if same_origin else FormActionKind.CROSS_ORIGIN),
    )


def pagination_rel(record: dict[str, Any]) -> str | None:
    """Return the paging ``rel`` this control declares, if it declares one.

    Only the first recognised value is reported. A control claiming both ``next``
    and ``prev`` is contradicting itself, and picking one by sort order at least
    makes the choice the same on every run.
    """
    rels = [part.lower() for part in str(record.get("rel") or "").split() if part]
    recognised = sorted(set(rels) & _PAGINATION_RELS)
    return recognised[0] if recognised else None


def is_pagination_control(
    record: dict[str, Any], control_type: ControlType, label: str | None
) -> bool:
    """Decide whether a button or link pages through a result set.

    Two signals, both structural. An explicit ``rel`` is definitive wherever it
    appears. Failing that, a bare number inside a navigation landmark is a page
    number: the landmark supplies the intent, so the digits do not have to be
    read as content. A numeric label outside a landmark stays an ordinary control,
    because "3" on a product card is a quantity, not a page.
    """
    if control_type not in {ControlType.LINK, ControlType.BUTTON}:
        return False
    rels = {part.lower() for part in str(record.get("rel") or "").split() if part}
    if rels & _PAGINATION_RELS:
        return True
    if not record.get("in_navigation"):
        return False
    return bool(label) and bool(_BARE_NUMBER.match(label.strip()))


def build_locator_chain(record: dict[str, Any], label: str | None) -> LocatorChain:
    """Collect every applicable candidate in specification 7.6 priority order.

    Re-resolution against a live page is task T5; this is the evidence it will
    walk. A structural path is always appended so a chain is never empty, but it
    is last, because it is the candidate most easily invalidated by a redesign.
    """
    candidates: list[LocatorCandidate] = []
    role = record.get("role") or None
    form_id = record.get("form_id") or None

    if record.get("id"):
        candidates.append(
            LocatorCandidate(kind=LocatorKind.ID_ATTRIBUTE, key=str(record["id"]), role=role)
        )
    if record.get("name"):
        candidates.append(
            LocatorCandidate(
                kind=LocatorKind.NAME_WITH_FORM,
                key=str(record["name"]),
                role=role,
                form_id=form_id,
            )
        )
    if label:
        candidates.append(
            LocatorCandidate(
                kind=LocatorKind.ACCESSIBLE_NAME_WITH_ROLE, key=label, role=role, form_id=form_id
            )
        )
        candidates.append(
            LocatorCandidate(
                kind=LocatorKind.ASSOCIATED_LABEL_TEXT, key=label, role=role, form_id=form_id
            )
        )
    candidates.append(
        LocatorCandidate(
            kind=LocatorKind.STRUCTURAL_PATH,
            key=str(record.get("structural_path") or record.get("tag") or "unknown"),
            role=role,
            form_id=form_id,
        )
    )
    return LocatorChain(primary=candidates[0], candidates=candidates)


def normalize_element(
    record: dict[str, Any], element_id: str, page_url: str | None = None
) -> NormalizedElement:
    """Map one raw record onto the public model.

    ``page_url`` decides only whether a form action is same-origin or cross-origin.
    It is optional so a record can still be normalized without a live page; the
    origin relationship then reads as unknown rather than as same-origin.
    """
    control_type = classify_control(record)
    label, source = resolve_label(record)
    method, action_route, action_kind = form_destination(record, page_url)
    sensitive = bool(record.get("sensitive")) or control_type in NEVER_CAPTURED
    raw_value = record.get("value")
    value = None if sensitive or not isinstance(raw_value, str) else raw_value
    box = record.get("box")

    return NormalizedElement(
        element_id=element_id,
        tag_name=str(record.get("tag") or "unknown"),
        role=record.get("role"),
        resolved_label=label,
        accessible_name=label,
        label_source=source,
        control_type=control_type,
        value=value,
        value_omitted_for_safety=sensitive,
        required=bool(record.get("required")),
        disabled=bool(record.get("disabled")),
        readonly=bool(record.get("readonly")),
        checked=record.get("checked"),
        visible=bool(record.get("visible")),
        bounding_box=BoundingBox(**box) if isinstance(box, dict) else None,
        form_id=record.get("form_id"),
        form_key=record.get("form_key"),
        control_name=record.get("name") or None,
        form_method=method,
        form_action_route=action_route,
        form_action_kind=action_kind,
        submits_form=bool(record.get("submits_form")),
        in_search_landmark=bool(record.get("in_search_landmark")),
        in_navigation=bool(record.get("in_navigation")),
        is_pagination_control=is_pagination_control(record, control_type, label),
        pagination_rel=pagination_rel(record),
        locator_chain=build_locator_chain(record, label),
        confidence=assess_confidence(record, source),
    )
