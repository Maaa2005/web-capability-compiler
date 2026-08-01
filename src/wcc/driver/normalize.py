"""Turn raw page signals into normalized elements (specification 7.2, 8.1, 8.2).

The browser reports signals; every judgement happens here. That split is what lets
label priority, eligibility, and confidence be tested without starting Chromium,
and it keeps the page — which is untrusted data under specification 10.3 — out of
the decision.
"""

from __future__ import annotations

from typing import Any

from wcc.models.common import (
    NEVER_CAPTURED,
    BoundingBox,
    Confidence,
    ControlType,
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
}

#: Roles that make an element interactive even without a native tag (8.2).
_INTERACTIVE_ROLES = frozenset(
    {"button", "link", "checkbox", "radio", "combobox", "searchbox", "textbox"}
)


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


def normalize_element(record: dict[str, Any], element_id: str) -> NormalizedElement:
    """Map one raw record onto the public model."""
    control_type = classify_control(record)
    label, source = resolve_label(record)
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
        locator_chain=build_locator_chain(record, label),
        confidence=assess_confidence(record, source),
    )
