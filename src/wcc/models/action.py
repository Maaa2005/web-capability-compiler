"""Planned actions and driver observations (specification 4.1, 10.4, 11.1)."""

from __future__ import annotations

from datetime import datetime

from pydantic import Field, model_validator

from wcc.models.common import ActionType, WCCModel


class PlannedAction(WCCModel):
    """One allowlisted low-level action addressed to a normalized element.

    The action names an element by its internal ``element_id``; the driver resolves
    it through the locator chain. Raw selectors are never accepted from outside the
    driver (specification 4.1).
    """

    action: ActionType
    element_id: str
    value: str | None = None
    option: str | None = None
    checked: bool | None = None

    @model_validator(mode="after")
    def _require_action_payload(self) -> PlannedAction:
        """Each action type carries exactly the payload it needs.

        ``set_checked`` demands an explicit desired state because specification
        10.4 forbids a state-relative toggle.
        """
        required: dict[ActionType, str] = {
            ActionType.SET_VALUE: "value",
            ActionType.SELECT_OPTION: "option",
            ActionType.SET_CHECKED: "checked",
        }
        expected = required.get(self.action)
        for field in ("value", "option", "checked"):
            present = getattr(self, field) is not None
            if field == expected and not present:
                raise ValueError(f"{self.action} requires {field}")
            if field != expected and present:
                raise ValueError(f"{self.action} does not accept {field}")
        return self

    def redacted(self) -> PlannedAction:
        """This action with its value replaced by a description of the value.

        Specification 18 requires redaction before persistence. The marker keeps
        the two facts an audit needs — that a value was supplied, and how much of
        one — while the value itself stops at the process boundary.
        """
        return self.model_copy(
            update={
                "value": _described(self.value),
                "option": _described(self.option),
            }
        )


def _described(value: str | None) -> str | None:
    return None if value is None else f"[redacted: {len(value)} characters]"


class NavigationEvent(WCCModel):
    """A navigation-adjacent effect the driver must surface to the policy layer.

    Attempted redirects, downloads, popups, and new pages are reported rather than
    followed, so the runtime can revalidate them (specification 4.1, 10.2).
    """

    kind: str
    url: str | None = None
    blocked: bool = False
    reason: str | None = None


class ActionObservation(WCCModel):
    """What the driver saw after performing one planned action."""

    action: PlannedAction
    succeeded: bool
    observed_at: datetime
    document_id: str
    snapshot_id: str | None = None
    document_changed: bool = False
    settled: bool = False
    navigation_events: list[NavigationEvent] = Field(default_factory=list)
    error_code: str | None = None
    error_message: str | None = None

    def redacted(self) -> ActionObservation:
        """This observation with the action's typed value removed."""
        return self.model_copy(update={"action": self.action.redacted()})
