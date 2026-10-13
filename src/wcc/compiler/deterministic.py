"""The deterministic compiler: one snapshot in, one report out (specification 9.1).

This module owns the order of the pipeline and nothing else. Detection lives in
:mod:`wcc.compiler.forms` and :mod:`wcc.compiler.navigation`, risk lives in
:mod:`wcc.compiler.risk`, and keeping the three apart is what makes the ordering
of specification 9.1 legible: candidates are found, then named, then given a
parameter schema, then classified for risk, and only then persisted. Risk is last
among those because it is computed from the finished plan; a classifier consulted
earlier would be classifying an intention rather than an act.

Nothing here reaches a browser and nothing here calls a model. A snapshot is a
value, so the whole compiler is testable by constructing one — which is the only
reason the Phase 3 acceptance can assert on categories, schemas, and risk without
Chromium being the thing under test.
"""

from __future__ import annotations

from datetime import UTC, datetime

from wcc.compiler.forms import (
    CompiledForm,
    FormGroup,
    FormReading,
    FormRefusal,
    compile_form,
    group_forms,
    read_form,
    refusal_plan,
)
from wcc.compiler.navigation import (
    NavigationTemplate,
    build_pagination_templates,
    build_result_item_template,
)
from wcc.compiler.risk import classify_plan
from wcc.identifiers import new_id
from wcc.models.capability import CapabilityTemplate
from wcc.models.common import Confidence
from wcc.models.compilation import CompilationReport, RejectedCandidate
from wcc.models.element import CollectionSnapshot, NormalizedElement
from wcc.models.ir import Evidence, IRWarning
from wcc.models.session import PageSnapshot

#: Stable codes. An agent branching on prose breaks when the prose improves.
GENERIC_DATA_ENTRY = "generic_data_entry_form"
LOW_CONFIDENCE = "low_confidence_candidate"


def compile_snapshot(snapshot: PageSnapshot) -> CompilationReport:
    """Compile every capability the snapshot deterministically supports."""
    templates: list[CapabilityTemplate] = []
    rejected: list[RejectedCandidate] = []
    warnings: list[IRWarning] = []
    used_names: set[str] = set()

    for group in group_forms(snapshot.elements):
        reading = read_form(group)

        if reading.category is None:
            rejected.append(_reject(group, GENERIC_DATA_ENTRY, _generic_message(group)))
            warnings.append(
                IRWarning(
                    code=GENERIC_DATA_ENTRY,
                    message=(
                        f"{group.display} is a generic data-entry form; "
                        "specification 8.3 reports these and does not compile them"
                    ),
                )
            )
            continue

        if reading.confidence is Confidence.LOW:
            rejected.append(
                _reject(
                    group,
                    LOW_CONFIDENCE,
                    f"{group.display}: reads as {reading.category} only by "
                    f"{reading.reason} — too weak to compile",
                    confidence=Confidence.LOW,
                )
            )
            warnings.append(
                IRWarning(
                    code=LOW_CONFIDENCE,
                    message=f"{group.display} was not compiled: {reading.reason} is low",
                )
            )
            continue

        try:
            compiled = compile_form(reading)
        except FormRefusal as refusal:
            rejected.append(
                _reject(group, refusal.code, refusal.message, confidence=reading.confidence)
            )
            warnings.append(IRWarning(code=refusal.code, message=refusal.message))
            continue

        templates.append(_form_template(snapshot, reading, compiled, used_names))

    for navigation in _navigation_templates(snapshot.elements, snapshot.collections):
        templates.append(_navigation_template(snapshot, navigation, used_names))

    return CompilationReport(
        snapshot_id=snapshot.snapshot_id,
        templates=templates,
        rejected=rejected,
        warnings=warnings,
    )


def _navigation_templates(
    elements: list[NormalizedElement], collections: list[CollectionSnapshot]
) -> list[NavigationTemplate]:
    found = list(build_pagination_templates(elements))
    result_items = build_result_item_template(elements, collections)
    if result_items is not None:
        found.append(result_items)
    return found


def _generic_message(group: FormGroup) -> str:
    """Why this form was not compiled, in the length an agent needs.

    The first version of this sentence explained the rule: "…resolves to no
    deterministic search or filter reading, so it is reported and not compiled".
    Accurate, and written for a person reading a test failure. It is sent to an
    agent on every inspection of the page, where the only decision it informs is
    "there is a form here I cannot offer you" — and on a small page two of these
    cost more than the capability that *was* compiled.

    The rule still lives in this module's documentation, which is where a person
    looks it up. The response carries the fact.
    """
    return f"{group.display}: submits by {group.method.upper()}, reads as neither search nor filter"


def _reject(
    group: FormGroup,
    code: str,
    message: str,
    confidence: Confidence | None = None,
) -> RejectedCandidate:
    """Record a refusal with its risk, when the risk can be computed.

    The risk of something refused is the whole point of listing it. Phase 3's
    acceptance asks that the POST contact form be "high, listed, and not
    executable", and that sentence is only true of a refusal that carries its
    classification.
    """
    risk = None
    minimal = refusal_plan(group)
    if minimal is not None:
        plan, targets = minimal
        # No category, deliberately. Naming one to obtain a classification would be
        # asserting the reading the compiler just declined to make.
        risk, _ = classify_plan(None, plan, targets)

    return RejectedCandidate(
        candidate_key=group.display,
        code=code,
        message=message,
        risk=risk,
        confidence=confidence,
        evidence=[
            Evidence(kind="dom", element_id=element.element_id, attribute="control")
            for element in group.elements
        ],
    )


def _unique(name: str, used: set[str]) -> str:
    """Keep capability names distinct within one page, deterministically.

    Suffixed by order of appearance rather than by anything about the form, so two
    compilations of an unchanged page agree. A name that silently replaced another
    would hide a whole capability.
    """
    candidate = name
    index = 2
    while candidate in used:
        candidate = f"{name}_{index}"
        index += 1
    used.add(candidate)
    return candidate


def _form_template(
    snapshot: PageSnapshot,
    reading: FormReading,
    compiled: CompiledForm,
    used_names: set[str],
) -> CapabilityTemplate:
    evidence = [
        Evidence(
            kind="dom",
            element_id=element.element_id,
            snapshot_id=snapshot.snapshot_id,
            attribute="control",
            detail=f"target {ref}",
        )
        for ref, element in compiled.targets.items()
    ]
    return CapabilityTemplate(
        template_id=new_id("tmpl"),
        origin=snapshot.origin,
        normalized_route_pattern=snapshot.normalized_route_pattern,
        page_signature=snapshot.page_signature,
        name=_unique(compiled.name, used_names),
        category=compiled.category,
        description=(
            f"{compiled.category.capitalize()} using {reading.group.display}, "
            f"detected by {reading.reason}. Risk: {compiled.risk_reason}."
        ),
        parameters=compiled.parameters,
        execution_plan_template=compiled.plan,
        target_locators=compiled.locators,
        expected_effect=compiled.expected_effect,
        risk=compiled.risk,
        evidence=evidence,
        confidence=compiled.confidence,
        created_at=datetime.now(UTC),
    )


def _navigation_template(
    snapshot: PageSnapshot,
    navigation: NavigationTemplate,
    used_names: set[str],
) -> CapabilityTemplate:
    evidence = [
        Evidence(
            kind="dom",
            element_id=element.element_id,
            snapshot_id=snapshot.snapshot_id,
            attribute="href",
        )
        for element in navigation.evidence_elements
    ]
    return CapabilityTemplate(
        template_id=new_id("tmpl"),
        origin=snapshot.origin,
        normalized_route_pattern=snapshot.normalized_route_pattern,
        page_signature=snapshot.page_signature,
        name=_unique(navigation.name, used_names),
        category=navigation.category,
        description=f"{navigation.description} Risk: {navigation.risk_reason}.",
        parameters=navigation.parameters,
        execution_plan_template=navigation.plan,
        target_locators=navigation.locators,
        expected_effect=navigation.expected_effect,
        risk=navigation.risk,
        evidence=evidence,
        confidence=navigation.confidence,
        created_at=datetime.now(UTC),
    )
