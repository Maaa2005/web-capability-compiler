"""Shared navigation capabilities (specification 7.11, 8.4).

Specification 7.11 opens with a prohibition rather than a design: do not generate
one capability per result item. The reason is not tidiness. A page of thirty
products would otherwise produce thirty capabilities that differ only in which row
they click, an agent would have to read all thirty to choose, and every one of
them would be invalidated by the next snapshot. The capability an agent actually
wants is "open a result", parameterized by which one.

That shape needs a target the parameter can select within, so the templates here
locate the *container* and let the argument choose the member:

``open_result_item``
    activates the item named by ``entity_id`` inside the result collection

``navigate_to_page``
    activates the link for ``page`` inside the pagination landmark

``navigate_next_page`` / ``navigate_previous_page``
    single links that declare ``rel``, so they need no parameter at all

Finding the container without being told about it is the one piece of inference
here. Result links are recognised as a repeated structure: two or more links
outside any navigation landmark whose structural paths differ only in a sibling
index. The container is their longest common path prefix. This is deliberately
narrow — a header nav holding "Home" and "Products" has exactly that shape, which
is why links inside navigation landmarks are excluded before the comparison rather
than filtered out of the result.
"""

from __future__ import annotations

import re
from collections import OrderedDict

from wcc.compiler.risk import classify_plan
from wcc.models.capability import (
    CapabilityCategory,
    ExpectedEffect,
    PlanStep,
    Risk,
)
from wcc.models.common import ActionType, Confidence, ControlType
from wcc.models.element import NormalizedElement
from wcc.models.locator import LocatorCandidate, LocatorChain, LocatorKind

_INDEX = re.compile(r"\[\d+\]$")

#: How many links must share a shape before it counts as a repeated structure.
#: Two is the minimum that can repeat at all; requiring three would make a
#: two-result page report no way to open its results.
_MIN_REPEATED = 2


class NavigationTemplate:
    """One generated navigation capability, before it becomes a template."""

    def __init__(
        self,
        name: str,
        description: str,
        parameters: dict[str, object],
        plan: list[PlanStep],
        locators: dict[str, LocatorChain],
        targets: dict[str, NormalizedElement],
        evidence_elements: list[NormalizedElement],
        confidence: Confidence,
        risk: Risk,
        risk_reason: str,
    ) -> None:
        self.name = name
        self.description = description
        self.parameters = parameters
        self.plan = plan
        self.locators = locators
        self.targets = targets
        self.evidence_elements = evidence_elements
        self.confidence = confidence
        self.risk = risk
        self.risk_reason = risk_reason
        self.category = CapabilityCategory.NAVIGATE
        self.expected_effect = ExpectedEffect(type="page_state_change", signals=["url_change"])


def _structural_path(element: NormalizedElement) -> str | None:
    for candidate in element.locator_chain.candidates:
        if candidate.kind is LocatorKind.STRUCTURAL_PATH:
            return candidate.key
    return None


def _segments(path: str) -> list[str]:
    return [part for part in path.split(">") if part]


def _shape(path: str) -> str:
    """The path with sibling indices removed, so two rows of a list look alike."""
    return ">".join(_INDEX.sub("", part) for part in _segments(path))


def common_prefix(paths: list[str]) -> str | None:
    """The deepest path every member shares, or ``None`` when they share nothing.

    Compared segment by segment including the index, so ``li[1]`` and ``li[2]``
    stop the prefix at their parent. That is the container, which is exactly what
    the shared capability needs to locate.
    """
    if not paths:
        return None
    split = [_segments(path) for path in paths]
    prefix: list[str] = []
    for parts in zip(*split, strict=False):
        first = parts[0]
        if any(part != first for part in parts):
            break
        prefix.append(first)
    return ">".join(prefix) if prefix else None


def _container_chain(path: str, role: str | None = None) -> LocatorChain:
    candidate = LocatorCandidate(kind=LocatorKind.STRUCTURAL_PATH, key=path, role=role)
    return LocatorChain(primary=candidate, candidates=[candidate])


def find_result_links(elements: list[NormalizedElement]) -> list[NormalizedElement]:
    """Links that repeat as a structure, which is what a result list is made of.

    Returns the largest such group, and only when one group is largest. Two groups
    of equal size is a page with two result lists, and picking either would attach
    ``open_result_item`` to a collection the agent did not mean. That case is left
    uncompiled rather than guessed.
    """
    grouped: OrderedDict[str, list[NormalizedElement]] = OrderedDict()
    for element in elements:
        if element.control_type is not ControlType.LINK:
            continue
        if element.in_navigation or element.is_pagination_control:
            continue
        path = _structural_path(element)
        if path is None or "[" not in path:
            continue
        grouped.setdefault(_shape(path), []).append(element)

    groups = [members for members in grouped.values() if len(members) >= _MIN_REPEATED]
    if not groups:
        return []
    largest = max(len(members) for members in groups)
    winners = [members for members in groups if len(members) == largest]
    return winners[0] if len(winners) == 1 else []


def build_result_item_template(elements: list[NormalizedElement]) -> NavigationTemplate | None:
    """The shared ``open_result_item`` capability of specification 7.11."""
    links = find_result_links(elements)
    if not links:
        return None
    paths = [p for p in (_structural_path(link) for link in links) if p]
    container = common_prefix(paths)
    if container is None:
        return None

    ref = "result_collection"
    plan = [PlanStep(action=ActionType.ACTIVATE, target_ref=ref, parameter="entity_id")]
    # Risk is classified against a member link rather than against the container,
    # because activating the container is not what happens: one of these links is.
    # The members are homogeneous by construction — every one is a non-pagination
    # link outside any navigation landmark — so any of them describes the act.
    risk, reason = classify_plan(CapabilityCategory.NAVIGATE, plan, {ref: links[0]})

    return NavigationTemplate(
        name="open_result_item",
        description=(
            "Open one result by entity reference. The runtime resolves entity_id only "
            "from entities issued in the current session and snapshot lineage."
        ),
        parameters={
            "type": "object",
            "properties": {"entity_id": {"type": "string"}},
            "required": ["entity_id"],
            "additionalProperties": False,
        },
        plan=plan,
        locators={ref: _container_chain(container)},
        targets={ref: links[0]},
        evidence_elements=links,
        confidence=Confidence.MEDIUM,
        risk=risk,
        risk_reason=reason,
    )


def build_pagination_templates(elements: list[NormalizedElement]) -> list[NavigationTemplate]:
    """Templates for paging, one per distinct way the page offers to page.

    A declared ``rel`` becomes its own parameterless capability and rates high:
    ``rel="next"`` is the page stating a relationship, not the compiler reading a
    word. Numbered links collapse into a single ``navigate_to_page`` taking the
    number, and rate medium, because the reading rests on the surrounding
    landmark rather than on a declaration.
    """
    templates: list[NavigationTemplate] = []
    controls = [e for e in elements if e.is_pagination_control]

    by_rel: dict[str, list[NormalizedElement]] = {}
    numbered: list[NormalizedElement] = []
    for element in controls:
        if element.pagination_rel in {"next", "prev", "previous"}:
            key = "prev" if element.pagination_rel == "previous" else element.pagination_rel
            by_rel.setdefault(key, []).append(element)
        elif element.pagination_rel is None:
            numbered.append(element)

    for rel, name in (("next", "navigate_next_page"), ("prev", "navigate_previous_page")):
        members = by_rel.get(rel, [])
        # More than one control claiming the same relationship is the page
        # contradicting itself; specification 7.7's rule against choosing the
        # closest applies just as much before resolution as during it.
        if len(members) != 1:
            continue
        control = members[0]
        ref = f"{rel}_page"
        plan = [PlanStep(action=ActionType.ACTIVATE, target_ref=ref)]
        risk, reason = classify_plan(CapabilityCategory.NAVIGATE, plan, {ref: control})
        templates.append(
            NavigationTemplate(
                name=name,
                description=f'Follow the link the page declares as rel="{rel}".',
                parameters={"type": "object", "properties": {}, "additionalProperties": False},
                plan=plan,
                locators={ref: control.locator_chain},
                targets={ref: control},
                evidence_elements=[control],
                confidence=Confidence.HIGH,
                risk=risk,
                risk_reason=reason,
            )
        )

    if len(numbered) >= _MIN_REPEATED:
        paths = [p for p in (_structural_path(e) for e in numbered) if p]
        container = common_prefix(paths)
        if container is not None:
            ref = "pagination"
            plan = [PlanStep(action=ActionType.ACTIVATE, target_ref=ref, parameter="page")]
            risk, reason = classify_plan(CapabilityCategory.NAVIGATE, plan, {ref: numbered[0]})
            templates.append(
                NavigationTemplate(
                    name="navigate_to_page",
                    description=(
                        "Go to a numbered page by activating its link inside the "
                        "pagination landmark."
                    ),
                    parameters={
                        "type": "object",
                        # No maximum. The pages observed in one snapshot are evidence
                        # of what existed then, and freezing that count into a
                        # portable template would make the schema wrong for the same
                        # page tomorrow. Phase 4 validates the argument against the
                        # live page, which is where the answer actually is.
                        "properties": {"page": {"type": "integer", "minimum": 1}},
                        "required": ["page"],
                        "additionalProperties": False,
                    },
                    plan=plan,
                    locators={ref: _container_chain(container)},
                    targets={ref: numbered[0]},
                    evidence_elements=numbered,
                    confidence=Confidence.MEDIUM,
                    risk=risk,
                    risk_reason=reason,
                )
            )

    return templates
