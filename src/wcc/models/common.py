"""Shared enums and base types for WCC public models.

Field semantics follow specification sections 7.1-7.7. Public models never expose
CSS selectors; locator detail stays inside :mod:`wcc.models.locator`.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, ConfigDict

SCHEMA_VERSION = "0.2.1"


class WCCModel(BaseModel):
    """Base for every public model.

    ``extra="forbid"`` mirrors the ``additionalProperties: false`` requirement the
    specification places on generated JSON Schemas (section 11.1, step 4).
    """

    model_config = ConfigDict(extra="forbid", frozen=True)


class Confidence(StrEnum):
    """Specification 7.4. Only ``medium`` and ``high`` may become templates."""

    HIGH = "high"
    MEDIUM = "medium"
    LOW = "low"


class SessionStatus(StrEnum):
    ACTIVE = "active"
    CLOSED = "closed"
    FAILED = "failed"


class LabelSource(StrEnum):
    """Specification 8.1 accessible-name priority, highest first.

    The order is the specification's, not the DOM's convenience: a wrapping
    ``<label>`` outranks ``aria-labelledby``. Phase 0 had these two transposed and
    ended with ``title``, which specification 8.1 does not list at all; the eighth
    source is a nearby heading or fieldset legend.
    """

    LABEL_FOR = "label_for"
    WRAPPING_LABEL = "wrapping_label"
    ARIA_LABELLEDBY = "aria_labelledby"
    ARIA_LABEL = "aria_label"
    BUTTON_TEXT = "button_text"
    NAME_ATTRIBUTE = "name_attribute"
    PLACEHOLDER = "placeholder"
    LEGEND_OR_HEADING = "legend_or_heading"


class ControlType(StrEnum):
    """Additive since schema 0.2.1: ``PASSWORD``, ``FILE``, and ``HIDDEN``.

    Specification 7.2 forbids capturing the values of these controls, and a type
    that cannot be named cannot be checked. A reader written against the earlier
    member list must treat an unknown control type as ``other``.
    """

    TEXT = "text"
    SEARCH = "search"
    NUMBER = "number"
    CHECKBOX = "checkbox"
    RADIO = "radio"
    SELECT = "select"
    TEXTAREA = "textarea"
    BUTTON = "button"
    LINK = "link"
    PASSWORD = "password"
    FILE = "file"
    HIDDEN = "hidden"
    OTHER = "other"


#: Controls whose value is never recorded (specification 7.2).
NEVER_CAPTURED = frozenset({ControlType.PASSWORD, ControlType.FILE, ControlType.HIDDEN})


class FormActionKind(StrEnum):
    """Where a form submits, expressed without the content of where.

    ``page_signature`` needs to tell a search form from a delete form, and the two
    differ in where they submit. It must not learn the search term, so only the
    origin relationship is recorded here; the path arrives separately as a
    normalized route pattern, which drops query values by construction.
    """

    SAME_ORIGIN = "same-origin"
    CROSS_ORIGIN = "cross-origin"
    UNKNOWN = "unknown"


class CollectionKind(StrEnum):
    """Specification 8.4 repeated-structure categories."""

    LIST = "list"
    TABLE = "table"
    CARDS = "cards"
    SEARCH_RESULTS = "search_results"


class ActionType(StrEnum):
    """Specification 10.4. State-relative ``toggle`` is deliberately absent."""

    FOCUS = "focus"
    SET_VALUE = "set_value"
    SELECT_OPTION = "select_option"
    SET_CHECKED = "set_checked"
    ACTIVATE = "activate"
    SCROLL_INTO_VIEW = "scroll_into_view"


class BoundingBox(WCCModel):
    x: float
    y: float
    width: float
    height: float
