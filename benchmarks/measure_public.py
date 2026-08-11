"""The same measurement, against pages nobody here wrote (specification 14.4).

``uv run python -m benchmarks.measure_public``

Every fixture in this repository was written by the person claiming the result,
which is the first thing a sceptical reader will notice. This module answers that
by pointing the identical measurement at public pages, and it is deliberately
*not* promoted to the benchmark of record: specification 14.4 makes local fixtures
the reproducible arm and public sites "supplementary, dated, and accompanied by
limitations", because a site can change between two runs and turn a measurement
into an anecdote. So every row carries the URL and the date it was observed.

**A refusal is a result.** Real pages are large, and specification 10.5 caps an
observation at 500 elements — so WCC will decline some of these outright while a
low-level snapshot happily returns something. That is a genuine limitation of this
project and it is reported in the same table as the wins, because a comparison
that silently dropped the pages where one arm failed would be measuring the
selection rather than the arms.

**Manners.** One page per site, one visit, read-only, no crawling, no logged-in
pages, and nothing behind a paywall. These are single visits of the kind any
browser makes.
"""

from __future__ import annotations

import argparse
import asyncio
import json
from dataclasses import asdict, dataclass
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import urlsplit

from playwright.async_api import async_playwright

from benchmarks.arms import BaselineArm
from benchmarks.run import DEFAULT_MODEL
from benchmarks.tokens import Counter, build_counter
from benchmarks.wcc_arm import WccArm
from wcc.driver.base import DriverError, DriverSettings
from wcc.driver.playwright_driver import PlaywrightDriver
from wcc.enrich.enricher import Enricher
from wcc.policy.url_policy import UrlPolicy
from wcc.runtime.coordinator import Coordinator
from wcc.store.store import Store

RESULTS = Path(__file__).resolve().parent / "results"

#: Chosen for range rather than for flattery: a page that is nearly empty, several
#: ordinary content pages, a search-results page, and a couple of large listings.
#: Two of them exist specifically so that people have something legitimate to
#: practise scraping against, which makes them the politest possible entries in a
#: list like this; the rest are ordinary public pages visited once each.
PAGES: tuple[tuple[str, str], ...] = (
    ("https://example.com/", "the smallest real page there is"),
    ("https://quotes.toscrape.com/", "a listing built for scraping practice"),
    ("https://books.toscrape.com/", "a 20-item product grid, built for practice"),
    ("https://www.python.org/", "a large marketing and navigation page"),
    ("https://news.ycombinator.com/", "30 links, minimal markup"),
    ("https://developer.mozilla.org/en-US/docs/Web/HTML", "a long documentation page"),
)


@dataclass
class PublicMeasurement:
    url: str
    host: str
    note: str
    observed_at: str
    baseline: int | None = None
    wcc: int | None = None
    unit: str = "characters"
    baseline_error: str | None = None
    wcc_error: str | None = None

    @property
    def ratio(self) -> float | None:
        if self.baseline and self.wcc:
            return self.wcc / self.baseline
        return None


async def _baseline(url: str) -> tuple[str | None, str | None]:
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        try:
            page = await (await browser.new_context()).new_page()
            arm = BaselineArm(page=page)
            await arm.call("browser_navigate", {"url": url})
            return await arm.call("browser_snapshot", {}), None
        except Exception as failed:
            return None, f"{type(failed).__name__}: {failed}"
        finally:
            await browser.close()


async def _wcc(url: str, store_path: Path) -> tuple[str | None, str | None]:
    # ``allow_private_network`` stays off, which is the point of it being a
    # developer flag: these are public addresses and the policy of specification
    # 10.2 applies to them unchanged.
    driver = PlaywrightDriver(UrlPolicy(), DriverSettings())
    store = Store(store_path)
    try:
        arm = WccArm(coordinator=Coordinator(driver, store, enricher=Enricher()), driver=driver)
        session = json.loads(await arm.call("open_session", {"url": url}))
        return await arm.call("inspect_page", {"session_id": session["session_id"]}), None
    except DriverError as refused:
        # The interesting failure. A refusal names its reason and that reason is
        # the finding: "this page is larger than specification 10.5 allows" is a
        # limitation of WCC, not an absence of data.
        return None, f"{refused.code}: {refused.message}"
    except Exception as failed:
        return None, f"{type(failed).__name__}: {failed}"
    finally:
        store.close()
        await driver.shutdown()


async def measure_one(url: str, note: str, counter: Counter | None) -> PublicMeasurement:
    observed = datetime.now(UTC).replace(microsecond=0).isoformat()
    host = urlsplit(url).netloc
    result = PublicMeasurement(url=url, host=host, note=note, observed_at=observed)

    snapshot, result.baseline_error = await _baseline(url)
    inspection, result.wcc_error = await _wcc(url, RESULTS / f".public-{host}.sqlite3")

    if counter is None:
        result.baseline = len(snapshot) if snapshot else None
        result.wcc = len(inspection) if inspection else None
    else:
        result.unit = counter.unit
        result.baseline = await counter.of_text(snapshot) if snapshot else None
        result.wcc = await counter.of_text(inspection) if inspection else None
    return result


def table(measurements: list[PublicMeasurement]) -> str:
    lines = [
        "| Site | Page | Baseline | WCC | WCC / baseline |",
        "|---|---|---:|---:|---|",
    ]
    for item in measurements:
        wcc = f"{item.wcc:,}" if item.wcc else f"refused — {item.wcc_error}"
        baseline = f"{item.baseline:,}" if item.baseline else f"failed — {item.baseline_error}"
        ratio = f"{item.ratio:.2f}×" if item.ratio else "—"
        lines.append(f"| `{item.host}` | {item.note} | {baseline} | {wcc} | {ratio} |")
    return "\n".join(lines)


async def _all(
    counter: Counter | None, pages: tuple[tuple[str, str], ...]
) -> list[PublicMeasurement]:
    measurements = []
    for url, note in pages:
        print(f"  {url}")
        measurements.append(await measure_one(url, note, counter))
    return measurements


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--chars-only", action="store_true")
    arguments = parser.parse_args(argv)

    RESULTS.mkdir(parents=True, exist_ok=True)
    counter = None if arguments.chars_only else build_counter(arguments.model)

    print(f"Visiting {len(PAGES)} public pages, once each, read-only:")
    measurements = asyncio.run(_all(counter, PAGES))

    print("\n" + table(measurements))
    print(f"\nUnit: {counter.unit if counter else 'characters'}.")
    print("Dated observations of pages this project does not control; see")
    print("specification 14.4 on why these supplement the fixtures rather than")
    print("replacing them.")

    stamp = datetime.now(UTC).strftime("%Y%m%d")
    path = RESULTS / f"public-sites-{stamp}.json"
    path.write_text(json.dumps([asdict(item) for item in measurements], indent=2))
    print(f"\nraw: {path}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
