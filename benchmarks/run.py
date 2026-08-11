"""One documented command that runs the benchmark (specification 14.4).

``uv run python -m benchmarks.run`` — that is the whole interface, and it is a
requirement rather than a convenience. A benchmark invoked differently by whoever
publishes it is a benchmark nobody else can reproduce.

Three things this module is responsible for that are easy to get subtly wrong:

**Clean sessions.** Each run gets its own browser context, and the cold arm gets
its own empty data directory. A warm run is only warm because a template was
persisted by an earlier run of the same task class, so the two must not share a
directory by accident, and the cold run must not inherit one from a previous
invocation.

**Verification, not self-report.** The model says DONE; the fixture decides. Every
run's success is read from the live page after the loop ends.

**Enrichment off.** Specification 14.2 requires it, and it is passed explicitly
rather than left to the environment — a benchmark whose result depends on whether
someone exported a variable is not a controlled experiment.
"""

from __future__ import annotations

import argparse
import asyncio
import functools
import http.server
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path

from benchmarks.agent_loop import ModelClient, run_loop
from benchmarks.arms import Arm, BaselineArm
from benchmarks.metrics import Run, comparison_table, write_runs
from benchmarks.tasks import TASKS, Task
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

#: Specification 14.2 fixes one model and one version across every arm. The value
#: is a CLI default rather than a constant read at import, so a published result
#: can name the model it used without the code having to be edited to produce it.
DEFAULT_MODEL = "claude-haiku-4-5"


class _QuietHandler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, format: str, *args: object) -> None:  # noqa: A002
        """The benchmark's output is the measurement, not the access log."""


@contextmanager
def fixture_server() -> Iterator[str]:
    """Serve ``fixtures/`` over loopback — the reproducible benchmark of record."""
    handler = functools.partial(_QuietHandler, directory=str(FIXTURES))
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://{server.server_address[0]}:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


async def run_one(
    client: ModelClient,
    task: Task,
    arm_name: str,
    attempt: int,
    base_url: str,
    data_dir: Path,
) -> Run:
    """One arm, one task, one attempt, in a browser session of its own."""
    started = datetime.now(UTC)
    value = task.input_for(arm_name)
    driver = PlaywrightDriver(UrlPolicy(), DriverSettings(allow_private_network=True))
    unauthorized = 0

    try:
        if arm_name == "baseline":
            arm: Arm = BaselineArm(page=await _blank_page(driver))
            await arm.call("browser_navigate", {"url": f"{base_url}{task.page}"})
            result = await run_loop(client, arm, task.prompt(value))
            succeeded, reason = await _verify_baseline(arm, task)
        else:
            store = Store(data_dir / "wcc.sqlite3")
            try:
                # Enrichment off by construction, per specification 14.2 — an
                # inert enricher rather than an unset variable.
                coordinator = Coordinator(driver, store, enricher=Enricher())
                wcc = WccArm(coordinator=coordinator, driver=driver)
                result = await run_loop(
                    client, wcc, f"Start at {base_url}{task.page}. {task.prompt(value)}"
                )
                unauthorized = wcc.unauthorized
                succeeded, reason = await _verify_wcc(driver, wcc, task)
            finally:
                store.close()
    finally:
        await driver.shutdown()

    finished = datetime.now(UTC)
    return Run(
        task_id=task.task_id,
        arm=arm_name,
        attempt=attempt,
        model=client.model,
        started_at=started.isoformat(),
        finished_at=finished.isoformat(),
        duration_seconds=result.duration_seconds,
        tool_calls=result.tool_calls,
        turns=result.turn_count,
        succeeded=succeeded,
        input_value=value,
        usage=result.usage,
        failure_reason=reason,
        unauthorized_executions=unauthorized,
        transcript=[turn.__dict__ for turn in result.turns],
    )


async def _blank_page(driver: PlaywrightDriver):  # type: ignore[no-untyped-def]
    """A raw Playwright page for the baseline arm.

    The baseline is deliberately *not* built on WCC's driver abstractions — it is
    meant to be what an agent has without this project, and borrowing WCC's
    normalization would quietly make the comparison flattering.
    """
    await driver.start()
    context = await driver.browser.new_context()
    return await context.new_page()


async def _verify_baseline(arm: Arm, task: Task) -> tuple[bool, str | None]:
    page = arm.page  # type: ignore[attr-defined]
    return _judge(page.url, await page.content(), task)


async def _verify_wcc(driver: PlaywrightDriver, arm: WccArm, task: Task) -> tuple[bool, str | None]:
    session_ids = list(arm.inspections)
    if not session_ids:
        return False, "the run never inspected a page"
    snapshot = await driver.snapshot(session_ids[-1])
    text = " ".join(element.resolved_label or "" for element in snapshot.elements) + " ".join(
        record.identity for c in snapshot.collections for record in c.records
    )
    return _judge(snapshot.url, text, task)


def _judge(url: str, text: str, task: Task) -> tuple[bool, str | None]:
    if task.expects_url_contains and task.expects_url_contains not in url:
        return False, f"the address never contained {task.expects_url_contains!r}"
    if task.expects_text.lower() not in text.lower():
        return False, f"the page never showed {task.expects_text!r}"
    return True, None


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--attempts", type=int, default=3, help="per arm; 3 is the minimum")
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="build everything and stop before the first paid call",
    )
    arguments = parser.parse_args(argv)

    if arguments.attempts < 3:
        parser.error("specification 14.2 requires at least three runs per condition")

    if arguments.dry_run:
        print(f"model {arguments.model}, {arguments.attempts} attempts per arm")
        print(f"tasks: {', '.join(task.task_id for task in TASKS)}")
        print("dry run: no provider was called and no result was written")
        return 0

    from benchmarks.provider import AnthropicModelClient

    runs = asyncio.run(_all(AnthropicModelClient(arguments.model), arguments.attempts))
    path = write_runs(RESULTS, runs)
    print(comparison_table(runs))
    print(f"\nraw runs: {path}")
    return 0


async def _all(client: ModelClient, attempts: int) -> list[Run]:
    runs: list[Run] = []
    with fixture_server() as base_url:
        for task in TASKS:
            for attempt in range(1, attempts + 1):
                warm_dir = RESULTS / f".data-{task.task_id}-{attempt}"
                for arm in ("baseline", "wcc_cold", "wcc_warm"):
                    # cold and warm share a directory *in that order*, which is the
                    # only thing that makes the second one warm; baseline never
                    # touches it.
                    runs.append(await run_one(client, task, arm, attempt, base_url, warm_dir))
    return runs


if __name__ == "__main__":
    raise SystemExit(main())
