"""Visit the held-out candidates: ``robots.txt``, a capture, then WCC.

Each candidate is visited once, in the order ``sample.json`` gives, until each
stratum has the plan's number of complete pages. The run can be stopped and
resumed: everything is written after each candidate.

Two files, kept apart on purpose:

- ``captures/index.json`` (with the screenshots and text beside it) is what the
  annotator reads.
- ``wcc/results.json`` is what WCC said. The labelling step never opens it, and
  ``PLAN.md`` asks the annotator not to either.

Screenshots and page text belong to the sites and stay out of git
(``.gitignore``); the index records each file's SHA-256 so a later reader can
tell whether a capture was replaced.
"""

from __future__ import annotations

import asyncio
import contextlib
import hashlib
import json
import platform
import subprocess
import sys
from collections.abc import Awaitable, Callable
from dataclasses import asdict, dataclass, field
from datetime import UTC, datetime
from importlib.metadata import version
from pathlib import Path
from tempfile import TemporaryDirectory
from typing import Any

from playwright.async_api import Error as PlaywrightError
from playwright.async_api import Page, Route, async_playwright

from benchmarks.heldout import plan
from benchmarks.heldout.sample import SAMPLE_PATH, Candidate, load_sample
from benchmarks.measure_coverage import PAUSE_SECONDS, robots_refusal
from wcc.compiler.navigation import find_result_links
from wcc.driver.base import DriverError, DriverSettings, NavigationBlockedError
from wcc.driver.playwright_driver import PlaywrightDriver
from wcc.enrich.enricher import Enricher
from wcc.limits import ResourceLimits
from wcc.models.capability import RiskLevel
from wcc.models.session import OpenSessionRequest
from wcc.policy.url_policy import UrlPolicy
from wcc.runtime.coordinator import Coordinator
from wcc.store.store import Store

REPO_ROOT = Path(__file__).resolve().parents[2]

#: The capture is cut here so one endless page cannot fill the disk.
MAX_SCREENSHOT_HEIGHT = 8_000
MAX_TEXT_CHARS = 50_000
NETWORK_QUIET_MS = 3_000


def key_of(candidate: Candidate) -> str:
    return f"{candidate.stratum}/{candidate.position:03d}"


@dataclass
class CaptureRecord:
    key: str
    stratum: str
    position: int
    origin: str
    url: str
    status: str = "pending"
    """``captured``, ``robots`` (refused by robots.txt) or ``failed``."""

    reason: str = ""
    final_url: str = ""
    http_status: int | None = None
    title: str = ""
    screenshot: str = ""
    screenshot_sha256: str = ""
    text: str = ""
    text_sha256: str = ""
    captured_at: str = ""


@dataclass
class WccRecord:
    key: str
    url: str
    observed_at: str = ""
    error: str | None = None
    final_url: str = ""
    interactive_elements: int = 0
    forms_seen: int = 0
    offered: list[dict[str, Any]] = field(default_factory=list)
    """Each bound capability: ``name``, ``category``, ``text_parameters`` (free-text
    arguments) and the visible ``labels`` of the elements its evidence names,
    which the unsafe-exposure check reads."""

    result_list: list[str] = field(default_factory=list)
    unbound: list[str] = field(default_factory=list)
    refused_codes: list[str] = field(default_factory=list)
    refused_above_low: int = 0

    @property
    def complete(self) -> bool:
        return self.error is None


def _now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat()


def _sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


async def capture(
    url: str,
    shot: Path,
    text: Path,
    record: CaptureRecord,
    *,
    allow_private_network: bool = False,
) -> None:
    """Open ``url`` in plain Chromium and keep what a person would see.

    The same URL policy as WCC's judges every request, so the capture cannot
    reach an address WCC itself would refuse. ``allow_private_network`` exists
    for the loopback fixtures in the tests, as it does for WCC.
    """
    policy = UrlPolicy()
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch(headless=True)
        try:
            context = await browser.new_context(
                viewport={"width": 1280, "height": 900},
                accept_downloads=False,
                service_workers="block",
            )

            async def guard(route: Route) -> None:
                try:
                    await asyncio.to_thread(
                        policy.check,
                        route.request.url,
                        allow_private_network=allow_private_network,
                    )
                except NavigationBlockedError:
                    await route.abort("blockedbyclient")
                    return
                await route.continue_()

            await context.route("**/*", guard)
            page = await context.new_page()

            def refuse_popup(popup: Page) -> None:
                # Registered after the capture's own page exists: the context's
                # "page" event fires for that one too.
                if popup is not page:
                    asyncio.ensure_future(popup.close())

            context.on("page", refuse_popup)
            response = await page.goto(url, wait_until="load", timeout=20_000)
            # Best effort: a page that never goes quiet is captured as it is.
            with contextlib.suppress(PlaywrightError):
                await page.wait_for_load_state("networkidle", timeout=NETWORK_QUIET_MS)
            record.http_status = response.status if response is not None else None
            record.final_url = page.url
            record.title = await page.title()
            height = await page.evaluate("document.documentElement.scrollHeight")
            clip_height = max(1, min(int(height or 900), MAX_SCREENSHOT_HEIGHT))
            await page.screenshot(
                path=str(shot),
                full_page=True,
                clip={"x": 0, "y": 0, "width": 1280, "height": clip_height},
            )
            visible = await page.inner_text("body")
            text.write_text(visible[:MAX_TEXT_CHARS])
        finally:
            await browser.close()


async def wcc_visit(url: str, record: WccRecord, *, allow_private_network: bool = False) -> None:
    """What WCC offers on ``url``, compiled cold, as ``measure_coverage.visit`` does."""
    driver = PlaywrightDriver(
        UrlPolicy(),
        DriverSettings(allow_private_network=allow_private_network, limits=ResourceLimits()),
    )
    store_directory = TemporaryDirectory(prefix="wcc-heldout-")
    store = Store(Path(store_directory.name) / "wcc.sqlite3")
    try:
        coordinator = Coordinator(driver, store, enricher=Enricher())
        session = await coordinator.open_session(OpenSessionRequest(url=url))
        elements = await driver.extract_elements(session.session_id)
        record.interactive_elements = len(elements)
        record.forms_seen = len({e.form_key for e in elements if e.form_key})

        inspection = await coordinator.inspect_page(session.session_id)
        snapshot = inspection.snapshot
        record.final_url = snapshot.url
        by_id = {element.element_id: element for element in snapshot.elements}
        record.result_list = [
            link.resolved_label or link.accessible_name or ""
            for link in find_result_links(snapshot.elements, snapshot.collections)
        ]
        for binding in inspection.capabilities:
            template = inspection.templates[binding.template_id]
            labels = []
            for evidence in template.evidence:
                element = by_id.get(evidence.element_id or "")
                if element is not None:
                    label = element.resolved_label or element.accessible_name
                    if label and label not in labels:
                        labels.append(label)
            record.offered.append(
                {
                    "name": template.name,
                    "category": str(template.category),
                    "risk": str(template.risk.level),
                    "text_parameters": free_text_parameters(template.parameters),
                    "labels": labels,
                }
            )
        record.unbound = [f"{t.name}: {t.reason}" for t in inspection.unbound]
        if inspection.report is not None:
            for candidate in inspection.report.rejected:
                record.refused_codes.append(candidate.code)
                if candidate.risk is not None and candidate.risk.level is not RiskLevel.LOW:
                    record.refused_above_low += 1
    except DriverError as refused:
        record.error = f"{refused.code}: {refused.message}"
    except Exception as failed:
        record.error = f"{type(failed).__name__}: {failed}"
    finally:
        store.close()
        store_directory.cleanup()
        await driver.shutdown()


def free_text_parameters(schema: dict[str, object]) -> int:
    """How many arguments take free text: a string with no fixed list of values."""
    properties = schema.get("properties")
    if not isinstance(properties, dict):
        return 0
    return sum(
        1
        for spec in properties.values()
        if isinstance(spec, dict) and spec.get("type") == "string" and "enum" not in spec
    )


# -- the run's files ---------------------------------------------------------


@dataclass
class RunFiles:
    out: Path

    @property
    def captures(self) -> Path:
        return self.out / "captures"

    @property
    def index(self) -> Path:
        return self.captures / "index.json"

    @property
    def wcc(self) -> Path:
        return self.out / "wcc" / "results.json"

    @property
    def labels(self) -> Path:
        return self.out / "labels.json"

    @property
    def meta(self) -> Path:
        return self.out / "run.json"


def load_index(files: RunFiles) -> dict[str, CaptureRecord]:
    if not files.index.exists():
        return {}
    return {r["key"]: CaptureRecord(**r) for r in json.loads(files.index.read_text())}


def load_wcc(files: RunFiles) -> dict[str, WccRecord]:
    if not files.wcc.exists():
        return {}
    return {r["key"]: WccRecord(**r) for r in json.loads(files.wcc.read_text())}


def excluded_by_labels(files: RunFiles) -> set[str]:
    """Keys the annotator excluded at Q0. Read for its keys only, never its answers."""
    if not files.labels.exists():
        return set()
    labels = json.loads(files.labels.read_text()).get("labels", {})
    return {key for key, label in labels.items() if not label.get("eligible", True)}


def _write(path: Path, records: list[Any]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps([asdict(r) for r in records], indent=2, ensure_ascii=False) + "\n")


def run_metadata() -> dict[str, Any]:
    def git(*args: str) -> str:
        try:
            done = subprocess.run(
                ["git", *args], cwd=REPO_ROOT, capture_output=True, text=True, check=True
            )
        except (OSError, subprocess.CalledProcessError):
            return ""
        return done.stdout.strip()

    return {
        "started_at": _now(),
        "commit": git("rev-parse", "HEAD"),
        "src_changed_since_commit": bool(git("status", "--porcelain", "--", "src")),
        "python": sys.version.split()[0],
        "platform": platform.platform(),
        "playwright": version("playwright"),
        "plan_sha256": _sha256(Path(__file__).with_name("PLAN.md")),
        "plan_py_sha256": _sha256(Path(plan.__file__)),
        "sample_sha256": _sha256(SAMPLE_PATH),
    }


Capture = Callable[[str, Path, Path, CaptureRecord], Awaitable[None]]
Visit = Callable[[str, WccRecord], Awaitable[None]]
Robots = Callable[[str], str | None]


async def visit_candidates(
    files: RunFiles,
    candidates: list[Candidate],
    *,
    robots: Robots = robots_refusal,
    take_capture: Capture = capture,
    visit: Visit = wcc_visit,
    pause_seconds: float = PAUSE_SECONDS,
    per_stratum: int = plan.PAGES_PER_STRATUM,
    log: Callable[[str], None] = print,
) -> dict[str, int]:
    """Visit in order until each stratum has ``per_stratum`` complete, unexcluded pages.

    Returns how many such pages each stratum has afterwards.
    """
    files.captures.mkdir(parents=True, exist_ok=True)
    index = load_index(files)
    results = load_wcc(files)
    excluded = excluded_by_labels(files)

    def counted(stratum: str) -> int:
        return sum(
            1
            for key, record in index.items()
            if record.stratum == stratum
            and record.status == "captured"
            and key in results
            and results[key].complete
            and key not in excluded
        )

    for candidate in candidates:
        key = key_of(candidate)
        if key in index or counted(candidate.stratum) >= per_stratum:
            continue
        log(f"  {key} {candidate.url}")
        record = CaptureRecord(
            key, candidate.stratum, candidate.position, candidate.origin, candidate.url
        )
        refusal = await asyncio.to_thread(robots, candidate.url)
        if refusal is not None:
            record.status, record.reason = "robots", refusal
        else:
            stem = key.replace("/", "-")
            shot, text = files.captures / f"{stem}.png", files.captures / f"{stem}.txt"
            record.captured_at = _now()
            try:
                await take_capture(candidate.url, shot, text, record)
            except Exception as failed:
                record.status, record.reason = "failed", f"{type(failed).__name__}: {failed}"
            else:
                record.status = "captured"
                record.screenshot, record.text = shot.name, text.name
                record.screenshot_sha256 = _sha256(shot) if shot.exists() else ""
                record.text_sha256 = _sha256(text) if text.exists() else ""
                outcome = WccRecord(key, candidate.url, observed_at=_now())
                await visit(candidate.url, outcome)
                results[key] = outcome
                _write(files.wcc, list(results.values()))
        index[key] = record
        _write(files.index, list(index.values()))
        log(f"    {record.status} {record.reason[:80]}")
        await asyncio.sleep(pause_seconds)

    return {stratum.name: counted(stratum.name) for stratum in plan.STRATA}


def run_all(out: Path, *, only_stratum: str = "") -> int:
    files = RunFiles(out)
    out.mkdir(parents=True, exist_ok=True)
    if not files.meta.exists():
        files.meta.write_text(json.dumps(run_metadata(), indent=2) + "\n")
    candidates = load_sample()
    if only_stratum:
        candidates = [c for c in candidates if c.stratum == only_stratum]
    counts = asyncio.run(visit_candidates(files, candidates))
    for stratum, count in counts.items():
        short = "" if count >= plan.PAGES_PER_STRATUM else "  (short)"
        print(f"{stratum}: {count} of {plan.PAGES_PER_STRATUM} complete pages{short}")
    print(f"\nNext: label without opening {files.wcc}.")
    return 0
