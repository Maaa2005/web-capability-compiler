"""Command-line entry point.

The command surface is fixed by specification 5.1 so that documentation and the
console script stay stable across phases. Phase 0 implements argument parsing and
``--help`` only; every other command reports the phase that will implement it
instead of pretending to work.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence

from wcc import __version__

EXIT_NOT_YET_IMPLEMENTED = 3

# Command -> phase that implements it, per specification section 13.
PENDING_COMMANDS: dict[str, str] = {
    "serve": "Phase 5 (MCP server over stdio)",
    "install-browser": "Phase 1 (Playwright driver)",
    "doctor": "Phase 1 (environment validation)",
    "export": "Phase 5 (portable capability files)",
    "import": "Phase 5 (portable capability files)",
    "clear-data": "Phase 5 (operational CLI)",
    "diagnostics": "Phase 5 (operational CLI)",
}


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="wcc",
        description=(
            "Web Capability Compiler: compile existing websites into reusable, "
            "typed capabilities for MCP agents."
        ),
    )
    parser.add_argument("--version", action="version", version=f"wcc {__version__}")

    subparsers = parser.add_subparsers(dest="command", metavar="command")
    subparsers.add_parser("serve", help="run the MCP server over stdio")
    subparsers.add_parser("install-browser", help="install the required Chromium build")
    subparsers.add_parser("doctor", help="validate the local environment")

    export_parser = subparsers.add_parser("export", help="export capabilities of a session")
    export_parser.add_argument("--session", required=True, metavar="SESSION_ID")
    export_parser.add_argument("--output", required=True, metavar="FILE")

    import_parser = subparsers.add_parser("import", help="import a portable capability file")
    import_parser.add_argument("file", metavar="FILE")

    subparsers.add_parser("clear-data", help="remove locally stored WCC data")
    subparsers.add_parser("diagnostics", help="print local diagnostics")

    return parser


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)

    if args.command is None:
        parser.print_help()
        return 0

    phase = PENDING_COMMANDS[args.command]
    print(
        f"wcc {args.command} is not implemented yet; it lands in {phase}.",
        file=sys.stderr,
    )
    return EXIT_NOT_YET_IMPLEMENTED


if __name__ == "__main__":
    raise SystemExit(main())
