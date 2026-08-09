"""Phase 3 acceptance, end to end against the fixture server.

Every criterion in the specification's Phase 3 list appears here once, asserted
against a real Chromium reading a real page. The unit tests elsewhere construct
snapshots directly, which is faster and sharper — but a compiler that only ever
sees snapshots a test wrote is a compiler tested against its own assumptions
about what a page looks like.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest

from wcc.compiler.deterministic import GENERIC_DATA_ENTRY, compile_snapshot
from wcc.driver.base import DriverSettings
from wcc.driver.playwright_driver import PlaywrightDriver
from wcc.models.capability import CapabilityCategory, RiskLevel, SideEffect
from wcc.models.compilation import CompilationReport
from wcc.models.session import OpenSessionRequest
from wcc.policy.url_policy import UrlPolicy


@pytest.fixture
async def driver() -> AsyncIterator[PlaywrightDriver]:
    instance = PlaywrightDriver(UrlPolicy(), DriverSettings(allow_private_network=True))
    try:
        yield instance
    finally:
        await instance.shutdown()


async def _compile(driver: PlaywrightDriver, base: str, page: str) -> CompilationReport:
    """Open one page, compile it, and close the session again.

    Closing matters more than it looks. Several tests below compile four pages in
    a row, and a helper that leaked a browser context per call would leave the
    suite holding contexts in proportion to how many pages it inspected — a cost
    that shows up as an unexplained stall late in a full run rather than as a
    failure anyone can attribute.
    """
    session = await driver.open_session(OpenSessionRequest(url=f"{base}/{page}/"))
    try:
        snapshot = await driver.snapshot(session.session_id)
    finally:
        await driver.close_session(session.session_id)
    return compile_snapshot(snapshot)


async def test_the_search_fixture_produces_a_search_template(
    driver: PlaywrightDriver, fixture_server: str
) -> None:
    report = await _compile(driver, fixture_server, "search-page")
    searches = [t for t in report.templates if t.category is CapabilityCategory.SEARCH]
    assert len(searches) == 1
    template = searches[0]
    properties = template.parameters["properties"]
    assert isinstance(properties, dict)
    assert set(properties) == {"keyword", "category", "in_stock"}
    assert properties["in_stock"]["type"] == "boolean"
    assert template.risk.level is RiskLevel.LOW


async def test_the_filter_fixture_produces_a_filter_template(
    driver: PlaywrightDriver, fixture_server: str
) -> None:
    report = await _compile(driver, fixture_server, "filter-page")
    assert [t.category for t in report.templates] == [CapabilityCategory.FILTER]


async def test_pagination_and_result_items_share_one_navigation_template_each(
    driver: PlaywrightDriver, fixture_server: str
) -> None:
    """Specification 7.11: not one capability per row, and not one per page number."""
    report = await _compile(driver, fixture_server, "pagination-page")
    names = sorted(t.name for t in report.templates)
    assert names == [
        "navigate_next_page",
        "navigate_previous_page",
        "navigate_to_page",
        "open_result_item",
    ]

    open_item = next(t for t in report.templates if t.name == "open_result_item")
    assert open_item.parameters["required"] == ["entity_id"]
    # Two result links on the page; still exactly one capability, and its evidence
    # names both of them rather than the one that happened to be first.
    assert len(open_item.evidence) == 2


async def test_every_template_carries_confidence_evidence_risk_and_a_page_signature(
    driver: PlaywrightDriver, fixture_server: str
) -> None:
    for page in ("search-page", "filter-page", "product-list", "pagination-page"):
        report = await _compile(driver, fixture_server, page)
        assert report.templates, f"{page} produced no templates"
        for template in report.templates:
            assert template.confidence
            assert template.evidence
            assert template.page_signature.startswith("sha256:")
            assert template.risk.level
            # An element reference that cannot say which observation it came from
            # is a pointer at nothing once the template is persisted.
            for item in template.evidence:
                if item.element_id is not None:
                    assert item.snapshot_id is not None


async def test_the_post_contact_form_is_high_listed_and_not_executable(
    driver: PlaywrightDriver, fixture_server: str
) -> None:
    report = await _compile(driver, fixture_server, "post-contact-form")

    listed = [r for r in report.rejected if r.candidate_key == "contact"]
    assert len(listed) == 1
    assert listed[0].code == GENERIC_DATA_ENTRY
    assert listed[0].risk is not None
    assert listed[0].risk.level is RiskLevel.HIGH
    assert listed[0].risk.side_effect is SideEffect.PERSISTENT

    # Not executable: it is not a template at all, so nothing can bind it.
    assert all(t.name != "contact" for t in report.templates)
    assert GENERIC_DATA_ENTRY in {w.code for w in report.warnings}

    # And the refusal does not suppress the GET search form beside it.
    assert [t.category for t in report.templates] == [CapabilityCategory.SEARCH]


async def test_a_submitter_overriding_its_form_to_post_is_not_low_risk(
    driver: PlaywrightDriver, fixture_server: str
) -> None:
    """A GET form whose button declares ``formmethod="post"`` submits by POST.

    The page reads as an ordinary search. Classifying from the form alone would
    call it low and let it execute, so this is the counter-review finding worth a
    browser test rather than a constructed snapshot: the defect lived in what the
    extractor reported, not in what the classifier did with it.
    """
    report = await _compile(driver, fixture_server, "submitter-override")
    assert len(report.templates) == 1
    template = report.templates[0]
    assert template.category is CapabilityCategory.SEARCH
    assert template.risk.level is RiskLevel.HIGH
    assert template.risk.side_effect is SideEffect.PERSISTENT
    assert report.executable_templates() == []


async def test_compiling_the_same_page_twice_gives_the_same_capabilities(
    driver: PlaywrightDriver, fixture_server: str
) -> None:
    """Determinism, asserted on everything except the identifiers minted per run.

    Template IDs and timestamps differ by design — they record when this
    compilation happened. Everything an agent reads must not.
    """

    def shape(report: CompilationReport) -> list[tuple[object, ...]]:
        return [
            (
                t.name,
                t.category,
                t.confidence,
                t.risk.level,
                t.risk.side_effect,
                repr(t.parameters),
                tuple((s.action, s.target_ref, s.parameter) for s in t.execution_plan_template),
                tuple(sorted(t.target_locators)),
                t.page_signature,
            )
            for t in report.templates
        ]

    first = await _compile(driver, fixture_server, "product-list")
    second = await _compile(driver, fixture_server, "product-list")
    assert shape(first) == shape(second)
    assert {t.template_id for t in first.templates} != {t.template_id for t in second.templates}
