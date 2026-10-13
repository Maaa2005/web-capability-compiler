"""What a page can do to the script that reads it (specification 10.3, 10.5).

The extraction script runs inside a document it does not trust, and until a
counter-review said so out loud, it was written as if it did. These tests are the
findings, each against a fixture that reproduced the failure before the fix:

- one element with a throwing getter aborted the entire observation;
- specification 10.5's element cap was applied in Python *after* the script had
  described every control, so an enormous page was fully walked before refusal,
  and the landmark scan recursed once per node on the way;
- ``label[for]`` took the first match, so a second label could hide the word the
  risk classifier reads.

Each runs in a real browser, because the thing being tested is what JavaScript
does to JavaScript.
"""

from __future__ import annotations

from collections.abc import AsyncIterator

import pytest

from wcc.compiler.navigation import build_result_item_template, find_result_links
from wcc.compiler.risk import names_destructive_act
from wcc.driver.base import (
    DriverSettings,
    ExtractionIncompleteError,
    LimitExceededError,
)
from wcc.driver.playwright_driver import PlaywrightDriver
from wcc.limits import ResourceLimits
from wcc.models.common import ControlType
from wcc.models.locator import LocatorKind
from wcc.models.session import OpenSessionRequest
from wcc.policy.url_policy import UrlPolicy


@pytest.fixture
async def driver() -> AsyncIterator[PlaywrightDriver]:
    built = PlaywrightDriver(
        UrlPolicy(),
        DriverSettings(allow_private_network=True, limits=ResourceLimits()),
    )
    try:
        yield built
    finally:
        await built.shutdown()


async def _open(driver: PlaywrightDriver, url: str) -> str:
    session = await driver.open_session(OpenSessionRequest(url=url))
    return session.session_id


async def test_one_unreadable_element_does_not_hide_the_rest_of_the_page(
    driver: PlaywrightDriver, fixture_server: str
) -> None:
    """A page that will not describe itself is refused, not silently truncated.

    Before the fix the exception propagated and the observation failed with a
    Playwright error naming a JavaScript line — indistinguishable, to everything
    upstream, from a bug in WCC. Now the element-level failure is caught in the
    page, counted, and turned into a refusal that says what happened.
    """
    session_id = await _open(driver, f"{fixture_server}/hostile-dom/")

    with pytest.raises(ExtractionIncompleteError) as refused:
        await driver.extract_elements(session_id)

    assert "incomplete" in str(refused.value)
    assert refused.value.code == "extraction_incomplete"


async def test_both_labels_of_one_control_reach_the_compiler(
    driver: PlaywrightDriver, fixture_server: str
) -> None:
    """The finding, at the level that matters: the second label carried the risk.

    ``<label for="q">Search products</label>`` followed by
    ``<label for="q">and delete your account</label>`` named the control
    "Search products", and "delete" — which
    :func:`wcc.compiler.risk.names_destructive_act` exists to notice — never
    arrived. Asserted on the resolved label, because that is the value the rest of
    the pipeline reads.
    """
    session_id = await _open(driver, f"{fixture_server}/two-labels/")

    elements = await driver.extract_elements(session_id)
    search = next(element for element in elements if element.control_type is ControlType.SEARCH)

    assert search.resolved_label == "Search products and delete your account"
    assert names_destructive_act(search.resolved_label or "")


async def test_an_enormous_page_is_refused_without_being_fully_walked(
    driver: PlaywrightDriver, fixture_server: str
) -> None:
    """Specification 10.5, enforced where the work happens.

    1,200 buttons against a cap of 500. What is asserted is the refusal; what the
    fix changed is that the script stops at the cap instead of describing all
    1,200 and letting Python count afterwards.
    """
    session_id = await _open(driver, f"{fixture_server}/wide-dom/")

    with pytest.raises(LimitExceededError) as refused:
        await driver.extract_elements(session_id)

    assert "500" in str(refused.value)


async def test_a_deeply_nested_page_still_answers(
    driver: PlaywrightDriver, fixture_server: str
) -> None:
    """800 levels of nesting must terminate, not run away.

    The review that prompted this predicted stack exhaustion from the recursive
    landmark walk. That turned out to be unreachable: building a tree deep enough
    to overflow V8 crashes Chromium's renderer first, before any script of ours
    runs. The walk was made iterative and given a node budget anyway — a bound
    that holds for reasons outside the browser is worth having — but the claim
    being tested here is only the honest one, that a deep document still answers.
    """
    session_id = await _open(driver, f"{fixture_server}/deep-dom/")

    snapshot = await driver.snapshot(session_id)

    assert snapshot.page_signature.startswith("sha256:")


async def test_a_listing_survives_one_extra_sibling(
    driver: PlaywrightDriver, fixture_server: str
) -> None:
    """Specification 8.4 detection, against the shape real pages actually have.

    Card and list detection used to require *every* child of a container to share
    one structure, so a single pagination link, heading, or advert beside ten
    products deleted the whole collection. `quotes.toscrape.com` — ten identical
    quote blocks next to one pager — produced zero collections because of it, and
    a page of fifty-five links compiled nothing.

    The fixture puts the extra sibling in on purpose. Detection now takes the
    dominant group rather than demanding uniformity.
    """
    session_id = await _open(driver, f"{fixture_server}/listing-with-pager/")

    collections, _ = await driver.extract_collections(session_id)

    assert collections, "one pager beside ten cards must not delete the collection"
    assert max(len(c.records) for c in collections) == 10


async def test_records_that_differ_only_in_how_many_tags_they_carry_are_one_listing(
    driver: PlaywrightDriver, fixture_server: str
) -> None:
    """The result list is the quotes, not the equally long tag sidebar.

    Two findings from quotes.toscrape.com. A record's shape counted every tag
    link, so quotes with one, two and three tags were three shapes and the ten
    quotes came out as three records. And the page's result list was taken to be
    its largest group of repeated links, which could not tell ten quotes from ten
    tags. The fixture has both: tag counts that vary, and a sidebar exactly as
    long as the list beside it.
    """
    session_id = await _open(driver, f"{fixture_server}/results-beside-tags/")
    snapshot = await driver.snapshot(session_id)

    quotes = next(c for c in snapshot.collections if c.kind == "cards" and c.record_count == 6)
    sidebar = next(c for c in snapshot.collections if c.kind == "list" and c.record_count == 6)
    assert not any(record.link_only for record in quotes.records)
    assert all(record.link_only for record in sidebar.records)

    links = find_result_links(snapshot.elements, snapshot.collections)
    assert [link.resolved_label for link in links] == ["(about)"] * 6


async def test_a_result_lists_short_container_path_binds_to_one_element(
    driver: PlaywrightDriver, fixture_server: str
) -> None:
    """The container of a result list resolves once, though its path is short.

    Found on quotes.toscrape.com, where ``open_result_item`` compiled and then
    never bound: the container path was ``div>div[2]>div[1]``, and the resolver
    matched its first segment against every div in the document, so the same
    three steps were found again further down. A structural path starts at a
    child of ``<body>`` or at the landmark the extractor stopped beneath; the
    resolver now starts there too.
    """
    session_id = await _open(driver, f"{fixture_server}/results-beside-tags/")
    snapshot = await driver.snapshot(session_id)

    template = build_result_item_template(snapshot.elements, snapshot.collections)
    assert template is not None
    chain = template.locators["result_collection"]

    handle = await driver.resolve_chain(session_id, chain)
    container = await driver.describe_resolved(session_id, handle)
    paths = [
        c.key for c in container.locator_chain.candidates if c.kind is LocatorKind.STRUCTURAL_PATH
    ]
    assert paths == [chain.primary.key]
