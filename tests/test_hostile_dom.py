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

from wcc.compiler.risk import names_destructive_act
from wcc.driver.base import (
    DriverSettings,
    ExtractionIncompleteError,
    LimitExceededError,
)
from wcc.driver.playwright_driver import PlaywrightDriver
from wcc.limits import ResourceLimits
from wcc.models.common import ControlType
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
