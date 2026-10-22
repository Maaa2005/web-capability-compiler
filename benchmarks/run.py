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
run's success is read from the page after the loop ends — its address and the
text a reader would see, never the markup — and the same check is applied to
every arm. The fixture's results pages are static, so the address is what shows
*which* search was made, and a search for anything other than the task's input
is a failed run.

**Enrichment off.** Specification 14.2 requires it, and it is passed explicitly
rather than left to the environment — a benchmark whose result depends on whether
someone exported a variable is not a controlled experiment.
"""

from __future__ import annotations

import argparse
import asyncio
import functools
import http.server
import tempfile
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import UTC, datetime
from pathlib import Path
from urllib.parse import parse_qsl, urlsplit

from playwright.async_api import async_playwright

from benchmarks.agent_loop import Agent, LoopAgent, LoopResult
from benchmarks.arms import BaselineArm
from benchmarks.metrics import Run, report, write_runs
from benchmarks.tasks import TASKS, Task
from benchmarks.wcc_arm import WccArm
from wcc.driver.base import DriverError, DriverSettings
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
    agent: Agent,
    task: Task,
    arm_name: str,
    attempt: int,
    base_url: str,
    data_dir: Path,
) -> Run:
    """One arm, one task, one attempt, in a browser session of its own."""
    started = datetime.now(UTC)
    value = task.input_for(arm_name)
    unauthorized = 0
    reused: list[bool] = []

    if arm_name == "baseline":
        result, succeeded, reason = await _run_baseline(agent, task, base_url, value)
    else:
        result, succeeded, reason, unauthorized, reused = await _run_wcc(
            agent, task, base_url, value, data_dir
        )

    finished = datetime.now(UTC)
    return Run(
        task_id=task.task_id,
        arm=arm_name,
        attempt=attempt,
        model=agent.model,
        served_model=result.served_model,
        started_at=started.isoformat(),
        finished_at=finished.isoformat(),
        duration_seconds=result.duration_seconds,
        tool_calls=result.tool_calls,
        turns=result.turn_count,
        succeeded=succeeded,
        input_value=value,
        usage=result.usage,
        failure_reason=reason,
        stopped_because=result.stopped_because,
        reused=reused,
        unauthorized_executions=unauthorized,
        transcript=[turn.__dict__ for turn in result.turns],
    )


async def _run_baseline(
    agent: Agent, task: Task, base_url: str, value: str
) -> tuple[LoopResult, bool, str | None]:
    """The baseline arm, on raw Playwright.

    Deliberately *not* built on WCC's driver. The arm is meant to be what an agent
    has without this project, and borrowing WCC's session handling or its
    normalization would quietly make the comparison flattering. Its own browser,
    its own context, closed when the run ends.
    """
    async with async_playwright() as playwright:
        browser = await playwright.chromium.launch()
        try:
            page = await (await browser.new_context()).new_page()
            arm = BaselineArm(page=page)
            await arm.call("browser_navigate", {"url": f"{base_url}{task.page}"})
            result = await agent.run(arm, task.prompt(value))
            succeeded, reason = _judge(page.url, await page.inner_text("body"), task, value)
            return result, succeeded, reason
        finally:
            await browser.close()


async def _run_wcc(
    agent: Agent, task: Task, base_url: str, value: str, data_dir: Path
) -> tuple[LoopResult, bool, str | None, int, list[bool]]:
    """The WCC arm, on the real runtime, with enrichment off per 14.2."""
    driver = PlaywrightDriver(UrlPolicy(), DriverSettings(allow_private_network=True))
    store = Store(data_dir / "wcc.sqlite3")
    try:
        coordinator = Coordinator(driver, store, enricher=Enricher())
        arm = WccArm(coordinator=coordinator, driver=driver)
        result = await agent.run(arm, f"Start at {base_url}{task.page}. {task.prompt(value)}")
        succeeded, reason = await _verify_wcc(arm, task, value)
        return result, succeeded, reason, arm.unauthorized, arm.reused
    finally:
        store.close()
        await driver.shutdown()


async def _verify_wcc(arm: WccArm, task: Task, value: str) -> tuple[bool, str | None]:
    """Judge the last session the agent opened, as it ended up.

    Read the same way as the baseline's page — address and visible text — rather
    than from WCC's own snapshot, so the two arms are held to one standard.
    """
    try:
        page = await arm.final_page()
    except DriverError as error:
        return False, f"the final page could not be read: {error}"
    if page is None:
        return False, "the run never opened a page"
    return _judge(*page, task, value)


def _judge(url: str, text: str, task: Task, value: str) -> tuple[bool, str | None]:
    wanted = _normalized(value)
    sent = [
        _normalized(given)
        for name, given in parse_qsl(urlsplit(url).query, keep_blank_values=True)
        if task.input_parameter in (None, name)
    ]
    if not any(wanted in given for given in sent):
        where = f"the {task.input_parameter!r} parameter" if task.input_parameter else "the address"
        return False, f"{where} never carried {value!r} (ended at {url})"
    if _normalized(task.expects_text) not in _normalized(text):
        return False, f"the page never showed {task.expects_text!r}"
    return True, None


def _normalized(text: str) -> str:
    return " ".join(text.lower().split())


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--model", default=DEFAULT_MODEL)
    parser.add_argument("--attempts", type=int, default=3, help="per arm; 3 is the minimum")
    parser.add_argument(
        "--provider",
        choices=("subscription", "api"),
        default="subscription",
        help="Claude Code signed in to a subscription, or the Messages API with a key",
    )
    parser.add_argument(
        "--calibrate",
        action="store_true",
        help="measure the input Claude Code adds to every request, then stop",
    )
    parser.add_argument(
        "--only",
        metavar="TASK",
        help="run one task, for a smoke test; the result is not a benchmark of record",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        help="build everything and stop before the first model call",
    )
    arguments = parser.parse_args(argv)

    if arguments.attempts < 3 and not arguments.only:
        parser.error("specification 14.2 requires at least three runs per condition")
    tasks = [task for task in TASKS if arguments.only in (None, task.task_id)]
    if not tasks:
        parser.error(f"no task is called {arguments.only!r}")

    if arguments.dry_run:
        print(f"model {arguments.model} via {arguments.provider}, {arguments.attempts} attempts")
        print(f"tasks: {', '.join(task.task_id for task in tasks)}")
        print("dry run: no provider was called and no result was written")
        return 0

    if arguments.calibrate:
        from benchmarks.subscription import calibrate

        usage = asyncio.run(calibrate(arguments.model))
        print(
            f"one near-empty request: {usage.input_tokens} input tokens "
            f"({usage.cache_read_input_tokens} read from cache), "
            f"{usage.output_tokens} output"
        )
        return 0

    agent: Agent
    if arguments.provider == "subscription":
        from benchmarks.subscription import SubscriptionAgent

        agent = SubscriptionAgent(arguments.model)
    else:
        from benchmarks.provider import AnthropicModelClient

        agent = LoopAgent(AnthropicModelClient(arguments.model))

    runs: list[Run] = []
    try:
        asyncio.run(_all(agent, tasks, arguments.attempts, runs))
    except BaseException:
        # Whatever finished is still evidence, but not a benchmark of record.
        if runs:
            print(f"stopped early; finished runs: {write_runs(RESULTS / 'incomplete', runs)}")
        raise
    directory = RESULTS if not arguments.only else RESULTS / "smoke"
    path = write_runs(directory, runs)
    print(report(runs))
    print(f"\nraw runs: {path}")
    return 0


async def _all(agent: Agent, tasks: list[Task], attempts: int, runs: list[Run]) -> list[Run]:
    with fixture_server() as base_url:
        for task in tasks:
            for attempt in range(1, attempts + 1):
                # A fresh directory per attempt, not a fixed path under results/:
                # a fixed path survives the invocation, and the next invocation's
                # cold run would then start with the template this one persisted.
                with tempfile.TemporaryDirectory(prefix=f"wcc-{task.task_id}-") as directory:
                    for arm in ("baseline", "wcc_cold", "wcc_warm"):
                        # cold and warm share the directory *in that order*, which
                        # is the only thing that makes the second one warm;
                        # baseline never touches it.
                        runs.append(
                            await run_one(agent, task, arm, attempt, base_url, Path(directory))
                        )
    return runs


if __name__ == "__main__":
    raise SystemExit(main())
