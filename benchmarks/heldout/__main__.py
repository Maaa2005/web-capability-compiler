"""``uv run python -m benchmarks.heldout {sample,run,label,score}``.

The four steps of ``PLAN.md``, in the order they must happen. Each step writes a
file the next one reads, so the order is visible in what gets committed.
"""

from __future__ import annotations

import argparse
import io
import json
import sys
from pathlib import Path


def _printing_never_fails() -> None:
    """Page titles and reports hold characters a Windows console's cp932 lacks."""
    for stream in (sys.stdout, sys.stderr):
        if isinstance(stream, io.TextIOWrapper):
            stream.reconfigure(errors="backslashreplace")


def main(argv: list[str] | None = None) -> int:
    _printing_never_fails()
    parser = argparse.ArgumentParser(prog="python -m benchmarks.heldout", description=__doc__)
    steps = parser.add_subparsers(dest="step", required=True)

    sample = steps.add_parser("sample", help="draw the candidates from the CrUX frame file")
    sample.add_argument("frame", type=Path, help="data/global/202609.csv.gz from crux-top-lists")

    blocklist = steps.add_parser(
        "blocklist", help="write E5's list: the frame's hosts that UT1 marks adult or gambling"
    )
    blocklist.add_argument("frame", type=Path, help="the CrUX frame file")
    blocklist.add_argument("ut1", type=Path, help="a folder holding <category>.domains files")

    run = steps.add_parser("run", help="visit candidates: robots.txt, capture, WCC")
    run.add_argument("--out", type=Path, required=True, help="the run's directory")
    run.add_argument("--stratum", default="", help="only this stratum (default: all)")

    label = steps.add_parser("label", help="label the captures, without WCC's results")
    label.add_argument("--out", type=Path, required=True, help="the run's directory")
    label.add_argument("--annotator", required=True, help="who is labelling")
    label.add_argument("--second", action="store_true", help="the optional agreement check")

    score = steps.add_parser("score", help="score WCC's results against the labels")
    score.add_argument("--out", type=Path, required=True, help="the run's directory")

    arguments = parser.parse_args(argv)
    if arguments.step == "blocklist":
        from benchmarks.heldout import plan
        from benchmarks.heldout.sample import BLOCKLIST_PATH, build_frame_blocklist, read_frame

        files = {name: arguments.ut1 / f"{name}.domains" for name in plan.UT1_CATEGORIES}
        hosts = build_frame_blocklist(read_frame(arguments.frame), files)
        BLOCKLIST_PATH.write_text("\n".join(hosts) + "\n", encoding="utf-8", newline="\n")
        print(f"{len(hosts)} hosts -> {BLOCKLIST_PATH}")
        return 0
    if arguments.step == "sample":
        from benchmarks.heldout import plan
        from benchmarks.heldout.sample import (
            BLOCKLIST_PATH,
            SAMPLE_PATH,
            draw,
            read_blocklist,
            read_frame,
            sample_document,
        )

        blocklist = read_blocklist(BLOCKLIST_PATH, plan.BLOCKLISTS[0].sha256)
        result = draw(read_frame(arguments.frame), blocklist=blocklist)
        SAMPLE_PATH.write_text(
            json.dumps(sample_document(result), indent=2) + "\n", encoding="utf-8", newline="\n"
        )
        print(f"{len(result.candidates)} candidates -> {SAMPLE_PATH}")
        print(f"excluded while drawing: {dict(sorted(result.excluded.items()))}")
        return 0
    if arguments.step == "run":
        from benchmarks.heldout.run import run_all

        return run_all(arguments.out, only_stratum=arguments.stratum)
    if arguments.step == "label":
        from benchmarks.heldout.label import label_all

        return label_all(arguments.out, arguments.annotator, second=arguments.second)
    from benchmarks.heldout.score import score_run

    return score_run(arguments.out)


if __name__ == "__main__":
    sys.exit(main())
