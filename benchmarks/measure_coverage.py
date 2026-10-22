"""Does it find anything, and does it refuse the right things?

``uv run python -m benchmarks.measure_coverage``

The payload measurements answer "how much does an observation cost". This answers
the two questions that come before it, and that were never asked of a real page:

**Coverage.** On a page nobody here wrote, does the compiler find a capability at
all? A cheaper observation of nothing is worth nothing.

**Refusal.** State-changing forms — add to basket, subscribe, post — must be
reported and not compiled (specification 8.3, 10.1). A run that found capabilities
everywhere by compiling those would be worse than one that found none.

Neither needs a model, a key, or a budget. Both are the parts of the claim that
were still resting on fixtures the author wrote.

**A zero is not automatically a failure.** A page with one paragraph and one link
*should* produce nothing. The report separates "found nothing and there was
nothing" from "found nothing and there was something", because collapsing them
would turn an honest limitation into a bad number and a real defect into an
excuse.

**Manners.** ``robots.txt`` is fetched and honoured for every host before the page
is opened — a disallowed page is skipped and recorded as skipped, not quietly
dropped. One page per site, one visit, read-only, no crawling, no logins. A pause
between sites.
"""

from __future__ import annotations

import argparse
import asyncio
import json
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from tempfile import TemporaryDirectory
from urllib.error import URLError
from urllib.parse import urlsplit
from urllib.robotparser import RobotFileParser

from wcc.compiler.navigation import find_result_links
from wcc.driver.base import DriverError, DriverSettings
from wcc.driver.playwright_driver import PlaywrightDriver
from wcc.enrich.enricher import Enricher
from wcc.models.capability import RiskLevel
from wcc.models.session import OpenSessionRequest
from wcc.policy.url_policy import UrlPolicy
from wcc.runtime.coordinator import Coordinator
from wcc.store.store import Store

RESULTS = Path(__file__).resolve().parent / "results"

#: Identifies the crawler to ``robots.txt`` and to the sites themselves. A tool
#: that reads robots.txt under one name and requests under another is not really
#: honouring it.
USER_AGENT = "wcc-benchmark (+https://github.com/Maaa2005/web-capability-compiler)"

#: Seconds between sites. Not a rate limit so much as a statement of intent: this
#: visits two dozen pages once each, and there is no reason for it to be fast.
PAUSE_SECONDS = 2.0

#: Public pages, one per host. Weighted towards documentation, open-source project
#: sites, and pages that exist to be scraped, because those are the ones where a
#: single automated visit is unambiguously welcome. Commercial retail is left out:
#: bot protection makes the result meaningless and the terms make it rude.
PAGES: tuple[tuple[str, str], ...] = (
    ("https://example.com/", "one paragraph, one link"),
    ("https://quotes.toscrape.com/", "a quote listing, built for scraping"),
    ("https://books.toscrape.com/", "a product grid, built for scraping"),
    ("https://webscraper.io/test-sites/e-commerce/allinone", "a test e-commerce page"),
    ("https://www.python.org/", "project home with search"),
    ("https://docs.python.org/3/", "documentation index with search"),
    ("https://pypi.org/", "a package index with search"),
    ("https://crates.io/", "a package index, client-rendered"),
    ("https://www.rust-lang.org/", "project home"),
    ("https://go.dev/", "project home with search"),
    ("https://www.djangoproject.com/", "project home with search"),
    ("https://flask.palletsprojects.com/en/stable/", "documentation with search"),
    ("https://news.ycombinator.com/", "a link listing, table layout"),
    ("https://lobste.rs/", "a link listing"),
    ("https://developer.mozilla.org/en-US/docs/Web/HTML", "a long documentation page"),
    ("https://en.wikipedia.org/wiki/Web_scraping", "an article with a search form"),
    ("https://www.gnu.org/", "an old-fashioned static site"),
    ("https://www.debian.org/", "an old-fashioned static site with search"),
    ("https://www.w3.org/", "a standards body home page"),
    ("https://arxiv.org/", "a preprint archive with search"),
    ("https://www.openstreetmap.org/", "a map application"),
    ("https://huggingface.co/models", "a model listing, client-rendered"),
)


@dataclass
class SiteCoverage:
    url: str
    host: str
    note: str
    observed_at: str = ""
    allowed: bool = True
    capabilities: list[str] = field(default_factory=list)
    categories: list[str] = field(default_factory=list)
    refused_codes: list[str] = field(default_factory=list)
    unbound: list[str] = field(default_factory=list)
    """Compiled templates the binder refused, as ``name: reason``. Without these a
    page whose only compiled capability failed to bind read as "nothing found"."""

    refused_above_low: int = 0
    """Refusals the compiler classified above ``low`` risk. These are the ones
    whose refusal is the point: a state-changing form that was reported rather
    than compiled is the safety model working."""

    result_list: list[str] = field(default_factory=list)
    """The labels of the links taken as the page's result list, in order. A
    capability name says that a result list was compiled, not which one: go.dev
    and www.python.org compiled ``open_result_item`` over the wrong list, and a
    run that recorded names alone could not show it."""

    collections: int = 0
    warnings: list[str] = field(default_factory=list)
    """What the snapshot said it left out, such as collections over the limit."""

    interactive_elements: int = 0
    forms_seen: int = 0
    error: str | None = None

    @property
    def found_something(self) -> bool:
        return bool(self.capabilities)

    @property
    def had_something_to_find(self) -> bool:
        """Whether the page offered anything a compiler could reasonably want.

        A page with no forms and almost no links had nothing to find, and scoring
        it as a miss would flatter the denominator. This is a coarse proxy and it
        is stated as one.
        """
        return self.forms_seen > 0 or self.interactive_elements >= 20


def robots_allows(url: str) -> bool:
    """Whether ``robots.txt`` permits this agent to fetch this path.

    A fetch failure is treated as permission, which is the conventional reading:
    a site with no robots.txt has not restricted anything. A site that *has* one
    and disallows the path is skipped and said to be skipped.
    """
    parts = urlsplit(url)
    parser = RobotFileParser()
    parser.set_url(f"{parts.scheme}://{parts.netloc}/robots.txt")
    try:
        parser.read()
    except (URLError, OSError):
        return True
    return parser.can_fetch(USER_AGENT, url)


async def visit(url: str, note: str) -> SiteCoverage:
    host = urlsplit(url).netloc
    result = SiteCoverage(
        url=url,
        host=host,
        note=note,
        observed_at=datetime.now(UTC).replace(microsecond=0).isoformat(),
    )

    if not robots_allows(url):
        result.allowed = False
        result.error = "robots.txt disallows this path"
        return result

    driver = PlaywrightDriver(UrlPolicy(), DriverSettings())
    # A fresh database per visit, so every page is measured on the cold path.
    # Re-using one silently changed the answer: the second run of a page binds a
    # stored template, and a warm inspection carries no compilation report — so
    # `books.toscrape.com` reported twenty refused forms on the first run and
    # none on the second. Coverage is a question about compiling, and it has to
    # be asked of a compiler that actually ran.
    store_directory = TemporaryDirectory(prefix="wcc-coverage-")
    store = Store(Path(store_directory.name) / "wcc.sqlite3")
    try:
        coordinator = Coordinator(driver, store, enricher=Enricher())
        session = await coordinator.open_session(OpenSessionRequest(url=url))
        elements = await driver.extract_elements(session.session_id)
        result.interactive_elements = len(elements)
        result.forms_seen = len({e.form_key for e in elements if e.form_key})

        inspection = await coordinator.inspect_page(session.session_id)
        snapshot = inspection.snapshot
        result.collections = len(snapshot.collections)
        result.warnings = list(snapshot.warnings)
        result.result_list = [
            link.resolved_label or link.accessible_name or ""
            for link in find_result_links(snapshot.elements, snapshot.collections)
        ]
        for binding in inspection.capabilities:
            template = inspection.templates[binding.template_id]
            result.capabilities.append(template.name)
            result.categories.append(str(template.category))

        result.unbound = [f"{template.name}: {template.reason}" for template in inspection.unbound]
        if inspection.report is not None:
            for candidate in inspection.report.rejected:
                result.refused_codes.append(candidate.code)
                if candidate.risk is not None and candidate.risk.level is not RiskLevel.LOW:
                    result.refused_above_low += 1
    except DriverError as refused:
        result.error = f"{refused.code}: {refused.message}"
    except Exception as failed:
        result.error = f"{type(failed).__name__}: {failed}"
    finally:
        store.close()
        store_directory.cleanup()
        await driver.shutdown()
    return result


def report(sites: list[SiteCoverage]) -> str:
    lines = [
        "| Site | Page | Elements | Forms | Capabilities | Refused (above low) |",
        "|---|---|---:|---:|---|---:|",
    ]
    for site in sites:
        if not site.allowed:
            lines.append(f"| `{site.host}` | {site.note} | — | — | skipped: robots.txt | — |")
            continue
        if site.error:
            lines.append(f"| `{site.host}` | {site.note} | — | — | error: {site.error[:60]} | — |")
            continue
        found = ", ".join(sorted(set(site.capabilities))) or "—"
        if site.unbound:
            names = sorted({entry.split(": ", 1)[0] for entry in site.unbound})
            found += f" (not bound: {', '.join(names)})"
        lines.append(
            f"| `{site.host}` | {site.note} | {site.interactive_elements} | "
            f"{site.forms_seen} | {found} | {len(site.refused_codes)} "
            f"({site.refused_above_low}) |"
        )
    lines += ["", "Result lists, as taken (first three of each):", ""]
    for site in sites:
        if site.result_list:
            shown = ", ".join(f"`{label[:40]}`" for label in site.result_list[:3])
            lines.append(f"- `{site.host}` ({len(site.result_list)}): {shown}")
    return "\n".join(lines)


def summarize(sites: list[SiteCoverage]) -> str:
    reached = [s for s in sites if s.allowed and not s.error]
    with_something = [s for s in reached if s.had_something_to_find]
    found = [s for s in with_something if s.found_something]
    empty_and_correct = [
        s for s in reached if not s.found_something and not s.had_something_to_find
    ]
    refused_risky = sum(s.refused_above_low for s in reached)

    return "\n".join(
        [
            f"pages visited:                    {len(sites)}",
            f"  skipped by robots.txt:          {sum(1 for s in sites if not s.allowed)}",
            f"  failed to observe:              {sum(1 for s in sites if s.error and s.allowed)}",
            f"  observed:                       {len(reached)}",
            "",
            "of the observed pages:",
            f"  had something to find:          {len(with_something)}",
            f"  ...and a capability was found:  {len(found)}"
            + (f"  ({100 * len(found) / len(with_something):.0f}%)" if with_something else ""),
            f"  had nothing to find, found none:{len(empty_and_correct)}",
            "",
            f"candidates refused above low risk: {refused_risky}",
            "  (state-changing forms reported rather than compiled — the safety",
            "   model working, not a shortfall in coverage)",
        ]
    )


async def _all(pages: tuple[tuple[str, str], ...]) -> list[SiteCoverage]:
    sites = []
    for url, note in pages:
        print(f"  {url}")
        sites.append(await visit(url, note))
        await asyncio.sleep(PAUSE_SECONDS)
    return sites


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--limit", type=int, default=0, help="visit only the first N pages")
    arguments = parser.parse_args(argv)

    pages = PAGES[: arguments.limit] if arguments.limit else PAGES
    RESULTS.mkdir(parents=True, exist_ok=True)

    print(f"Visiting {len(pages)} public pages, once each, read-only:")
    sites = asyncio.run(_all(pages))

    print("\n" + report(sites))
    print("\n" + summarize(sites))

    stamp = datetime.now(UTC).strftime("%Y%m%d")
    path = RESULTS / f"coverage-{stamp}.json"
    path.write_text(json.dumps([asdict(site) for site in sites], indent=2))
    print(f"\nraw: {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
