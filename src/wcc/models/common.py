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
    """Specification 8.1 accessible-name priority, highest first."""

    LABEL_FOR = "label_for"
    ARIA_LABELLEDBY = "aria_labelledby"
    ARIA_LABEL = "aria_label"
    WRAPPING_LABEL = "wrapping_label"
    BUTTON_TEXT = "button_text"
    NAME_ATTRIBUTE = "name_attribute"
    PLACEHOLDER = "placeholder"
    TITLE = "title"


class ControlType(StrEnum):
    TEXT = "text"
    SEARCH = "search"
    NUMBER = "number"
    CHECKBOX = "checkbox"
    RADIO = "radio"
    SELECT = "select"
    TEXTAREA = "textarea"
    BUTTON = "button"
    LINK = "link"
    OTHER = "other"


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
