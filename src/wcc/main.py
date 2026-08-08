"""Command-line entry point.

The command surface is fixed by specification 5.1 so that documentation and the
console script stay stable across phases. A command either works or names the
phase that will make it work; nothing here pretends to succeed.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence

from wcc import __version__
from wcc.environment import run_doctor

EXIT_NOT_YET_IMPLEMENTED = 3
EXIT_ENVIRONMENT_UNFIT = 1

#: Commands this build actually performs.
IMPLEMENTED_COMMANDS = frozenset({"install-browser", "doctor"})

# Command -> phase that implements it, per specification section 13.
PENDING_COMMANDS: dict[str, str] = {
    "serve": "Phase 5 (MCP server over stdio)",
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

    if args.command == "install-browser":
        return _install_browser()
    if args.command == "doctor":
        return _doctor()

    phase = PENDING_COMMANDS[args.command]
    print(
        f"wcc {args.command} is not implemented yet; it lands in {phase}.",
        file=sys.stderr,
    )
    return EXIT_NOT_YET_IMPLEMENTED


def _install_browser() -> int:
    """Install the browser build, reporting the installer's own exit code."""
    from wcc.driver.environment import install_chromium

    return install_chromium()


def _doctor() -> int:
    """Print every check, then fail if any of them did.

    The exit code is the machine-readable half of the answer: a setup script can
    branch on it without parsing the lines a human reads.
    """
    checks = run_doctor()
    width = max(len(check.name) for check in checks)
    for check in checks:
        marker = "ok  " if check.ok else "FAIL"
        print(f"{marker}  {check.name.ljust(width)}  {check.detail}")

    if all(check.ok for check in checks):
        return 0
    print("\nwcc doctor found problems; the lines marked FAIL say what to fix.", file=sys.stderr)
    return EXIT_ENVIRONMENT_UNFIT


if __name__ == "__main__":
    raise SystemExit(main())
