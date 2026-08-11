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
from dataclasses import asdict, dataclass, field
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

    @property
    def total_tokens(self) -> int:
        return self.input_tokens + self.output_tokens

    def plus(self, input_tokens: int | None, output_tokens: int | None) -> Usage:
        if input_tokens is None or output_tokens is None:
            return Usage(self.input_tokens, self.output_tokens, reported=False)
        return Usage(
            self.input_tokens + input_tokens,
            self.output_tokens + output_tokens,
            reported=self.reported,
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
    usage: Usage = field(default_factory=Usage)
    failure_reason: str | None = None
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
