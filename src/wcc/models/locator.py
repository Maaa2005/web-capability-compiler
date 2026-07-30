"""Locator candidates and chains (specification 7.6, 7.7).

These are internal models. They are never returned through the MCP interface and
never accept an agent-supplied selector.
"""

from __future__ import annotations

from enum import IntEnum

from pydantic import Field

from wcc.models.common import WCCModel


class LocatorKind(IntEnum):
    """Candidate kinds in priority order (specification 7.6).

    Lower value means higher priority, so a chain sorts by ``kind`` directly.
    """

    ID_ATTRIBUTE = 1
    NAME_WITH_FORM = 2
    ACCESSIBLE_NAME_WITH_ROLE = 3
    ASSOCIATED_LABEL_TEXT = 4
    STRUCTURAL_PATH = 5


class LocatorCandidate(WCCModel):
    """One way to find an element again.

    ``key`` holds the matching value for the candidate kind: the ``id`` value, the
    control ``name``, the accessible name, the label text, or a structural path
    rooted at the nearest landmark or form ancestor. A structural path may carry
    tag-index information, but execution must never fall back to a global
    nth-child-only path, a bounding box, a screenshot coordinate, or a selector
    supplied by an external agent.
    """

    kind: LocatorKind
    key: str
    role: str | None = None
    form_id: str | None = None
    landmark_id: str | None = None


class LocatorChain(WCCModel):
    """All applicable candidates for one element, plus the primary candidate.

    Re-resolution (specification 7.7) walks ``candidates`` in priority order and
    accepts only an exactly-one match. Zero matches and ambiguous matches both
    continue to the next candidate; the closest element is never chosen.
    """

    primary: LocatorCandidate
    candidates: list[LocatorCandidate] = Field(min_length=1)

    def ordered(self) -> list[LocatorCandidate]:
        """Candidates in resolution order, primary first."""
        rest = [c for c in self.candidates if c != self.primary]
        return [self.primary, *sorted(rest, key=lambda c: c.kind)]
