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

import argparse
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
from benchmarks.run import DEFAULT_MODEL
from benchmarks.tokens import Counter, build_counter
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
    baseline_tokens: int | None = None
    wcc_tokens: int | None = None
    """Counted, when a counter was available. ``None`` when only characters were
    measured — never estimated from the character count, because specification
    14.3 draws the line exactly there."""

    counted_by: str | None = None
    """Which ruler produced the token numbers, in words. Anthropic's count and
    tiktoken's are both exact and they are not the same measurement, so the
    distinction travels with the figures instead of living in a commit message."""

    @property
    def ratio(self) -> float:
        if self.baseline_tokens is not None and self.wcc_tokens is not None:
            return self.wcc_tokens / self.baseline_tokens
        return self.wcc_chars / self.baseline_chars

    @property
    def unit(self) -> str:
        return self.counted_by or "characters"


@contextmanager
def fixture_server() -> Iterator[str]:
    handler = functools.partial(http.server.SimpleHTTPRequestHandler, directory=str(FIXTURES))
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://{server.server_address[0]}:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


async def measure(
    base_url: str,
    page: str,
    path: str,
    description: str,
    counter: Counter | None = None,
) -> Measurement:
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

    baseline_tokens = wcc_tokens = None
    if counter is not None:
        baseline_tokens = await counter.of_text(snapshot)
        wcc_tokens = await counter.of_text(inspection)

    return Measurement(
        page=page,
        description=description,
        baseline_chars=len(snapshot),
        wcc_chars=len(inspection),
        baseline_tokens=baseline_tokens,
        wcc_tokens=wcc_tokens,
        counted_by=counter.unit if counter is not None else None,
    )


def table(measurements: list[Measurement]) -> str:
    exact = any(item.baseline_tokens is not None for item in measurements)
    unit = "tokens" if exact else "chars"
    lines = [
        f"| Page | Size | Baseline snapshot ({unit}) | WCC inspection ({unit}) | WCC / baseline |",
        "|---|---|---:|---:|---:|",
    ]
    for item in measurements:
        left = item.baseline_tokens if exact else item.baseline_chars
        right = item.wcc_tokens if exact else item.wcc_chars
        lines.append(
            f"| `{item.page}` | {item.description} | {left:,} | {right:,} | {item.ratio:.2f}× |"
        )
    return "\n".join(lines)


async def _all(counter: Counter | None) -> list[Measurement]:
    with fixture_server() as base_url:
        return [await measure(base_url, *page, counter=counter) for page in PAGES]


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--model",
        default=DEFAULT_MODEL,
        help="which tokenizer to count with; counting is free and runs no inference",
    )
    parser.add_argument(
        "--chars-only",
        action="store_true",
        help="skip token counting even if a key is present",
    )
    arguments = parser.parse_args(argv)

    RESULTS.mkdir(parents=True, exist_ok=True)
    counter = None if arguments.chars_only else build_counter(arguments.model)
    measurements = asyncio.run(_all(counter))

    print(table(measurements))
    if counter is None:
        print("\nCharacters, counted exactly. Not tokens: specification 14.3 wants")
        print("those from provider metadata, and four-per-token is an approximation.")
    else:
        print(f"\nUnit: {counter.unit}. Nothing was billed and no inference ran.")
    print("\nThis is one observation. How many a task takes is not measured here;")
    print("that needs `benchmarks.run`, an agent, and a budget.")

    (RESULTS / "payload-sizes.json").write_text(
        json.dumps(
            [
                {
                    "page": item.page,
                    "description": item.description,
                    "baseline_snapshot_chars": item.baseline_chars,
                    "wcc_inspection_chars": item.wcc_chars,
                    "baseline_snapshot_tokens": item.baseline_tokens,
                    "wcc_inspection_tokens": item.wcc_tokens,
                    "unit_of_ratio": item.unit,
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
