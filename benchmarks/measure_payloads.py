"""Measure what one observation costs, without calling a model.

``uv run python -m benchmarks.measure_payloads``

This is not the benchmark. :mod:`benchmarks.run` is the benchmark, and it needs an
API key and a budget because specification 14.3 requires token counts to come from
provider usage metadata rather than from an estimate. This module answers a
smaller question that happens to need neither: **how large is a single
observation, on pages of different sizes?**

The distinction matters and is kept visible in every number this prints. What is
measured here is the payload a tool returns — exact, in characters. What is *not*
measured is how many times an agent asks for one, which is where WCC's saving is
supposed to come from and which cannot be known without running an agent.

So this is evidence for one half of the claim. It is published as that half.
"""

from __future__ import annotations

import asyncio
import functools
import http.server
import json
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from dataclasses import dataclass
from pathlib import Path

from playwright.async_api import async_playwright

from benchmarks.arms import BaselineArm
from benchmarks.wcc_arm import WccArm
from wcc.driver.base import DriverSettings
from wcc.driver.playwright_driver import PlaywrightDriver
from wcc.enrich.enricher import Enricher
from wcc.policy.url_policy import UrlPolicy
from wcc.runtime.coordinator import Coordinator
from wcc.store.store import Store

REPO_ROOT = Path(__file__).resolve().parent.parent
FIXTURES = REPO_ROOT / "fixtures"
RESULTS = REPO_ROOT / "benchmarks" / "results"

#: The pages, smallest first. The pairing is the finding: a page's *controls*
#: decide what WCC returns, while everything on it decides what a snapshot
#: returns, so the two costs diverge as a page grows.
PAGES = (
    ("search-page", "/search-page/", "6 controls"),
    ("filter-page", "/filter-page/", "5 controls"),
    ("product-list", "/product-list/", "3 results"),
    ("large-catalog", "/large-catalog/", "150 products"),
)


@dataclass(frozen=True)
class Measurement:
    page: str
    description: str
    baseline_chars: int
    wcc_chars: int

    @property
    def ratio(self) -> float:
        return self.wcc_chars / self.baseline_chars


@contextmanager
def fixture_server() -> Iterator[str]:
    handler = functools.partial(
        http.server.SimpleHTTPRequestHandler, directory=str(FIXTURES)
    )
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://{server.server_address[0]}:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


async def measure(base_url: str, page: str, path: str, description: str) -> Measurement:
    """One `browser_snapshot` against one `inspect_page`, on the same page."""
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        try:
            tab = await (await browser.new_context()).new_page()
            arm = BaselineArm(page=tab)
            await arm.call("browser_navigate", {"url": base_url + path})
            snapshot = await arm.call("browser_snapshot", {})
        finally:
            await browser.close()

    driver = PlaywrightDriver(UrlPolicy(), DriverSettings(allow_private_network=True))
    store = Store(RESULTS / f".measure-{page}.sqlite3")
    try:
        wcc = WccArm(coordinator=Coordinator(driver, store, enricher=Enricher()), driver=driver)
        session = json.loads(await wcc.call("open_session", {"url": base_url + path}))
        inspection = await wcc.call("inspect_page", {"session_id": session["session_id"]})
    finally:
        store.close()
        await driver.shutdown()

    return Measurement(
        page=page,
        description=description,
        baseline_chars=len(snapshot),
        wcc_chars=len(inspection),
    )


def table(measurements: list[Measurement]) -> str:
    lines = [
        "| Page | Size | Baseline snapshot | WCC inspection | WCC / baseline |",
        "|---|---|---:|---:|---:|",
    ]
    for item in measurements:
        lines.append(
            f"| `{item.page}` | {item.description} | {item.baseline_chars:,} | "
            f"{item.wcc_chars:,} | {item.ratio:.2f}× |"
        )
    return "\n".join(lines)


async def _all() -> list[Measurement]:
    with fixture_server() as base_url:
        return [await measure(base_url, *page) for page in PAGES]


def main() -> int:
    RESULTS.mkdir(parents=True, exist_ok=True)
    measurements = asyncio.run(_all())

    print(table(measurements))
    print("\nCharacters, measured. Not tokens, and not a token-reduction figure:")
    print("specification 14.3 requires those to come from provider usage metadata,")
    print("which needs `benchmarks.run` and an API key.")

    (RESULTS / "payload-sizes.json").write_text(
        json.dumps(
            [
                {
                    "page": item.page,
                    "description": item.description,
                    "baseline_snapshot_chars": item.baseline_chars,
                    "wcc_inspection_chars": item.wcc_chars,
                    "ratio": round(item.ratio, 4),
                }
                for item in measurements
            ],
            indent=2,
        )
    )
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
