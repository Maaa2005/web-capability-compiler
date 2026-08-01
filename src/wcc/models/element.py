"""Normalized elements and extracted collections (specification 7.2, 8.2, 8.4)."""

from __future__ import annotations

from pydantic import Field, model_validator

from wcc.models.common import (
    NEVER_CAPTURED,
    BoundingBox,
    CollectionKind,
    Confidence,
    ControlType,
    LabelSource,
    WCCModel,
)
from wcc.models.locator import LocatorChain


class NormalizedElement(WCCModel):
    """One interactive element eligible under specification 8.2.

    ``value`` carries a current control value only when it is safe to record.
    Password values, payment-card values, security codes, hidden authentication
    tokens, and file-input paths are never captured (specification 7.2), so the
    extractor leaves ``value`` as ``None`` for those controls rather than storing
    a redacted placeholder.
    """

    element_id: str
    tag_name: str
    role: str | None = None
    resolved_label: str | None = None
    accessible_name: str | None = None
    label_source: LabelSource | None = None
    control_type: ControlType = ControlType.OTHER
    value: str | None = None
    value_omitted_for_safety: bool = False
    required: bool = False
    disabled: bool = False
    readonly: bool = False
    checked: bool | None = None
    visible: bool = True
    bounding_box: BoundingBox | None = None
    form_id: str | None = None
    locator_chain: LocatorChain
    confidence: Confidence = Confidence.LOW

    @model_validator(mode="after")
    def _forbid_sensitive_values(self) -> NormalizedElement:
        """Refuse to hold a value specification 7.2 says must never be captured.

        The extractor already declines to read these values in the page, so this
        is the second of two independent barriers. One barrier is a convention a
        future edit can quietly break; two make the omission a property of the
        model, and the failure is loud.
        """
        if self.control_type in NEVER_CAPTURED and self.value is not None:
            raise ValueError(
                f"a {self.control_type} value must never be captured "
                "(specification 7.2); leave value unset and set "
                "value_omitted_for_safety"
            )
        return self


class CollectionRecord(WCCModel):
    """One extracted record inside a collection.

    ``identity`` is the deterministic per-record projection that feeds
    ``collection_hash`` (specification 8.4). ``evidence_element_ids`` preserves the
    mapping back to the DOM elements that produced the record.
    """

    record_id: str
    identity: str
    fields: dict[str, str] = Field(default_factory=dict)
    evidence_element_ids: list[str] = Field(default_factory=list)
    entity_id: str | None = None


class CollectionSnapshot(WCCModel):
    """A repeated structure detected on the page (specification 8.4).

    ``order_is_meaningful`` tells the verifier whether ``collection_hash`` must be
    sensitive to record order.
    """

    collection_id: str
    kind: CollectionKind
    label: str | None = None
    records: list[CollectionRecord] = Field(default_factory=list)
    record_count: int = 0
    order_is_meaningful: bool = True
    collection_hash: str
    truncated: bool = False
    confidence: Confidence = Confidence.LOW
