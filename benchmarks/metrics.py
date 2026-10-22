"""What is measured, and the only arithmetic done to it (specification 14.3, 14.4).

Two rules shape this module.

**Tokens come from the provider.** :class:`Usage` is filled from API usage metadata
and from nothing else. Specification 14.3 forbids estimating tokens from terminal
text, and the way that rule gets broken is not by someone deciding to estimate —
it is by a default of zero quietly standing in for a field that was never
returned. So a run whose usage is missing is marked ``usage_reported=False`` and
excluded from every summary rather than counted as cheap.

**Failures are never averaged in.** :func:`summarize` takes only the runs that met
their verification condition. Specification 14.4 says a token-reduction figure must
never merge failed and successful runs, and the reason is arithmetic: the cheapest
possible run is one that gives up immediately.

Median and range, not mean. Three runs is the specified minimum, and a mean over
three is one bad network round-trip away from being a different number.
"""

from __future__ import annotations

import json
from dataclasses import asdict, dataclass, field, replace
from datetime import UTC, datetime
from pathlib import Path
from statistics import median

#: The three conditions of specification 14.1, run in separate clean sessions.
ARMS = ("baseline", "wcc_cold", "wcc_warm")


@dataclass(frozen=True)
class Usage:
    """Provider-reported token counts for one run."""

    input_tokens: int = 0
    output_tokens: int = 0
    reported: bool = False
    """False when the provider returned no usage metadata for at least one call.

    Carried rather than inferred from zeros, because zero is also what a run that
    made no calls would report, and those two must not summarize alike.
    """
    cache_creation_input_tokens: int = 0
    cache_read_input_tokens: int = 0
    """How much of ``input_tokens`` was written to or read from a prompt cache.

    A breakdown, not an addition: ``input_tokens`` already counts them. The
    provider reports cached input in separate fields, and a total that left them
    out would credit an arm for the caching rather than for what the model read.
    """

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens

    def plus(self, input_tokens: int | None, output_tokens: int | None) -> Usage:
        if input_tokens is None or output_tokens is None:
            return replace(self, reported=False)
        return replace(
            self,
            input_tokens=self.input_tokens + input_tokens,
            output_tokens=self.output_tokens + output_tokens,
        )


@dataclass
class Run:
    """One execution of one task under one arm — the raw record.

    Every field specification 14.4 requires in a raw log is here, so a published
    result can be checked against the run that produced it rather than trusted.
    """

    task_id: str
    arm: str
    attempt: int
    model: str
    started_at: str
    finished_at: str
    duration_seconds: float
    tool_calls: int
    turns: int
    succeeded: bool
    input_value: str
    served_model: str | None = None
    """The snapshot that answered. ``model`` is what was asked for, usually an
    alias, and an alias is moved to each new snapshot as it ships — so the
    version specification 14.2 fixes is this field, not that one."""

    usage: Usage = field(default_factory=Usage)
    failure_reason: str | None = None
    stopped_because: str | None = None
    """How the agent's loop ended — ``end_turn``, or ``max_turns`` when it ran out.
    Kept beside the verdict rather than folded into it: a run can end on time and
    have done the wrong thing, or run out of turns with the page already right."""

    reused: list[bool] = field(default_factory=list)
    """Per ``inspect_page``, in order, whether stored templates were bound rather
    than the page compiled. Empty for the baseline, which has no such call. A
    ``wcc_warm`` run whose first entry is ``False`` was not warm, and the record
    shows it rather than the arm's name vouching for it."""

    unauthorized_executions: int = 0
    """Executions above ``low`` risk. Specification 14.4 requires this to be zero,
    so it is counted rather than assumed; a benchmark that did not look could not
    report it."""

    transcript: list[dict[str, object]] = field(default_factory=list)

    def to_json(self) -> dict[str, object]:
        record = asdict(self)
        record["usage"]["total_tokens"] = self.usage.total_tokens
        return record


def write_runs(directory: Path, runs: list[Run]) -> Path:
    """Write every raw run, including the failures (specification 14.4)."""
    directory.mkdir(parents=True, exist_ok=True)
    stamp = datetime.now(UTC).strftime("%Y%m%dT%H%M%SZ")
    path = directory / f"runs-{stamp}.json"
    path.write_text(json.dumps([run.to_json() for run in runs], indent=2, sort_keys=True))
    return path


@dataclass(frozen=True)
class ArmSummary:
    arm: str
    runs: int
    successes: int
    median_total_tokens: int | None
    token_range: tuple[int, int] | None
    median_tool_calls: float | None
    median_seconds: float | None

    @property
    def reportable(self) -> bool:
        return self.median_total_tokens is not None


def summarize(runs: list[Run], arm: str) -> ArmSummary:
    """Median and observed range over the successful, usage-reporting runs."""
    attempted = [run for run in runs if run.arm == arm]
    counted = [run for run in attempted if run.succeeded and run.usage.reported]

    if not counted:
        return ArmSummary(
            arm=arm,
            runs=len(attempted),
            successes=sum(1 for run in attempted if run.succeeded),
            median_total_tokens=None,
            token_range=None,
            median_tool_calls=None,
            median_seconds=None,
        )

    totals = sorted(run.usage.total_tokens for run in counted)
    return ArmSummary(
        arm=arm,
        runs=len(attempted),
        successes=len(counted),
        median_total_tokens=int(median(totals)),
        token_range=(totals[0], totals[-1]),
        median_tool_calls=median([run.tool_calls for run in counted]),
        median_seconds=round(median([run.duration_seconds for run in counted]), 2),
    )


def comparison_table(runs: list[Run]) -> str:
    """A Markdown table of the three arms, reported independently.

    No reduction percentage is computed here on purpose. Specification 14.4 says
    the arms are reported independently, and a single headline ratio is exactly
    the number that survives being quoted without its denominator.
    """
    lines = [
        "| Arm | Runs | Counted | Median total tokens | Range | Median tool calls |"
        " Median seconds |",
        "|---|---|---|---|---|---|---|",
    ]
    for arm in ARMS:
        summary = summarize(runs, arm)
        if not summary.reportable:
            lines.append(
                f"| {arm} | {summary.runs} | {summary.successes} | not reported | — | — | — |"
            )
            continue
        low, high = summary.token_range or (0, 0)
        lines.append(
            f"| {arm} | {summary.runs} | {summary.successes} | "
            f"{summary.median_total_tokens} | {low}–{high} | "
            f"{summary.median_tool_calls} | {summary.median_seconds} |"
        )
    return "\n".join(lines)


def report(runs: list[Run]) -> str:
    """One table per task, in the order the tasks ran.

    Pooling tasks into one median would average the six-control page with the
    large catalog — the two exist precisely so the saving can be seen to depend
    on page size, and a pooled figure would answer neither.

    Under each table, how many ``wcc_warm`` runs bound a stored template on their
    first inspection. Specification 14.1 defines warm that way, so the count is
    printed beside the figure rather than taken on trust from the arm's name.

    Last, the snapshot that answered. Specification 14.2 fixes the version across
    the whole benchmark, and each run can only check its own — so a second
    snapshot, or a run that never said, is stated here rather than left for a
    reader to find in the raw records.
    """
    sections: list[str] = []
    for task_id in dict.fromkeys(run.task_id for run in runs):
        task_runs = [run for run in runs if run.task_id == task_id]
        section = f"### {task_id}\n\n{comparison_table(task_runs)}"
        warm = [run for run in task_runs if run.arm == "wcc_warm"]
        if warm:
            bound = sum(1 for run in warm if run.reused[:1] == [True])
            section += (
                f"\n\n{bound} of {len(warm)} wcc_warm runs bound a stored template "
                "on their first inspection."
            )
        sections.append(section)
    sections.append(_snapshots(runs))
    return "\n\n".join(sections)


def _snapshots(runs: list[Run]) -> str:
    served = sorted({run.served_model for run in runs if run.served_model})
    unnamed = sum(1 for run in runs if not run.served_model)
    lines: list[str] = []
    if len(served) == 1 and not unnamed:
        lines.append(f"Every run was answered by {served[0]}.")
    elif len(served) > 1:
        lines.append(f"More than one snapshot answered: {', '.join(served)}.")
    elif served:
        lines.append(f"Answered by {served[0]}.")
    if unnamed:
        lines.append(
            f"{unnamed} run{'' if unnamed == 1 else 's'} did not record which snapshot answered."
        )
    return " ".join(lines)
