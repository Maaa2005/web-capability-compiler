"""Score WCC's held-out results against the labels, by the rules of ``PLAN.md``.

The score is a pure function of four files: ``sample.json``, the capture index,
``wcc/results.json`` and ``labels.json``. The report names each file's SHA-256, and
the plan's, so anyone can check that a published score came from the published files
and was judged by the published line.

``score_run`` is the step of record. Before it reads WCC's results it stops unless
the code, the plan and the sample are the ones the run started with and the labels
are committed. ``score_files`` has no such check, and only tests call it directly.
"""

from __future__ import annotations

import hashlib
import json
import math
import unicodedata
from collections.abc import Sequence
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any

from benchmarks.heldout import plan
from benchmarks.heldout.run import (
    CaptureRecord,
    RunFiles,
    WccRecord,
    key_of,
    load_index,
    load_wcc,
    resume_mismatches,
    run_metadata,
    uncommitted,
)
from benchmarks.heldout.sample import SAMPLE_PATH, Candidate, load_sample


class NotReady(Exception):
    """Scoring would have to guess: a page in the scored window has no label."""


def normalise(text: str) -> str:
    return " ".join(unicodedata.normalize("NFKC", text).casefold().split())


def title_found(title: str, labels: list[str]) -> bool:
    wanted = normalise(title)
    if not wanted:
        return False
    for label in labels:
        seen = normalise(label)
        if seen == wanted:
            return True
        shorter, longer = sorted((seen, wanted), key=len)
        if len(shorter) >= plan.TITLE_CONTAINMENT_MIN_CHARS and shorter in longer:
            return True
    return False


@dataclass(frozen=True)
class PageScore:
    key: str
    stratum: str
    origin: str
    search_offered: bool
    search_labelled: bool
    search_right: bool
    results_offered: bool
    results_labelled: bool
    results_matched: bool
    next_offered: bool
    next_labelled: bool
    next_right: bool
    unsafe: list[str]
    any_offered: bool
    had_something_by_proxy: bool
    had_something_by_label: bool
    offered: list[dict[str, Any]]


def score_page(
    key: str, stratum: str, origin: str, label: dict[str, Any], wcc: WccRecord
) -> PageScore:
    offered = wcc.offered
    names = {o["name"] for o in offered}
    results_offered = plan.RESULT_LIST_NAME in names
    titles = list(label.get("titles") or [])
    matched = (
        results_offered
        and bool(label["result_list"])
        and bool(titles)
        and all(title_found(t, wcc.result_list) for t in titles)
    )
    controls = {normalise(c) for c in label.get("controls") or [] if normalise(c)}
    unsafe = sorted(
        {
            f"{o['name']}: {lab}"
            for o in offered
            if o.get("risk") not in plan.NON_EXECUTABLE_RISKS
            for lab in o.get("labels", [])
            if normalise(lab) in controls
        }
    )
    search_offered = any(
        o["category"] == plan.SEARCH_CATEGORY
        or (o["category"] == plan.FILTER_CATEGORY and o.get("text_parameters", 0) > 0)
        for o in offered
    )
    next_offered = any(name in names for name in plan.NEXT_PAGE_NAMES)
    return PageScore(
        key=key,
        stratum=stratum,
        origin=origin,
        search_offered=search_offered,
        search_labelled=bool(label["search"]),
        search_right=search_offered and bool(label["search"]),
        results_offered=results_offered,
        results_labelled=bool(label["result_list"]),
        results_matched=matched,
        next_offered=next_offered,
        next_labelled=bool(label["next_page"]),
        next_right=next_offered and bool(label["next_page"]),
        unsafe=unsafe,
        any_offered=bool(offered),
        had_something_by_proxy=wcc.forms_seen > 0 or wcc.interactive_elements >= 20,
        had_something_by_label=bool(label["search"] or label["result_list"] or label["next_page"]),
        offered=offered,
    )


def wilson(k: int, n: int, z: float = 1.959964) -> tuple[float, float] | None:
    """The Wilson score interval for ``k`` successes in ``n`` trials."""
    if n == 0:
        return None
    p = k / n
    denominator = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / denominator
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / denominator
    return max(0.0, centre - half), min(1.0, centre + half)


def metric(k: int, n: int) -> dict[str, Any]:
    interval = wilson(k, n)
    return {
        "k": k,
        "n": n,
        "value": k / n if n else None,
        "wilson95": list(interval) if interval else None,
        "judged": n >= plan.MIN_CASES_TO_JUDGE,
    }


def metrics(pages: list[PageScore]) -> dict[str, Any]:
    def pr(offered: str, labelled: str, right: str) -> dict[str, Any]:
        predicted = [p for p in pages if getattr(p, offered)]
        actual = [p for p in pages if getattr(p, labelled)]
        return {
            "precision": metric(sum(1 for p in predicted if getattr(p, right)), len(predicted)),
            "recall": metric(sum(1 for p in actual if getattr(p, right)), len(actual)),
        }

    proxy = [p for p in pages if p.had_something_by_proxy]
    labelled = [p for p in pages if p.had_something_by_label]
    unsafe_pages = sum(1 for p in pages if p.unsafe)
    upper = wilson(unsafe_pages, len(pages))
    return {
        "pages": len(pages),
        "search": pr("search_offered", "search_labelled", "search_right"),
        "result_list": pr("results_offered", "results_labelled", "results_matched"),
        "next_page": pr("next_offered", "next_labelled", "next_right"),
        "unsafe_exposures": {
            "pages": unsafe_pages,
            "wilson95_upper": upper[1] if upper else None,
        },
        "any_capability": {
            "old_proxy_denominator": metric(sum(1 for p in proxy if p.any_offered), len(proxy)),
            "label_denominator": metric(sum(1 for p in labelled if p.any_offered), len(labelled)),
        },
    }


def verdict(
    m: dict[str, Any], unfinished: Sequence[str] = (), short: Sequence[str] = ()
) -> dict[str, Any]:
    """The plan's pass line. ``unfinished`` names strata the run has not finished, and
    ``short`` those that ran out of candidates before the plan's number of pages.

    A number that clears the line over part of the sample says nothing about the
    rest, so a run with either can fail but cannot pass.
    """

    def judged(entry: dict[str, Any], floor: float) -> str:
        if not entry["judged"]:
            return "not judged"
        return "pass" if entry["value"] >= floor else "fail"

    def both(kind: str, precision: float, recall: float) -> str:
        parts = [
            judged(m[kind]["precision"], precision),
            judged(m[kind]["recall"], recall),
        ]
        if "fail" in parts:
            return "fail"
        return "not judged" if "not judged" in parts else "pass"

    criteria = {
        "T1 unsafe exposures = 0": (
            "pass" if m["unsafe_exposures"]["pages"] <= plan.MAX_UNSAFE_EXPOSURES else "fail"
        ),
        "T2 search P >= 0.90 and R >= 0.60": both(
            "search", plan.SEARCH_MIN_PRECISION, plan.SEARCH_MIN_RECALL
        ),
        "T3 result list P >= 0.70 and R >= 0.40": both(
            "result_list", plan.RESULTS_MIN_PRECISION, plan.RESULTS_MIN_RECALL
        ),
    }
    outcomes = set(criteria.values())
    overall = "fail" if "fail" in outcomes else "not judged" if "not judged" in outcomes else "pass"
    if overall == "pass" and (unfinished or short):
        overall = "not judged"
    return {
        "criteria": criteria,
        "unfinished_strata": list(unfinished),
        "short_strata": list(short),
        "overall": overall,
    }


def select_scored(
    candidates: list[Candidate],
    index: dict[str, CaptureRecord],
    results: dict[str, WccRecord],
    labels: dict[str, dict[str, Any]],
    per_stratum: int = plan.PAGES_PER_STRATUM,
) -> tuple[list[Candidate], dict[str, dict[str, int]]]:
    """The first ``per_stratum`` complete, eligible pages of each stratum, in order."""
    scored: list[Candidate] = []
    accounting: dict[str, dict[str, int]] = {}
    for stratum in plan.STRATA:
        tally = {
            "used": 0,
            "robots": 0,
            "unreachable": 0,
            "landed_elsewhere": 0,
            "failed": 0,
            "wcc_error": 0,
            "excluded": 0,
            "scored": 0,
        }
        for candidate in (c for c in candidates if c.stratum == stratum.name):
            if tally["scored"] >= per_stratum:
                break
            key = key_of(candidate)
            record = index.get(key)
            if record is None:
                break
            tally["used"] += 1
            if record.status in ("robots", "unreachable", "landed_elsewhere"):
                tally[record.status] += 1
                continue
            if record.status != "captured":
                tally["failed"] += 1
                continue
            outcome = results.get(key)
            if outcome is None or not outcome.complete:
                tally["wcc_error"] += 1
                continue
            label = labels.get(key)
            if label is None:
                raise NotReady(f"{key} is in the scored window and has no label")
            if not label.get("eligible"):
                tally["excluded"] += 1
                continue
            tally["scored"] += 1
            scored.append(candidate)
        accounting[stratum.name] = tally
    return scored, accounting


def stratum_states(
    candidates: list[Candidate],
    accounting: dict[str, dict[str, int]],
    per_stratum: int = plan.PAGES_PER_STRATUM,
) -> dict[str, str]:
    """``complete``, ``short`` (all the plan's candidates used, too few pages) or
    ``unfinished`` (candidates are left, or some are waiting to be tried again because
    the connection failed, so the stratum has not been measured)."""
    states: dict[str, str] = {}
    for stratum in plan.STRATA:
        tally = accounting[stratum.name]
        total = sum(1 for c in candidates if c.stratum == stratum.name)
        if tally["scored"] >= per_stratum:
            states[stratum.name] = "complete"
        elif (
            total >= plan.CANDIDATES_PER_STRATUM
            and tally["used"] >= total
            and not tally.get("unreachable")
        ):
            states[stratum.name] = "short"
        else:
            states[stratum.name] = "unfinished"
    return states


def cohen_kappa(first: list[bool], second: list[bool]) -> float | None:
    n = len(first)
    if n == 0:
        return None
    observed = sum(1 for a, b in zip(first, second, strict=True) if a == b) / n
    p1, p2 = sum(first) / n, sum(second) / n
    expected = p1 * p2 + (1 - p1) * (1 - p2)
    if expected == 1:
        return None
    return (observed - expected) / (1 - expected)


def agreement(first: dict[str, Any], second: dict[str, Any]) -> dict[str, Any]:
    keys = sorted(k for k in second if k in first and first[k].get("eligible"))
    keys = [k for k in keys if second[k].get("eligible")]
    out: dict[str, Any] = {"pages": len(keys)}
    for question in ("search", "result_list", "next_page"):
        out[question] = cohen_kappa(
            [bool(first[k][question]) for k in keys], [bool(second[k][question]) for k in keys]
        )
    return out


def _sha256(path: Path) -> str | None:
    return hashlib.sha256(path.read_bytes()).hexdigest() if path.exists() else None


def score_files(
    files: RunFiles,
    candidates: list[Candidate] | None = None,
    per_stratum: int = plan.PAGES_PER_STRATUM,
) -> dict[str, Any]:
    """Score a run. ``candidates`` and ``per_stratum`` are for tests; the run of
    record always uses the committed sample and the plan's number."""
    candidates = load_sample() if candidates is None else candidates
    index = load_index(files)
    results = load_wcc(files)
    labels_document = json.loads(files.labels.read_text(encoding="utf-8"))
    labels = labels_document["labels"]
    scored, accounting = select_scored(candidates, index, results, labels, per_stratum)
    pages = [
        score_page(key_of(c), c.stratum, c.origin, labels[key_of(c)], results[key_of(c)])
        for c in scored
    ]
    m = metrics(pages)
    states = stratum_states(candidates, accounting, per_stratum)
    unfinished = [name for name, state in states.items() if state == "unfinished"]
    short = [name for name, state in states.items() if state == "short"]
    document: dict[str, Any] = {
        "inputs": {
            "PLAN.md": _sha256(Path(plan.__file__).with_name("PLAN.md")),
            "plan.py": _sha256(Path(plan.__file__)),
            "sample.json": _sha256(SAMPLE_PATH),
            "captures/index.json": _sha256(files.index),
            "wcc/results.json": _sha256(files.wcc),
            "labels.json": _sha256(files.labels),
        },
        "run": json.loads(files.meta.read_text(encoding="utf-8")) if files.meta.exists() else None,
        "annotator": labels_document.get("annotator"),
        "accounting": accounting,
        "strata": states,
        "metrics": m,
        "visible_text_unread": sum(results[key_of(c)].visible_text_unread for c in scored),
        "verdict": verdict(m, unfinished, short),
        "pages": [asdict(p) for p in pages],
    }
    second = files.out / "labels-second.json"
    if second.exists():
        document["agreement"] = agreement(
            labels, json.loads(second.read_text(encoding="utf-8"))["labels"]
        )
    return document


def _fmt(entry: dict[str, Any]) -> str:
    if entry["value"] is None:
        return "— (0 cases)"
    low, high = entry["wilson95"]
    judged = "" if entry["judged"] else ", not judged"
    return f"{entry['value']:.2f} ({entry['k']}/{entry['n']}; 95% {low:.2f}–{high:.2f}{judged})"


def _stratum_warnings(states: dict[str, str]) -> list[str]:
    lines = []
    for state, meaning in (
        ("unfinished", "not finished, so the run cannot pass"),
        ("short", "used every candidate and came up short, so the run cannot pass"),
    ):
        names = [name for name, seen in states.items() if seen == state]
        if names:
            lines += ["", f"**Strata {meaning}:** {', '.join(names)}."]
    return lines


def _offered(capability: dict[str, Any]) -> str:
    """One capability for the report: every label, so a reader can check T1 from here."""
    labels = capability.get("labels", [])
    named = [name for name in capability.get("names", []) if name not in labels]
    return (
        f"{capability['name']} [{capability.get('risk', 'risk unknown')}] "
        f"({', '.join(labels)})" + (f" named {', '.join(named)}" if named else "")
    )


def report(document: dict[str, Any]) -> str:
    m, v = document["metrics"], document["verdict"]
    lines = [
        "# Held-out evaluation: result",
        "",
        f"**Verdict: {v['overall']}** over {m['pages']} scored pages. "
        "Thresholds and rules: `benchmarks/heldout/PLAN.md`.",
        *_stratum_warnings(document["strata"]),
        "",
        "| Criterion | Result |",
        "|---|---|",
        *(f"| {name} | {outcome} |" for name, outcome in v["criteria"].items()),
        "",
        "| Type | Precision | Recall |",
        "|---|---|---|",
    ]
    for kind in ("search", "result_list", "next_page"):
        lines.append(f"| {kind} | {_fmt(m[kind]['precision'])} | {_fmt(m[kind]['recall'])} |")
    unsafe = m["unsafe_exposures"]
    upper = unsafe["wilson95_upper"]
    lines += [
        "",
        f"Unsafe exposures: {unsafe['pages']} pages"
        + (f" (95% upper bound {upper:.3f})" if upper is not None else ""),
        "",
        "Elements whose visible text could not be read, with their name used instead: "
        f"{document['visible_text_unread']}",
        "",
        "Any capability on a page with something to find (the old measure):",
        f"- old proxy denominator: {_fmt(m['any_capability']['old_proxy_denominator'])}",
        f"- labels' denominator: {_fmt(m['any_capability']['label_denominator'])}",
        "",
        "| Stratum | used | robots | unreachable | landed elsewhere | failed | WCC error | "
        "excluded | scored |",
        "|---|---:|---:|---:|---:|---:|---:|---:|---:|",
    ]
    for stratum, tally in document["accounting"].items():
        lines.append(
            f"| {stratum} | {tally['used']} | {tally['robots']} | {tally['unreachable']} | "
            f"{tally['landed_elsewhere']} | {tally['failed']} | {tally['wcc_error']} | "
            f"{tally['excluded']} | {tally['scored']} |"
        )
    if "agreement" in document:
        a = document["agreement"]
        lines += ["", f"Agreement over {a['pages']} pages (Cohen's κ): "]
        lines += [f"- {q}: {a[q]}" for q in ("search", "result_list", "next_page")]
    else:
        lines += ["", "Single annotator: no agreement check was made."]
    lines += [
        "",
        "| Page | S label/WCC | R label/WCC/match | N label/WCC | unsafe | offered (labels) |",
        "|---|---|---|---|---|---|",
    ]
    yn = {True: "y", False: "n"}
    for page in document["pages"]:
        offered = "; ".join(_offered(o) for o in page["offered"])
        lines.append(
            f"| {page['key']} {page['origin']} "
            f"| {yn[page['search_labelled']]}/{yn[page['search_offered']]} "
            f"| {yn[page['results_labelled']]}/{yn[page['results_offered']]}/"
            f"{yn[page['results_matched']]} "
            f"| {yn[page['next_labelled']]}/{yn[page['next_offered']]} "
            f"| {'; '.join(page['unsafe']) or '—'} | {offered or '—'} |"
        )
    lines += ["", "Inputs (SHA-256):", ""]
    lines += [f"- `{name}`: `{digest}`" for name, digest in document["inputs"].items()]
    run = document.get("run") or {}
    lines += ["", f"Run at commit `{run.get('commit')}`, code `{run.get('code_sha256')}`."]
    return "\n".join(lines) + "\n"


def scoring_blockers(files: RunFiles) -> list[str]:
    """Why this run may not be scored yet. Reads no results and no labels' answers."""
    if not files.meta.exists():
        return [f"{files.meta} is missing, so the conditions of the run cannot be checked"]
    try:
        recorded = json.loads(files.meta.read_text(encoding="utf-8"))
    except ValueError as unreadable:
        return [f"{files.meta} cannot be read: {unreadable}"]
    problems = resume_mismatches(recorded, run_metadata(), files.out)
    second = files.out / "labels-second.json"
    for path in (files.labels, *([second] if second.exists() else [])):
        problem = uncommitted(path)
        if problem is not None:
            problems.append(problem)
    return problems


def score_run(out: Path) -> int:
    files = RunFiles(out)
    blockers = scoring_blockers(files)
    if blockers:
        print(f"not scored: {out} does not meet the plan's conditions for scoring.")
        for blocker in blockers:
            print(f"  {blocker}")
        return 2
    try:
        document = score_files(files)
    except NotReady as missing:
        print(f"not scored: {missing}. Run `label` first.")
        return 2
    (out / "score.json").write_text(
        json.dumps(document, indent=2, ensure_ascii=False) + "\n", encoding="utf-8"
    )
    text = report(document)
    (out / "report.md").write_text(text, encoding="utf-8")
    print(text)
    return 0
