"""Session, document, and page-snapshot models (specification 7.1, 7.2)."""

from __future__ import annotations

from datetime import datetime

from pydantic import Field

from wcc.models.common import SCHEMA_VERSION, SessionStatus, WCCModel
from wcc.models.element import CollectionSnapshot, NormalizedElement


class OpenSessionRequest(WCCModel):
    """Input for opening a session.

    Deliberately narrow. Specification 12.1 gives the MCP caller a URL and nothing
    else, and the private-network allowance of specification 10.2 is a
    *developer-only* flag: an external agent that could set it per request would be
    able to switch off the SSRF boundary at will. That allowance lives in
    ``DriverSettings``, which comes from trusted local configuration.
    """

    url: str
    headless: bool = True
    locale: str | None = None


class SessionInfo(WCCModel):
    """One session equals one browser context with one active page.

    ``current_document_id`` changes only after a committed navigation to a new
    document; ``current_snapshot_id`` changes on every observation, including
    same-document updates (specification 7.2).
    """

    session_id: str
    created_at: datetime
    initial_url: str
    current_document_id: str | None = None
    current_snapshot_id: str | None = None
    status: SessionStatus = SessionStatus.ACTIVE


class StructuredDataBlock(WCCModel):
    """A JSON-LD or Schema.org block found on the page (specification 3.2)."""

    source: str
    data: dict[str, object]


class PageSnapshot(WCCModel):
    """One observation of the current document.

    The three fingerprints are distinct and not interchangeable (specification
    7.3): ``structural_hash`` detects inspection changes, ``page_signature``
    identifies a reusable page class for template lookup, and each collection
    carries its own ``collection_hash`` for postcondition verification.
    """

    schema_version: str = SCHEMA_VERSION
    session_id: str
    document_id: str
    snapshot_id: str
    captured_at: datetime
    url: str
    origin: str
    normalized_route_pattern: str
    title: str | None = None
    language: str | None = None
    elements: list[NormalizedElement] = Field(default_factory=list)
    collections: list[CollectionSnapshot] = Field(default_factory=list)
    structured_data: list[StructuredDataBlock] = Field(default_factory=list)
    structural_hash: str
    page_signature: str
    warnings: list[str] = Field(default_factory=list)
