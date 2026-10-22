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
here. Result links are the activation links of a detected collection's records
(specification 8.4), not links that merely repeat: tags, categories, bylines and
per-item actions all repeat, and on real pages they routinely outnumber the
results they belong to. The container is the links' longest common path prefix.
Links inside navigation landmarks are excluded before the comparison, because a
header nav holding "Home" and "Products" is still not a result list when it is
detected as a collection.
"""

from __future__ import annotations

from wcc.compiler.reclassify import container_representative
from wcc.compiler.risk import classify_plan, names_destructive_act
from wcc.models.capability import (
    CapabilityCategory,
    ExpectedEffect,
    PlanStep,
    Risk,
)
from wcc.models.common import ActionType, Confidence, ControlType
from wcc.models.element import CollectionSnapshot, NormalizedElement
from wcc.models.locator import LocatorCandidate, LocatorChain, LocatorKind

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


def find_result_links(
    elements: list[NormalizedElement], collections: list[CollectionSnapshot]
) -> list[NormalizedElement]:
    """The activation links of the collection that is the page's result list.

    A collection qualifies with two or more records whose activation link is a
    non-pagination link outside any navigation landmark. Collections whose records
    say something beyond their link are preferred to collections of bare links —
    ten quotes over a ten-tag sidebar, twenty books over fifty category links —
    and a page whose only repeated links are bare still gets a capability for
    them, because a plain list of result links is a result list too. A collection
    most of whose links head a list of their own is a row of footer columns, not a
    result list, and does not qualify.

    Within the preferred kind the largest wins, and only when one is largest. Two
    collections of equal size is a page with two result lists, and picking either
    would attach ``open_result_item`` to a collection the agent did not mean. That
    case is left uncompiled rather than guessed.
    """
    links: dict[str, NormalizedElement] = {}
    for element in elements:
        if element.control_type is not ControlType.LINK:
            continue
        if element.in_navigation or element.is_pagination_control:
            continue
        path = _structural_path(element)
        if path is not None:
            links[path] = element

    # Keyed by the member paths: a collection nested in another, or detected from
    # two container levels, is one list and must not tie with itself.
    candidates: dict[tuple[str, ...], bool] = {}
    for collection in collections:
        activatable = [r for r in collection.records if r.activation_locator is not None]
        paths = tuple(
            dict.fromkeys(
                key
                for record in activatable
                if record.activation_locator is not None
                and (key := record.activation_locator.primary.key) in links
            )
        )
        if len(paths) < _MIN_REPEATED:
            continue
        bare = all(record.link_only for record in activatable)
        candidates[paths] = candidates.get(paths, True) and bare

    candidates = {
        paths: bare
        for paths, bare in candidates.items()
        if not _heads_of_other_lists(paths, candidates)
    }
    for bare in (False, True):
        tier = [paths for paths, is_bare in candidates.items() if is_bare is bare]
        if not tier:
            continue
        largest = max(len(paths) for paths in tier)
        winners = [paths for paths in tier if len(paths) == largest]
        return [links[path] for path in winners[0]] if len(winners) == 1 else []
    return []


def _heads_of_other_lists(paths: tuple[str, ...], candidates: dict[tuple[str, ...], bool]) -> bool:
    """Whether most of these links are each the first link of a list of their own.

    A footer column is a heading over a list of links, so a row of columns is
    detected as cards that say something beyond their link — the heading — and
    each card's link is merely the first entry of its column. On crates.io that
    made "rust-lang.org, The Cargo Book, Usage Policy" the result list. The test is
    per record: a link heads a list when it sits in another detected list that
    shares nothing else with this one. A list nested inside one larger list shares
    all its links with it and is not excluded, and a quote card's author link is
    in no tag list.

    More than half, not all. go.dev's row of use-case columns ends in a "More use
    cases" card built like the columns with no list under it, and one record
    heading nothing kept the whole row as the result list. A result list in which
    one link also starts a list of related links is still mostly records that head
    nothing, and stays.
    """
    mine = set(paths)
    heads = sum(any(set(other) & mine == {path} for other in candidates) for path in paths)
    return heads * 2 > len(paths)


def _worst(members: list[NormalizedElement]) -> NormalizedElement:
    """The member whose own label names a destructive act, or the first.

    One "Delete" link among thirty products decides the list's risk. Specification
    10.1 says uncertainty raises and never lowers, and a rule that read only the
    first member would lower it by the accident of ordering.
    """
    return next((m for m in members if names_destructive_act(m.resolved_label)), members[0])


def build_result_item_template(
    elements: list[NormalizedElement], collections: list[CollectionSnapshot]
) -> NavigationTemplate | None:
    """The shared ``open_result_item`` capability of specification 7.11."""
    links = find_result_links(elements, collections)
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
    # The member is the one binding will re-classify against — the worst of every
    # element under the saved path, not only of the records that chose it — so the
    # level saved here is the level binding reads back. When those members do not
    # agree, binding refuses whatever is saved, and the records' worst link at
    # least says why the list was offered.
    representative = container_representative(container, elements) or _worst(links)
    risk, reason = classify_plan(CapabilityCategory.NAVIGATE, plan, {ref: representative})

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
