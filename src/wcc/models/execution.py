"""What one execution of a bound capability did (specification 7.12, 11.1).

The result is returned synchronously and stored, and those two uses pull in the
same direction: everything a caller needs in order to decide what happened must be
*in* the result, because there is no log to consult and no job to poll.

So a failure is described rather than raised. An exception crossing the MCP
boundary becomes a string, and a string cannot say which of six steps ran, whether
the page changed, or whether the refusal came before any action touched the page.
:class:`ExecutionResult` says all three, on every path, including the paths where
nothing ran at all.
"""

from __future__ import annotations

from datetime import datetime
from enum import StrEnum

from pydantic import Field, model_validator

from wcc.models.action import ActionObservation
from wcc.models.common import SCHEMA_VERSION, ActionType, WCCModel
from wcc.models.ir import Entity


class ExecutionStatus(StrEnum):
    """How an execution ended.

    ``REFUSED`` and ``STALE`` are separate because they ask different things of an
    agent. Stale means the page moved and the defined next move is to inspect and
    re-bind (specification 11.2). Refused means the request itself was wrong — bad
    arguments, or a risk that does not permit execution — and re-binding would
    produce the same answer.
    """

    SUCCEEDED = "succeeded"
    FAILED = "failed"
    STALE = "stale"
    REFUSED = "refused"


class ExecutionError(WCCModel):
    """Why an execution did not succeed, in a form an agent can branch on.

    ``code`` is the stable half. The driver's error taxonomy already carries codes
    for the same reason (specification 11.2), and a runtime that answered only in
    prose would make the boundary between them the one place an agent has to parse
    English.
    """

    code: str
    message: str


class ExecutedStep(WCCModel):
    """One step of the plan, whether or not it ran.

    A step that never ran is recorded with ``observation`` unset rather than
    omitted from the list. The plan is what the capability promised to do, and a
    result that quietly listed only the steps that happened would let a partial
    execution read like a shorter plan that completed.
    """

    index: int
    action: ActionType
    target_ref: str
    parameter: str | None = None
    observation: ActionObservation | None = None
    error: ExecutionError | None = None

    @property
    def ran(self) -> bool:
        return self.observation is not None

    def redacted(self) -> ExecutedStep:
        """This step with the value it typed replaced by a description of it."""
        if self.observation is None:
            return self
        return self.model_copy(
            update={"observation": self.observation.redacted()},
        )


class Verification(WCCModel):
    """Whether the expected effect was observed (specification 11.3).

    ``signals_expected`` counts the signals the template named, and
    ``signals_matched`` how many were seen. They are reported separately from
    ``passed`` because the ratio is the evidence and ``passed`` is the reading of
    it: a capability whose effect showed through one of its two signals succeeded,
    and an operator looking at a run that keeps matching one of two deserves to see
    that without re-deriving it.
    """

    passed: bool
    signals_expected: int
    signals_matched: int
    observed: list[str] = Field(default_factory=list)
    missing: list[str] = Field(default_factory=list)


class ExecutionResult(WCCModel):
    """The complete, synchronous answer to one ``execute_capability`` call."""

    schema_version: str = SCHEMA_VERSION
    execution_id: str
    session_id: str
    capability_id: str
    template_id: str
    status: ExecutionStatus
    started_at: datetime
    completed_at: datetime
    steps: list[ExecutedStep] = Field(default_factory=list)
    observed_effects: list[str] = Field(default_factory=list)
    verification: Verification | None = None
    document_id_after: str | None = None
    snapshot_id_after: str | None = None
    result_entities: list[Entity] = Field(default_factory=list)
    warnings: list[str] = Field(default_factory=list)
    """What the runtime noticed but did not refuse.

    A capability rated low describes what *WCC* does, not what the page does in
    response. A page whose input handler issues a POST has changed server state
    during an execution that was cleared as read-only, and the runtime cannot
    prevent that from outside the page. Reporting it is what makes "no unauthorized
    execution above low" a claim someone can check rather than assume.
    """

    error: ExecutionError | None = None

    def redacted(self) -> ExecutionResult:
        """The same result with the values that were typed into the page removed.

        Specification 16 forbids logging full sensitive form values and 18 requires
        redaction before persistence, and a search term is exactly the field that
        looks harmless until it is an email address, an order number, or a name.
        The values are in the result because the caller supplied them and needs to
        see what ran; they are not in the database, because nobody needs them
        there tomorrow.

        What survives is what an audit actually asks: which parameter, what type of
        value, and how long it was. "The keyword parameter received 24 characters"
        answers "did this execution carry personal data" without carrying it.
        """
        return self.model_copy(update={"steps": [step.redacted() for step in self.steps]})

    @model_validator(mode="after")
    def _an_unsuccessful_result_says_why(self) -> ExecutionResult:
        """Refuse a failure that carries no reason, and a success that carries one.

        Both directions matter. A failure without an error leaves the caller with
        nothing to act on; a success carrying an error is a result that disagrees
        with itself, and a caller reading either field alone would be told
        something different from a caller reading the other.
        """
        if self.status is ExecutionStatus.SUCCEEDED and self.error is not None:
            raise ValueError("a succeeded execution must not carry an error")
        if self.status is not ExecutionStatus.SUCCEEDED and self.error is None:
            raise ValueError(f"a {self.status} execution must say why in error")
        return self

    @model_validator(mode="after")
    def _nothing_succeeds_unverified(self) -> ExecutionResult:
        """Specification 11.3: success requires an observed effect.

        Enforced on the model rather than trusted to the verifier, because this is
        the one claim the whole benchmark rests on — a token reduction from a task
        that did not actually happen is not a saving.
        """
        if self.status is ExecutionStatus.SUCCEEDED and (
            self.verification is None or not self.verification.passed
        ):
            raise ValueError(
                "an execution succeeds only when its expected effect was observed "
                "(specification 11.3)"
            )
        return self
