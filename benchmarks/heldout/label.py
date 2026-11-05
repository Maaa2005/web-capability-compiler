"""Label the held-out captures, one page at a time, without WCC's results.

This module reads ``captures/index.json`` and the files beside it, and writes
``labels.json`` (or ``labels-second.json`` for the agreement check). It never
opens ``wcc/results.json``; a test holds that.

Answers are saved after every page, so labelling can stop and resume.
"""

from __future__ import annotations

import json
import random
from collections.abc import Callable
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from benchmarks.heldout import plan
from benchmarks.heldout.run import CaptureRecord, RunFiles, WccRecord, key_of, load_index
from benchmarks.heldout.sample import Candidate
from benchmarks.heldout.score import NotReady, select_scored

EXCLUSIONS = ("adult", "login-or-age-wall", "blank-or-error", "parked", "not-a-web-page")

Ask = Callable[[str], str]


def _now() -> str:
    return datetime.now(UTC).replace(microsecond=0).isoformat()


def _yes(ask: Ask, question: str) -> bool:
    while True:
        answer = ask(f"{question} [y/n] ").strip().lower()
        if answer in ("y", "yes"):
            return True
        if answer in ("n", "no"):
            return False


def _texts(ask: Ask, prompt: str, count: int, *, required: int) -> list[str]:
    texts: list[str] = []
    while len(texts) < count:
        answer = ask(f"{prompt} {len(texts) + 1} (empty to stop): ").strip()
        if not answer:
            if len(texts) >= required:
                break
            continue
        texts.append(answer)
    return texts


def ask_page(ask: Ask) -> dict[str, Any]:
    """The five questions of ``PLAN.md``, in order."""
    if not _yes(ask, "Q0 Can this page be labelled?"):
        choices = ", ".join(f"{i + 1}={name}" for i, name in enumerate(EXCLUSIONS))
        while True:
            picked = ask(f"   Why not? {choices}: ").strip()
            if picked.isdigit() and 1 <= int(picked) <= len(EXCLUSIONS):
                return {"eligible": False, "exclusion": EXCLUSIONS[int(picked) - 1]}
    label: dict[str, Any] = {"eligible": True, "exclusion": None}
    label["search"] = _yes(ask, "Q1 Can a visitor type words to search this site?")
    label["result_list"] = _yes(ask, "Q2 Is there a main list of items, each opening its own page?")
    label["titles"] = _texts(ask, "   Title of item", 2, required=2) if label["result_list"] else []
    label["next_page"] = _yes(ask, "Q3 Is there a link to the next page of such a list?")
    label["state_changing"] = _yes(
        ask, "Q4 Is there a control that would change something (sign up, log in, cart...)?"
    )
    label["controls"] = (
        _texts(ask, "   Visible text of control", 3, required=1) if label["state_changing"] else []
    )
    return label


def agreement_keys(eligible_keys: list[str]) -> list[str]:
    """The pages the second annotator labels, drawn with the plan's seed."""
    chosen = sorted(eligible_keys)
    count = min(plan.AGREEMENT_PAGES, len(chosen))
    return sorted(random.Random(plan.SEED).sample(chosen, count))


def scored_keys(
    index: dict[str, CaptureRecord],
    first: dict[str, dict[str, Any]],
    per_stratum: int = plan.PAGES_PER_STRATUM,
) -> list[str]:
    """The pages the score will count, by its own rule, from the index alone.

    ``select_scored`` needs only whether each WCC visit finished, and the index says
    that, so WCC's results stay closed.
    """
    records = sorted(index.values(), key=lambda r: (r.stratum, r.position))
    candidates = [Candidate(r.stratum, r.position, r.origin, 0) for r in records]
    finished = {
        r.key: WccRecord(r.key, r.url, error=None if r.wcc_complete else "did not finish")
        for r in records
    }
    scored, _ = select_scored(candidates, index, finished, first, per_stratum)
    return [key_of(c) for c in scored]


def _second_annotators_pages(
    files: RunFiles, index: dict[str, CaptureRecord], annotator: str, per_stratum: int
) -> list[str]:
    """Check the second annotator may start, and draw from the scored pages."""
    if not files.labels.exists():
        raise SystemExit(f"{files.labels} does not exist; the first annotator labels first")
    document = json.loads(files.labels.read_text(encoding="utf-8"))
    if str(document.get("annotator", "")).strip().casefold() == annotator.strip().casefold():
        raise SystemExit(
            f"{files.labels} was written by {document['annotator']!r}; agreement needs "
            "a second person"
        )
    try:
        return agreement_keys(scored_keys(index, document["labels"], per_stratum))
    except NotReady as missing:
        raise SystemExit(f"the first annotator has not finished: {missing}") from missing


def _show(files: RunFiles, record: CaptureRecord, say: Callable[[str], None]) -> None:
    say("")
    say(f"=== {record.key}  {record.url}")
    say(f"    landed on {record.final_url}  (HTTP {record.http_status})  {record.title!r}")
    say(f"    screenshot: {(files.captures / record.screenshot).resolve()}")
    text_path = files.captures / record.text
    if text_path.exists():
        lines = [
            line for line in text_path.read_text(encoding="utf-8").splitlines() if line.strip()
        ]
        say(f"    text: {text_path.resolve()} (first lines below)")
        for line in lines[:25]:
            say(f"      | {line[:120]}")


def label_pages(
    files: RunFiles,
    annotator: str,
    *,
    second: bool = False,
    ask: Ask = input,
    say: Callable[[str], None] = print,
    per_stratum: int = plan.PAGES_PER_STRATUM,
) -> Path:
    index = load_index(files)
    captured = sorted(
        (r for r in index.values() if r.status == "captured"),
        key=lambda r: (r.stratum, r.position),
    )
    path = files.out / ("labels-second.json" if second else "labels.json")
    document: dict[str, Any] = (
        json.loads(path.read_text(encoding="utf-8"))
        if path.exists()
        else {
            "annotator": annotator,
            "blind_to_wcc": True,
            "started_at": _now(),
            "labels": {},
        }
    )
    if document["annotator"] != annotator:
        raise SystemExit(
            f"{path} was started by {document['annotator']!r}; one file, one annotator"
        )

    if second:
        # Kept from the first session on: a run resumed later moves the scored pages,
        # and the pages already labelled must stay the ones that were drawn.
        if "pages" not in document:
            document["pages"] = _second_annotators_pages(files, index, annotator, per_stratum)
        wanted = set(document["pages"])
        captured = [r for r in captured if r.key in wanted]

    for record in captured:
        if record.key in document["labels"]:
            continue
        _show(files, record, say)
        label = ask_page(ask)
        label["labelled_at"] = _now()
        document["labels"][record.key] = label
        path.write_text(json.dumps(document, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    say(f"\n{len(document['labels'])} pages labelled in {path}")
    return path


def label_all(out: Path, annotator: str, *, second: bool = False) -> int:
    print(
        "Label from the screenshot and text only. Do not open wcc/results.json\n"
        "or look at the live site; PLAN.md explains why."
    )
    label_pages(RunFiles(out), annotator, second=second)
    return 0
