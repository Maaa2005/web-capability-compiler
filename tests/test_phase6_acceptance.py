"""Phase 6 acceptance, against the real pipeline rather than the enricher alone.

The three criteria are about the *system*, not about the module, and a test that
only drove :class:`~wcc.enrich.enricher.Enricher` would satisfy none of them. So
these run a live Chromium against the fixture site through the coordinator, once
with no provider and once with a provider that misbehaves, and assert that the
capabilities an agent ends up with are the same either way.

Only the last test uses a provider that succeeds. That ordering is intentional:
the claim being defended is that enrichment is optional, and the negative cases
are where that claim can fail.
"""

from __future__ import annotations

from collections.abc import AsyncIterator
from pathlib import Path

import pytest

from wcc.driver.base import DriverSettings
from wcc.driver.playwright_driver import PlaywrightDriver
from wcc.enrich.enricher import Enricher
from wcc.enrich.models import CapabilityEnrichment, PageEnrichment
from wcc.enrich.provider import EnrichmentUnavailable
from wcc.models.capability import CapabilityTemplate
from wcc.models.session import OpenSessionRequest
from wcc.policy.url_policy import UrlPolicy
from wcc.runtime.coordinator import Coordinator
from wcc.store.store import Store


class ScriptedProvider:
    """Answers with a fixed response, or raises. No network, by construction."""

    def __init__(self, response: PageEnrichment | Exception) -> None:
        self._response = response
        self.calls = 0

    async def enrich(self, request: object) -> PageEnrichment:
        self.calls += 1
        if isinstance(self._response, Exception):
            raise self._response
        return self._response


@pytest.fixture
async def driver() -> AsyncIterator[PlaywrightDriver]:
    built = PlaywrightDriver(UrlPolicy(), DriverSettings(allow_private_network=True))
    try:
        yield built
    finally:
        await built.shutdown()


async def _capabilities(
    driver: PlaywrightDriver, store: Store, url: str, enricher: Enricher | None
) -> dict[str, str]:
    """Inspect ``url`` and return the offered ``name -> description`` mapping."""
    coordinator = Coordinator(driver, store, enricher=enricher)
    session = await coordinator.open_session(OpenSessionRequest(url=url))
    inspection = await coordinator.inspect_page(session.session_id)
    try:
        return {
            inspection.templates[binding.template_id].name: inspection.templates[
                binding.template_id
            ].description
            for binding in inspection.capabilities
        }
    finally:
        await coordinator.close_session(session.session_id)


async def test_the_full_system_works_with_the_llm_disabled(
    driver: PlaywrightDriver, tmp_path: Path, fixture_server: str
) -> None:
    """Acceptance 1, and the reason the other two matter."""
    with Store(tmp_path / "wcc.db") as store:
        offered = await _capabilities(driver, store, f"{fixture_server}/search-page/", None)

    assert "submit_search_form" in offered


async def test_invalid_model_output_leaves_the_deterministic_output_usable(
    driver: PlaywrightDriver, tmp_path: Path, fixture_server: str
) -> None:
    """Acceptance 2: invalid output is rejected and the compiler's answer stands.

    Both failure modes in one test because the guarantee is the same for both: a
    provider that cannot be reached, and one that answers about a page it was not
    asked about.
    """
    url = f"{fixture_server}/search-page/"
    with Store(tmp_path / "unreachable.db") as store:
        unreachable = await _capabilities(
            driver, store, url, Enricher(ScriptedProvider(EnrichmentUnavailable("no route")))
        )
    with Store(tmp_path / "nonsense.db") as store:
        nonsense_provider = ScriptedProvider(
            PageEnrichment(
                page_type="search",
                capabilities=[
                    CapabilityEnrichment(ref="c99", name="search_products", subject="products")
                ],
            )
        )
        nonsense = await _capabilities(driver, store, url, Enricher(nonsense_provider))
    with Store(tmp_path / "plain.db") as store:
        plain = await _capabilities(driver, store, url, None)

    assert unreachable == nonsense == plain


async def test_enrichment_cannot_alter_locators_actions_or_lower_risk_end_to_end(
    driver: PlaywrightDriver, tmp_path: Path, fixture_server: str
) -> None:
    """Acceptance 3, compared against the same page compiled without a provider.

    Renaming is allowed to succeed here — that is the feature — and everything
    else is asserted to be byte-identical to the run with no provider at all. The
    hostile cases (a lowered risk hint, a selector in a description, a parameter
    that does not exist) are unit-tested in ``test_enrichment``, because every
    capability the fixture compiles is already low risk and a page cannot be made
    to offer a high-risk one to bind against.
    """
    url = f"{fixture_server}/search-page/"

    with Store(tmp_path / "plain.db") as store:
        coordinator = Coordinator(driver, store)
        session = await coordinator.open_session(OpenSessionRequest(url=url))
        baseline = await coordinator.inspect_page(session.session_id)
        before = {template.name: template for template in baseline.templates.values()}
        await coordinator.close_session(session.session_id)

    response = PageEnrichment(
        page_type="product search",
        capabilities=[
            CapabilityEnrichment(
                ref=f"c{index}",
                name=f"search_products_{index}",
                subject="products",
                risk_hint=None,
            )
            for index in range(1, len(before) + 1)
        ],
    )

    with Store(tmp_path / "enriched.db") as store:
        coordinator = Coordinator(driver, store, enricher=Enricher(ScriptedProvider(response)))
        session = await coordinator.open_session(OpenSessionRequest(url=url))
        inspection = await coordinator.inspect_page(session.session_id)
        after = list(inspection.templates.values())
        await coordinator.close_session(session.session_id)

    assert [template.name for template in after] != list(before)

    # Matched by locator map rather than by name, because the name is the one
    # thing that legitimately changed. Two capabilities on one page never share a
    # locator map: they are different controls.
    by_locators = {_key(template): template for template in before.values()}
    for template in after:
        original = by_locators[_key(template)]
        assert template.execution_plan_template == original.execution_plan_template
        assert template.risk == original.risk
        assert template.category == original.category


def _key(template: CapabilityTemplate) -> str:
    return template.model_dump_json(include={"target_locators"})
