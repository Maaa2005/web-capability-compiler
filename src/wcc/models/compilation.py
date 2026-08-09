"""What one pass of the deterministic compiler produced (specification 8.3, 9.1).

The report has three lists, and the reason there are three rather than one is the
awkward pair of requirements the specification places side by side. Section 8.3
says a generic data-entry form is "detected and reported in warnings but not
compiled"; Phase 3's acceptance says the POST contact form must be "high, listed,
and not executable". Both are satisfied only if a refusal is a first-class result
that carries its classification, rather than a template that happens to be
unrunnable or a sentence in a log.

So: ``templates`` is what an agent may be offered, ``rejected`` is what was seen
and declined and why, and ``warnings`` is everything that went wrong while
reading. A page that yields no templates still reports what it found, which is
the difference between "this page offers nothing" and "the compiler saw nothing".
"""

from __future__ import annotations

from pydantic import Field

from wcc.models.capability import CapabilityTemplate, Risk, RiskLevel
from wcc.models.common import SCHEMA_VERSION, Confidence, WCCModel
from wcc.models.ir import Evidence, IRWarning


class RejectedCandidate(WCCModel):
    """Something compilable-looking that was deliberately not compiled.

    ``risk`` is present whenever it could be classified, and classifying the risk
    of something being refused is not wasted work: an agent told only that a form
    was skipped will try to reach it another way, while one told the form is a
    high-risk POST has been given the reason.
    """

    candidate_key: str
    """Which candidate: a form key, or a synthetic key for a non-form group."""

    code: str
    """Stable and machine-readable. ``message`` is the prose version."""

    message: str
    risk: Risk | None = None
    confidence: Confidence | None = None
    evidence: list[Evidence] = Field(default_factory=list)


class CompilationReport(WCCModel):
    """Everything one snapshot compiled to.

    Carries ``snapshot_id`` because every claim in it is about one observation.
    Two reports of the same page class are comparable only through their
    templates' page signatures, never by equality.
    """

    schema_version: str = SCHEMA_VERSION
    snapshot_id: str
    templates: list[CapabilityTemplate] = Field(default_factory=list)
    rejected: list[RejectedCandidate] = Field(default_factory=list)
    warnings: list[IRWarning] = Field(default_factory=list)

    def executable_templates(self) -> list[CapabilityTemplate]:
        """The templates whose risk permits execution at all (specification 10.1).

        "Permits" is doing real work in that sentence. This is a property of the
        template, not permission to run: binding re-resolves every target and
        re-classifies risk against the live page before anything executes.
        """
        return [t for t in self.templates if t.risk.level is RiskLevel.LOW]
