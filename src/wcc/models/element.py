"""Normalized elements and extracted collections (specification 7.2, 8.2, 8.4)."""

from __future__ import annotations

from pydantic import Field, model_validator

from wcc.models.common import (
    NEVER_CAPTURED,
    BoundingBox,
    CollectionKind,
    Confidence,
    ControlType,
    FormActionKind,
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

    The ``form_*`` fields describe where a submit sends the page, not what it
    sends. They exist because unique re-resolution is not semantic equivalence: a
    submit replaced by a different-purpose button keeping the same id, accessible
    name, and in-form structure still re-resolves to exactly one element, and only
    the destination separates the two. ``form_action_route`` is already normalized
    when it arrives, so no query value is ever held here.
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
    form_key: str | None = None
    """Which form this control belongs to, named or not.

    ``form_id`` is the ``id`` attribute and is often absent. Grouping controls for
    compilation by ``form_id`` alone would put every unnamed form on a page into
    one bucket, and the resulting plan would fill a field in one form and submit
    another. ``form_key`` falls back to the form's document position, which is
    unique within a snapshot and stable for as long as the page class is.
    """

    control_name: str | None = None
    """The submitted ``name`` attribute, kept for parameter naming.

    A generated parameter should be called what the form calls it. The name is
    recoverable from the locator chain, but reading it back out of a locator is the
    kind of indirection that survives until someone reorders the candidates.
    """

    form_method: str | None = None
    form_action_route: str | None = None
    form_action_kind: FormActionKind | None = None
    submits_form: bool = False
    in_search_landmark: bool = False
    """Whether an ancestor declares ``role="search"`` (specification 8.3)."""

    in_navigation: bool = False
    """Whether an ancestor is a navigation landmark.

    Repeated-structure detection needs this. A header nav holding "Home" and
    "Products" is two sibling links with matching structural paths, which is
    indistinguishable from a two-item result list by shape alone.
    """

    is_pagination_control: bool = False
    pagination_rel: str | None = None
    """The ``rel`` value that made this a pagination control, when there was one.

    ``next`` and ``prev`` are declarations, not readings of a label, and telling
    forward from backward off the word "Next" would fail on the first page that
    said "More".
    """

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
    activation_locator: LocatorChain | None = None
    """Where activating this record would click, when the record has a link.

    Specification 7.11 addresses a result by entity reference, and an entity that
    cannot say where it is can only be read. The chain is stored rather than an
    element ID for the usual reason: the ID belongs to one observation, while the
    chain is what re-resolves against the page in front of us (specification 7.7).

    ``None`` is a real answer. A record whose only content is text is reportable
    and not openable, and inventing a target for it — the container, the nearest
    link — would activate something the record does not name.
    """
    link_only: bool = False
    """Whether every piece of the record's text belongs to one of its links.

    A tag, a category or a menu entry is only a link; a quote, a product or a story
    says something around it. Both repeat on a page, and only the second is what
    ``open_result_item`` means by a result (specification 7.11).
    """


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
