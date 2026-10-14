"""The coordinator: specification 11.1, in the order the specification states it.

Nine numbered steps run before a capability is allowed to touch a page, and eight
of them can refuse. That shape is deliberate — the gates are cheap, the action is
not undoable, and every gate that runs *after* an action would be an apology rather
than a check.

Two decisions are worth stating because they are not forced by the specification.

**The baseline snapshot is taken at execution time, not reused from inspection.**
Step 3 confirms the origin and re-resolves the targets, and both questions are
about the page as it is now. The same fresh snapshot is then the verifier's
"before", so what the gates checked and what the verification compares are provably
the same observation.

**One snapshot after the plan, not one after every step.** Step 7 reads as though a
snapshot follows each action, and taking one would mean a full extraction per
keystroke on a three-field form for an answer nobody reads: the verifier compares
the state before the plan with the state after it, and the intermediate steps are
already reported through their :class:`ActionObservation`, which carries settle and
document change. What is lost is the ability to attribute an effect to a particular
step of a multi-step plan; what is bought is that a search stays one page load
rather than four.
"""

from __future__ import annotations

import asyncio
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import UTC, datetime
from typing import Any, Protocol

from wcc.binding.binder import Binder, BindingRefused
from wcc.binding.registry import SessionRegistry
from wcc.compiler.deterministic import compile_snapshot
from wcc.compiler.reclassify import SnapshotRiskClassifier
from wcc.compiler.risk import classify_plan
from wcc.driver.base import DriverError
from wcc.enrich.enricher import Enricher
from wcc.identifiers import new_id
from wcc.ir.builder import build_page_ir
from wcc.limits import ResourceLimits
from wcc.models.action import ActionObservation, NavigationEvent, PlannedAction
from wcc.models.capability import (
    BindingStatus,
    BoundCapability,
    CapabilityTemplate,
    PlanStep,
    RiskLevel,
)
from wcc.models.common import ActionType
from wcc.models.compilation import CompilationReport
from wcc.models.element import NormalizedElement
from wcc.models.execution import (
    ExecutedStep,
    ExecutionError,
    ExecutionResult,
    ExecutionStatus,
    Verification,
)
from wcc.models.ir import PageIR
from wcc.models.locator import LocatorChain
from wcc.models.session import OpenSessionRequest, PageSnapshot, SessionInfo
from wcc.runtime.arguments import ArgumentError, validate_arguments
from wcc.runtime.targets import (
    ENTITY_PARAMETER,
    PAGE_PARAMETER,
    TargetUnavailable,
    current_record,
    entity_of,
    page_link_chain,
    record_of,
    resolve_member,
)
from wcc.runtime.verify import verify
from wcc.store.store import Store


class RuntimeDriver(Protocol):
    """Everything the coordinator asks of a browser, and nothing more.

    Wider than specification 4.1's five methods, and deliberately declared here
    rather than added to :class:`~wcc.driver.base.Driver`: the specification states
    that interface exactly, so the extra questions the runtime needs — re-resolve
    this saved chain, act on what I already resolved, is this inside that — live in
    the protocol of the component that asks them.
    """

    async def open_session(self, request: OpenSessionRequest) -> SessionInfo: ...

    async def close_session(self, session_id: str) -> None: ...

    async def snapshot(self, session_id: str) -> PageSnapshot: ...

    async def wait_settled(self, session_id: str, timeout_ms: int) -> None: ...

    async def resolve_chain(
        self, session_id: str, chain: LocatorChain, *, described_as: str = ...
    ) -> str: ...

    async def contains(self, session_id: str, container_id: str, member_id: str) -> bool: ...

    def drain_navigation_events(self, session_id: str) -> list[NavigationEvent]: ...

    async def describe_resolved(self, session_id: str, handle_id: str) -> NormalizedElement: ...

    def release(self, session_id: str, handle_id: str) -> None: ...

    async def execute_resolved_action(
        self, session_id: str, handle_id: str, action: PlannedAction
    ) -> ActionObservation: ...


@dataclass(frozen=True)
class Inspection:
    """What one ``inspect_page`` produced.

    ``reused`` is the cold/warm distinction the Phase 7 benchmark measures, and it
    is a fact about this inspection rather than a statistic derived later: warm
    means every capability offered here came from storage without the page being
    compiled again.
    """

    snapshot: PageSnapshot
    ir: PageIR
    capabilities: list[BoundCapability]
    templates: dict[str, CapabilityTemplate]
    reused: bool
    report: CompilationReport | None = None
    unbound: list[UnboundTemplate] = field(default_factory=list)
    """Templates this inspection tried to bind and could not, with the reason.

    A compiled template that fails to bind leaves the page looking as if nothing
    had been found, and without this the coverage run, the report and the agent
    were all silent about why. Only the pass that produced ``capabilities`` is
    reported: a stored template refusing before a recompile is the defined next
    move of specification 7.10, not news.
    """


@dataclass(frozen=True)
class UnboundTemplate:
    """One template the binder refused on this page, and what it said."""

    template_id: str
    name: str
    reason: str


@dataclass
class _SessionState:
    """What the runtime remembers about one live session.

    In memory and never persisted, for the reason
    :class:`~wcc.binding.registry.SessionRegistry` gives: a binding names a browser
    page, and no browser page outlives the process that opened it.
    """

    snapshot: PageSnapshot | None = None
    ir: PageIR | None = None
    bindings: dict[str, BoundCapability] = field(default_factory=dict)
    templates: dict[str, CapabilityTemplate] = field(default_factory=dict)


class ExecutionRefused(Exception):
    """A gate of specification 11.1 or 11.2 stopped the execution.

    ``status`` carries which kind, because the two need different things from the
    caller: ``STALE`` asks for a fresh inspection and a re-bind, ``REFUSED`` says
    the request itself will not become acceptable by retrying it.
    """

    def __init__(self, status: ExecutionStatus, code: str, message: str) -> None:
        super().__init__(message)
        self.status = status
        self.error = ExecutionError(code=code, message=message)


class Coordinator:
    """Cold compile, warm bind, and the execution protocol over one driver."""

    def __init__(
        self,
        driver: RuntimeDriver,
        store: Store,
        *,
        registry: SessionRegistry | None = None,
        binder: Binder | None = None,
        limits: ResourceLimits | None = None,
        enricher: Enricher | None = None,
    ) -> None:
        self._driver = driver
        self._store = store
        self._registry = registry or SessionRegistry()
        self._binder = binder or Binder(driver, SnapshotRiskClassifier())
        self._limits = limits or ResourceLimits()
        #: Inert by default. A coordinator built without one behaves exactly as it
        #: did before Phase 6, which is what "the full system works with the LLM
        #: disabled" has to mean at the level of the code rather than the config.
        self._enricher = enricher or Enricher()
        self._states: dict[str, _SessionState] = {}
        self._locks: dict[str, asyncio.Lock] = {}

    @property
    def registry(self) -> SessionRegistry:
        return self._registry

    def _lock(self, session_id: str) -> asyncio.Lock:
        """One session, one thing happening to it at a time.

        Every gate of specification 11.1 reads the page and then acts on what it
        read. Two concurrent executions on one session can both pass their gates
        against the same observation, and then the first one to run changes the
        page the second was cleared for — the second is now acting on a judgement
        about a page that no longer exists. Inspection and closing take the same
        lock, because both move the state the gates consult.
        """
        return self._locks.setdefault(session_id, asyncio.Lock())

    # -- session lifecycle -------------------------------------------------

    async def open_session(self, request: OpenSessionRequest) -> SessionInfo:
        session = await self._driver.open_session(request)
        self._registry.opened(session.session_id)
        self._states[session.session_id] = _SessionState()
        self._store.save_session(session)
        return session

    async def close_session(self, session_id: str) -> None:
        async with self._lock(session_id):
            await self._close_session(session_id)
        self._locks.pop(session_id, None)

    async def _close_session(self, session_id: str) -> None:
        """Close the page and retire every binding that named it.

        The registry alone would answer "stale" for these, and the records are
        updated anyway: a stored binding still marked active after its session
        ended is a row that disagrees with the truth, and the next process to read
        it has no way to tell that this one knew better.
        """
        state = self._states.pop(session_id, None)
        self._registry.closed(session_id)
        if state is not None:
            for binding in state.bindings.values():
                self._store.save_binding(
                    binding.model_copy(update={"status": BindingStatus.CLOSED})
                )
        await self._driver.close_session(session_id)

    # -- inspection: the warm path first, the cold path only if it fails ---

    async def inspect_page(self, session_id: str) -> Inspection:
        async with self._lock(session_id):
            return await self._inspect_page(session_id)

    async def _inspect_page(self, session_id: str) -> Inspection:
        """Observe the page and offer capabilities for it (specification 7.10).

        Lookup comes first and compilation is the fallback, which is the whole
        point of the cache: a page class already understood costs a snapshot and a
        re-resolution, not a compilation. Specification 7.10 also says a changed
        ``structural_hash`` does not prevent reuse — the page class is identified
        by origin and route, and whether the saved plan still works is answered by
        binding it rather than by comparing fingerprints.
        """
        snapshot = await self._driver.snapshot(session_id)
        ir = build_page_ir(snapshot)
        self._store.save_snapshot(snapshot)
        self._store.save_ir(ir)

        state = self._states.setdefault(session_id, _SessionState())
        state.snapshot = snapshot
        state.ir = ir
        state.bindings = {}
        state.templates = {}

        stored = self._store.find_templates(snapshot.origin, snapshot.normalized_route_pattern)
        bindings, unbound = await self._bind_all(session_id, snapshot, stored)
        if bindings:
            self._remember(state, bindings)
            return Inspection(
                snapshot=snapshot,
                ir=ir,
                capabilities=[binding for binding, _ in bindings],
                templates={template.template_id: template for _, template in bindings},
                reused=True,
                unbound=unbound,
            )

        report = compile_snapshot(snapshot)
        # Specification 9.1 places semantic naming inside the pipeline, before
        # persistence — so the warm path serves enriched names without a second
        # model call, and an export carries the name a client actually saw.
        enriched = await self._enricher.enrich(snapshot, report.templates)
        report = report.model_copy(update={"templates": enriched})
        saved = [self._store.save_template(template) for template in enriched]
        bindings, unbound = await self._bind_all(session_id, snapshot, saved)
        self._remember(state, bindings)
        return Inspection(
            snapshot=snapshot,
            ir=ir,
            capabilities=[binding for binding, _ in bindings],
            templates={template.template_id: template for _, template in bindings},
            reused=False,
            report=report,
            unbound=unbound,
        )

    async def _bind_all(
        self,
        session_id: str,
        snapshot: PageSnapshot,
        templates: list[CapabilityTemplate],
    ) -> tuple[list[tuple[BoundCapability, CapabilityTemplate]], list[UnboundTemplate]]:
        """Bind each template on its own, keeping the ones that hold.

        One template failing to bind says nothing about the next: they describe
        different capabilities on the same page. This is not the partial binding
        specification 7.10 step 6 forbids, which is about the targets *within* one
        template — and that rule is enforced inside the binder, where it belongs.
        The ones that do not hold are returned with the binder's reason rather than
        dropped, so the inspection can say why a page offers less than it compiled.
        """
        bound: list[tuple[BoundCapability, CapabilityTemplate]] = []
        unbound: list[UnboundTemplate] = []
        for template in templates:
            try:
                binding, matched = await self._binder.bind(session_id, snapshot, [template])
            except BindingRefused as refused:
                self._store.record_bind_outcome(template.template_id, succeeded=False)
                reason = refused.reasons.get(template.template_id, refused.message)
                unbound.append(UnboundTemplate(template.template_id, template.name, reason))
                continue
            self._store.record_bind_outcome(matched.template_id, succeeded=True)
            self._store.save_binding(binding)
            bound.append((binding, matched))
        return bound, unbound

    def _remember(
        self,
        state: _SessionState,
        bindings: list[tuple[BoundCapability, CapabilityTemplate]],
    ) -> None:
        for binding, template in bindings:
            state.bindings[binding.capability_id] = binding
            state.templates[binding.capability_id] = template

    # -- execution ---------------------------------------------------------

    async def execute(
        self,
        session_id: str,
        capability_id: str,
        arguments: dict[str, Any] | None = None,
    ) -> ExecutionResult:
        """Run one bound capability and return its complete result (11.1).

        Never raises for a refusal. Specification 11.1 step 9 returns the result
        synchronously, and a caller that has to catch exceptions to learn that its
        arguments were wrong is a caller reading two different report formats for
        one question.
        """
        async with self._lock(session_id):
            return await self._execute(session_id, capability_id, dict(arguments or {}))

    async def _execute(
        self, session_id: str, capability_id: str, arguments: dict[str, Any]
    ) -> ExecutionResult:
        started = datetime.now(UTC)
        execution_id = new_id("exec")

        try:
            state, binding, template = self._bound(session_id, capability_id)
        except ExecutionRefused as refused:
            return self._refused(execution_id, session_id, capability_id, "", started, refused)

        try:
            return await self._execute_bound(
                execution_id, started, session_id, state, binding, template, arguments
            )
        except ExecutionRefused as refused:
            if refused.status is ExecutionStatus.STALE:
                self._retire(state, binding)
            return self._refused(
                execution_id, session_id, capability_id, template.template_id, started, refused
            )

    async def _execute_bound(
        self,
        execution_id: str,
        started: datetime,
        session_id: str,
        state: _SessionState,
        binding: BoundCapability,
        template: CapabilityTemplate,
        arguments: dict[str, Any],
    ) -> ExecutionResult:
        before = await self._driver.snapshot(session_id)
        self._same_page(before, template, binding)
        handles = await self._resolve_targets(session_id, template)
        self._check_arguments(template, arguments)
        handles |= await self._resolve_members(
            session_id, state, template, before, arguments, handles
        )
        described = await self._describe(session_id, handles)
        self._check_risk(template, described)

        steps: list[ExecutedStep] = []
        try:
            steps, observations = await self._run_plan(
                session_id, template, handles, arguments, described
            )
            after, result_ir = await self._observe_result(session_id)
        except ExecutionRefused:
            raise
        except Exception as broke:
            # Once a step has run, the page has already been acted on. An exception
            # escaping from here would give the caller no result at all, which reads
            # as "it did not happen" and invites a retry of something that did.
            # Anything raised after the first action becomes a complete failed
            # result instead, saying which steps ran and that the effect is unknown.
            if not any(step.ran for step in steps):
                raise
            return self._unknown_effect(
                execution_id, started, session_id, binding, template, steps, broke
            )
        finally:
            for handle_id in handles.values():
                self._driver.release(session_id, handle_id)

        verification, observed = verify(template.expected_effect, before, after, observations)
        warnings = _state_changing_warnings(self._driver.drain_navigation_events(session_id))
        failed = next((step for step in steps if step.error is not None), None)

        status, error = self._verdict(failed, verification)
        result = ExecutionResult(
            execution_id=execution_id,
            session_id=session_id,
            capability_id=binding.capability_id,
            template_id=template.template_id,
            status=status,
            started_at=started,
            completed_at=datetime.now(UTC),
            steps=steps,
            observed_effects=observed,
            verification=verification,
            document_id_after=after.document_id,
            snapshot_id_after=after.snapshot_id,
            result_entities=result_ir.entities,
            warnings=warnings,
            error=error,
        )
        self._store.save_execution(result)
        if status is ExecutionStatus.SUCCEEDED:
            self._store.record_successful_use(template.template_id)
        return result

    def _unknown_effect(
        self,
        execution_id: str,
        started: datetime,
        session_id: str,
        binding: BoundCapability,
        template: CapabilityTemplate,
        steps: list[ExecutedStep],
        broke: Exception,
    ) -> ExecutionResult:
        """A result for an execution that acted and then could not be observed."""
        result = ExecutionResult(
            execution_id=execution_id,
            session_id=session_id,
            capability_id=binding.capability_id,
            template_id=template.template_id,
            status=ExecutionStatus.FAILED,
            started_at=started,
            completed_at=datetime.now(UTC),
            steps=steps,
            error=ExecutionError(
                code="effect_unknown",
                message=(
                    f"{sum(1 for step in steps if step.ran)} step(s) ran and the page "
                    f"could not be observed afterwards ({broke}); whether the effect "
                    "happened is not known"
                ),
            ),
        )
        self._store.save_execution(result)
        return result

    async def _observe_result(self, session_id: str) -> tuple[PageSnapshot, PageIR]:
        """Steps 7-8's observation: settle, look again, and read what is there."""
        await self._settle(session_id)
        after = await self._driver.snapshot(session_id)
        self._store.save_snapshot(after)
        result_ir = build_page_ir(after)
        self._store.save_ir(result_ir)
        return after, result_ir

    @staticmethod
    def _verdict(
        failed: ExecutedStep | None, verification: Verification
    ) -> tuple[ExecutionStatus, ExecutionError | None]:
        """Turn what happened into a status, letting the action fault win.

        A plan whose second step failed and whose page changed anyway is a failure,
        not a success with a note. Reporting it the other way round would credit
        the capability with an effect it did not complete — and specification 11.3
        is explicit that an unfinished task is never counted.
        """
        if failed is not None and failed.error is not None:
            return ExecutionStatus.FAILED, failed.error
        if not verification.passed:
            return ExecutionStatus.FAILED, ExecutionError(
                code="effect_not_observed",
                message=(
                    f"every step ran, and none of the expected signals "
                    f"{verification.missing} was observed; observed {verification.observed}"
                ),
            )
        return ExecutionStatus.SUCCEEDED, None

    def _bound(
        self, session_id: str, capability_id: str
    ) -> tuple[_SessionState, BoundCapability, CapabilityTemplate]:
        """Steps 1-2: the session is live and the capability belongs to it."""
        state = self._states.get(session_id)
        if state is None or not self._registry.is_live(session_id):
            raise ExecutionRefused(
                ExecutionStatus.STALE, "session_not_active", f"{session_id} is not an open session"
            )
        binding = state.bindings.get(capability_id)
        template = state.templates.get(capability_id)
        if binding is None or template is None:
            raise ExecutionRefused(
                ExecutionStatus.STALE,
                "capability_not_bound",
                f"{capability_id} is not a capability bound in {session_id}; "
                "inspect the page again to bind it",
            )
        if not self._registry.executable(binding):
            raise ExecutionRefused(
                ExecutionStatus.STALE,
                "binding_not_active",
                f"{capability_id} is {self._registry.status_of(binding)}",
            )
        return state, binding, template

    def _same_page(
        self,
        before: PageSnapshot,
        template: CapabilityTemplate,
        binding: BoundCapability,
    ) -> None:
        """Step 3's first half: this is still the page the capability was bound to."""
        if before.origin != template.origin:
            raise ExecutionRefused(
                ExecutionStatus.STALE,
                "origin_changed",
                f"the page is now {before.origin} and the capability was compiled "
                f"for {template.origin}",
            )
        if before.document_id != binding.document_id:
            raise ExecutionRefused(
                ExecutionStatus.STALE,
                "document_changed",
                "the page has navigated since this capability was bound",
            )

    def _check_arguments(self, template: CapabilityTemplate, arguments: dict[str, Any]) -> None:
        """Step 4: the arguments satisfy the capability's own schema."""
        try:
            validate_arguments(template.parameters, arguments)
        except ArgumentError as invalid:
            raise ExecutionRefused(ExecutionStatus.REFUSED, invalid.code, str(invalid)) from invalid

    async def _describe(
        self, session_id: str, handles: dict[str, str]
    ) -> dict[str, NormalizedElement]:
        """What each resolved target actually is, read back from the element itself.

        Not from the snapshot. The snapshot says what the page held a moment ago and
        has to be matched to the template by a second rule, which can disagree with
        the resolver — and when the two disagree it is the safe element that gets
        classified and the other one that gets clicked.
        """
        with self._as_stale():
            return {
                ref: await self._driver.describe_resolved(session_id, handle_id)
                for ref, handle_id in handles.items()
            }

    def _check_risk(
        self, template: CapabilityTemplate, targets: dict[str, NormalizedElement]
    ) -> None:
        """Step 5: only ``low`` may run, judged on the elements that will be acted on."""
        current, _ = classify_plan(template.category, template.execution_plan_template, targets)
        if current.level is not RiskLevel.LOW:
            raise ExecutionRefused(
                ExecutionStatus.REFUSED,
                "risk_not_low",
                f"this page now reads as {current.level} risk ({current.side_effect}), "
                "and specification 11.1 permits only low",
            )

    async def _resolve_targets(
        self, session_id: str, template: CapabilityTemplate
    ) -> dict[str, str]:
        """Step 3's second half: every target the template names, or none of them.

        All of them, before any of them is used. The binder makes the same move for
        the same reason (specification 7.10 step 6): a plan that filled two fields
        and then found its submit missing has already typed into the page.

        This runs before the argument check, which is the order specification 11.1
        numbers — and it is the order that reports the more useful of two true
        answers. A page whose submit has been removed does not become executable by
        fixing the arguments, so "stale, inspect again" is what the caller needs to
        hear, not "that integer should have been a string".
        """
        with self._as_stale():
            return {
                ref: await self._driver.resolve_chain(
                    session_id, chain, described_as=f"{template.name}.{ref}"
                )
                for ref, chain in template.target_locators.items()
            }

    async def _resolve_members(
        self,
        session_id: str,
        state: _SessionState,
        template: CapabilityTemplate,
        before: PageSnapshot,
        arguments: dict[str, Any],
        containers: dict[str, str],
    ) -> dict[str, str]:
        """The rest of step 3, for the targets an argument has to choose.

        Necessarily after step 4: a member is selected *by* an argument, and
        resolving one before the argument had been validated would mean acting on a
        value the schema was about to reject. The specification numbers the steps
        without anticipating a target that depends on an argument; this splits the
        re-resolution rather than moving the validation, so nothing unvalidated is
        ever used and every target is still resolved before any step runs.
        """
        with self._as_stale():
            return {
                step.target_ref: await self._member(
                    session_id, state, before, containers[step.target_ref], step, arguments
                )
                for step in template.execution_plan_template
                if step.parameter in (ENTITY_PARAMETER, PAGE_PARAMETER)
            }

    @contextmanager
    def _as_stale(self) -> Iterator[None]:
        """Report every failure to identify a target as specification 11.2's ``stale``."""
        try:
            yield
        except TargetUnavailable as unavailable:
            raise ExecutionRefused(
                ExecutionStatus.STALE, "target_unavailable", unavailable.message
            ) from unavailable
        except DriverError as unresolved:
            raise ExecutionRefused(
                ExecutionStatus.STALE, "target_unavailable", str(unresolved)
            ) from unresolved

    async def _member(
        self,
        session_id: str,
        state: _SessionState,
        before: PageSnapshot,
        container_id: str,
        step: PlanStep,
        arguments: dict[str, Any],
    ) -> str:
        """The member of a container that this call's argument selects."""
        if step.parameter == ENTITY_PARAMETER:
            if state.ir is None:
                raise TargetUnavailable("no inspection has issued entities for this session")
            entity = entity_of(state.ir, str(arguments[ENTITY_PARAMETER]))
            observed = record_of(state.snapshot or before, entity)
            record = current_record(before, observed.identity)
            if record.activation_locator is None:
                raise TargetUnavailable(
                    f"{entity.entity_id} names a record with no link of its own, "
                    "so there is nothing this capability could open"
                )
            chain = record.activation_locator
            described = f"entity {entity.entity_id}"
        else:
            page = int(arguments[PAGE_PARAMETER])
            chain = page_link_chain(before, page)
            described = f"page {page}"

        return await resolve_member(self._driver, session_id, container_id, chain, described)

    async def _run_plan(
        self,
        session_id: str,
        template: CapabilityTemplate,
        handles: dict[str, str],
        arguments: dict[str, Any],
        described: dict[str, NormalizedElement],
    ) -> tuple[list[ExecutedStep], list[ActionObservation]]:
        """Step 6: one allowlisted action at a time, stopping at the first failure.

        Continuing past a failed step would run the rest of a plan whose
        precondition no longer holds — filling the remaining fields of a form and
        submitting it without the value the failed step was supposed to supply,
        which submits something the caller never asked for.
        """
        steps: list[ExecutedStep] = []
        observations: list[ActionObservation] = []
        stopped = False

        for index, step in enumerate(template.execution_plan_template):
            if stopped:
                steps.append(_pending(index, step))
                continue
            if _is_skipped(step, arguments):
                steps.append(
                    ExecutedStep(
                        index=index,
                        action=step.action,
                        target_ref=step.target_ref,
                        parameter=step.parameter,
                    )
                )
                continue

            if step.action is ActionType.ACTIVATE:
                await self._still_the_same_act(session_id, step, handles, described)

            action = _planned(step, handles[step.target_ref], arguments)
            observation = await self._driver.execute_resolved_action(
                session_id, handles[step.target_ref], action
            )
            observations.append(observation)
            error = (
                None
                if observation.succeeded
                else ExecutionError(
                    code=observation.error_code or "action_failed",
                    message=observation.error_message or f"step {index} did not complete",
                )
            )
            steps.append(
                ExecutedStep(
                    index=index,
                    action=step.action,
                    target_ref=step.target_ref,
                    parameter=step.parameter,
                    observation=observation,
                    error=error,
                )
            )
            stopped = error is not None

        return steps, observations

    async def _still_the_same_act(
        self,
        session_id: str,
        step: PlanStep,
        handles: dict[str, str],
        described: dict[str, NormalizedElement],
    ) -> None:
        """Re-read an activation target immediately before activating it.

        The gate ran before the first step, and a plan has more than one. Filling a
        field fires the page's own event handlers, and one of them may change what
        the submit does — ``formmethod`` from GET to POST is a single attribute. The
        handle would still point at the same element, so nothing else would notice.

        Only the risk-relevant signals are compared, and only before an activation:
        that is the step whose consequence the classification described.
        """
        now = await self._driver.describe_resolved(session_id, handles[step.target_ref])
        before = described[step.target_ref]
        if _risk_signature(now) != _risk_signature(before):
            raise ExecutionRefused(
                ExecutionStatus.REFUSED,
                "target_changed_mid_plan",
                f"{step.target_ref} changed what it does while the plan was running; "
                "the risk this capability was cleared for no longer describes it",
            )

    async def _settle(self, session_id: str) -> None:
        """Step 7's settle, which a busy page must not turn into a failed execution.

        The action has already happened. A page that never goes quiet leaves the
        verification comparing an early view of the result, which is a weaker
        answer — and a weaker answer is what the verifier is for.
        """
        try:
            await self._driver.wait_settled(session_id, self._limits.settle_timeout_ms)
        except DriverError:
            return

    def _retire(self, state: _SessionState, binding: BoundCapability) -> None:
        """Drop a binding the page has outgrown (specification 11.2 step 4)."""
        state.bindings.pop(binding.capability_id, None)
        state.templates.pop(binding.capability_id, None)
        self._store.save_binding(binding.model_copy(update={"status": BindingStatus.STALE}))

    def _refused(
        self,
        execution_id: str,
        session_id: str,
        capability_id: str,
        template_id: str,
        started: datetime,
        refused: ExecutionRefused,
    ) -> ExecutionResult:
        result = ExecutionResult(
            execution_id=execution_id,
            session_id=session_id,
            capability_id=capability_id,
            template_id=template_id,
            status=refused.status,
            started_at=started,
            completed_at=datetime.now(UTC),
            error=refused.error,
        )
        self._store.save_execution(result)
        return result

    def execution_result(self, session_id: str, execution_id: str) -> ExecutionResult | None:
        """Retrieve a stored result, refusing one that belongs to another session.

        Ownership is checked in the query rather than after it. A caller learning
        that an execution exists but is not theirs has already learned something
        about another session.
        """
        return self._store.load_execution(execution_id, session_id=session_id)


def _pending(index: int, step: PlanStep) -> ExecutedStep:
    return ExecutedStep(
        index=index,
        action=step.action,
        target_ref=step.target_ref,
        parameter=step.parameter,
        error=ExecutionError(
            code="not_attempted", message="an earlier step failed, so this one did not run"
        ),
    )


def _is_skipped(step: PlanStep, arguments: dict[str, Any]) -> bool:
    """Whether this step has no argument to carry out.

    The compiler marks a parameter required exactly when the form does, so an
    optional one the caller omitted is a control the caller chose not to touch. The
    step is recorded and not run: filling it with an empty string would clear
    whatever default the page had chosen, which is a change nobody requested, and
    on a filter form it is the difference between "any category" and "no category".
    """
    return step.parameter is not None and step.parameter not in arguments


def _planned(step: PlanStep, handle_id: str, arguments: dict[str, Any]) -> PlannedAction:
    """Build the driver action for one step, from the argument it names."""
    value = arguments.get(step.parameter) if step.parameter else None

    if step.action is ActionType.SET_VALUE:
        return PlannedAction(action=step.action, element_id=handle_id, value=_as_text(value))
    if step.action is ActionType.SELECT_OPTION:
        return PlannedAction(action=step.action, element_id=handle_id, option=_as_text(value))
    if step.action is ActionType.SET_CHECKED:
        return PlannedAction(action=step.action, element_id=handle_id, checked=bool(value))
    return PlannedAction(action=step.action, element_id=handle_id)


def _as_text(value: Any) -> str:
    """Render an argument as the text a control receives.

    Booleans are spelled in JSON's lower case rather than Python's. A field filled
    with ``True`` would reach the server as a value no form on the web expects, and
    the mismatch would show up as a search that silently returned nothing.
    """
    if value is None:
        return ""
    if isinstance(value, bool):
        return "true" if value else "false"
    return str(value)


def _state_changing_warnings(events: list[NavigationEvent]) -> list[str]:
    """Name every state-changing request the page made while the plan ran.

    One line per distinct URL rather than one per request: a handler firing on each
    keystroke would otherwise bury the result in its own repetition.
    """
    seen = {
        event.url or "an unnamed address"
        for event in events
        if event.kind == "state_changing_request"
    }
    return [
        f"the page issued a state-changing request to {url} during an execution "
        "classified as low risk"
        for url in sorted(seen)
    ]


def _risk_signature(element: NormalizedElement) -> tuple[object, ...]:
    """Everything :func:`classify_plan` reads off one element.

    Kept next to the mid-plan check rather than imported from the classifier,
    because the two are answering different questions: the classifier asks what the
    risk *is*, and this asks whether the answer it gave is still about the same
    thing.
    """
    return (
        element.control_type,
        element.submits_form,
        element.form_method,
        element.form_action_kind,
        element.form_action_route,
        element.resolved_label,
    )
