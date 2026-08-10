"""Command-line entry point.

The command surface is fixed by specification 5.1 so that documentation and the
console script stay stable across phases. A command either works or names the
phase that will make it work; nothing here pretends to succeed.
"""

from __future__ import annotations

import argparse
import sys
from collections.abc import Sequence
from pathlib import Path

from wcc import __version__
from wcc.environment import run_doctor

EXIT_NOT_YET_IMPLEMENTED = 3
EXIT_ENVIRONMENT_UNFIT = 1

#: Commands this build actually performs.
IMPLEMENTED_COMMANDS = frozenset(
    {"install-browser", "doctor", "serve", "export", "import", "clear-data", "diagnostics"}
)

# Command -> phase that implements it, per specification section 13. Empty at
# Phase 5: every command specification 5.1 names now does something. The mapping
# stays because the convention it encodes — unimplemented surface names the phase
# that will implement it rather than failing vaguely — outlives this build.
PENDING_COMMANDS: dict[str, str] = {}


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

    export_parser = subparsers.add_parser("export", help="export capabilities of a page class")
    export_parser.add_argument(
        "--session",
        required=True,
        metavar="SESSION_ID",
        help=(
            "the session whose page class to export; a closed session still names "
            "one, because the templates outlive it"
        ),
    )
    export_parser.add_argument("--output", required=True, metavar="FILE")

    import_parser = subparsers.add_parser("import", help="import a portable capability file")
    import_parser.add_argument("file", metavar="FILE")

    clear_parser = subparsers.add_parser("clear-data", help="remove locally stored WCC data")
    clear_parser.add_argument(
        "--yes",
        action="store_true",
        help="skip the confirmation prompt (required when stdin is not a terminal)",
    )
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
    if args.command == "serve":
        return _serve()
    if args.command == "export":
        return _export(args.session, Path(args.output))
    if args.command == "import":
        return _import(Path(args.file))
    if args.command == "clear-data":
        return _clear_data(assume_yes=args.yes)
    if args.command == "diagnostics":
        return _diagnostics()

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


def _serve() -> int:
    """Run the MCP server over stdio until the client disconnects.

    Imported inside the function so that ``wcc doctor`` and ``wcc clear-data``
    keep working on a machine where the MCP SDK or Playwright is broken — which
    is exactly the machine those two commands exist for.
    """
    from wcc.mcp.server import serve

    return serve()


def _export(session_id: str, output: Path) -> int:
    """Write every template of the session's page class to a portable file.

    Specification 7.13 exports templates, never session-bound capabilities, so
    what a session identifies here is a *page class* — the origin and route it
    last observed. That is also why this works after the session has closed: the
    templates were always the durable half.
    """
    from wcc.portability.exporter import export_templates, write_file
    from wcc.store.store import Store

    with Store() as store:
        page = store.last_page_of(session_id)
        if page is None:
            print(
                f"no observation is stored for session {session_id}; "
                "inspect a page in that session before exporting it",
                file=sys.stderr,
            )
            return EXIT_ENVIRONMENT_UNFIT
        origin, route = page
        templates = store.find_templates(origin, route)

    if not templates:
        print(f"no templates are stored for {origin}{route}", file=sys.stderr)
        return EXIT_ENVIRONMENT_UNFIT

    write_file(output, export_templates(templates))
    print(f"wrote {len(templates)} template(s) for {origin}{route} to {output}")
    return 0


def _import(source: Path) -> int:
    """Check a capability file and store what survives.

    The checks that can be made without a page are made here; risk
    re-classification and locator re-resolution happen at bind time, against a
    real document, exactly as they do for a locally compiled template.
    """
    from wcc.portability.importer import ImportRefused, import_file
    from wcc.store.store import Store

    try:
        templates = import_file(source)
    except ImportRefused as refused:
        print(f"refused: {refused}", file=sys.stderr)
        return EXIT_ENVIRONMENT_UNFIT

    with Store() as store:
        stored = [store.save_template(template) for template in templates]
    print(f"imported {len(stored)} template(s) from {source}")
    print("They are stored, not trusted: each one re-resolves its targets and is")
    print("re-classified for risk against the live page before it can run.")
    return 0


def _clear_data(*, assume_yes: bool) -> int:
    """Remove every file WCC owns in its data directory (specification 18).

    Confirmation is required unless ``--yes`` is passed, and a non-interactive
    run without it refuses rather than assuming consent. This deletes compiled
    templates and their history, which no amount of re-running reconstructs.
    """
    from wcc.store.paths import DataDirectoryError, owned_paths, resolve_data_dir

    try:
        directory = resolve_data_dir()
        targets = [path for path in owned_paths() if path.exists()]
    except DataDirectoryError as unusable:
        print(str(unusable), file=sys.stderr)
        return EXIT_ENVIRONMENT_UNFIT

    if not targets:
        print(f"nothing to remove in {directory}")
        return 0

    if not assume_yes:
        if not sys.stdin.isatty():
            print(
                f"would remove {len(targets)} file(s) from {directory}. "
                "Re-run with --yes to confirm; refusing to assume consent from a "
                "non-interactive shell.",
                file=sys.stderr,
            )
            return EXIT_ENVIRONMENT_UNFIT
        print(f"This removes {len(targets)} file(s) from {directory}:")
        for path in targets:
            print(f"  {path.name}")
        if input("Remove them? [y/N] ").strip().lower() not in {"y", "yes"}:
            print("nothing was removed")
            return 0

    for path in targets:
        path.unlink()
    print(f"removed {len(targets)} file(s) from {directory}")
    return 0


def _diagnostics() -> int:
    """Print sanitized local counts and environment checks (specification 16).

    Counts and versions, never page content. Specification 16 says diagnostics
    "must not reveal private page content by default", and the cheapest way to
    keep that true is to have nothing here that reads a payload.
    """
    from wcc.config import build_enricher, enrichment_enabled, enrichment_model
    from wcc.store.database import SCHEMA_REVISION
    from wcc.store.paths import DataDirectoryError, resolve_data_dir
    from wcc.store.store import Store

    print(f"wcc {__version__}")
    print(f"database revision  {SCHEMA_REVISION}")
    # Whether page structure leaves this machine is the single most important
    # thing an operator can learn from diagnostics, so it is printed either way
    # rather than only when it is on.
    # Asked of a real enricher rather than of the variable that requests one, so
    # that "on" means a provider was actually built. Configured-but-broken is the
    # state worth naming: it is the one where someone believes it is working.
    if not enrichment_enabled():
        print("semantic enrichment  off (set WCC_ENRICHMENT=1 to enable)")
    elif build_enricher().enabled:
        print(f"semantic enrichment  on, via {enrichment_model()}")
    else:
        print("semantic enrichment  requested but unavailable; see the warning above")
    try:
        print(f"data directory     {resolve_data_dir()}")
        with Store() as store:
            counts = store.counts()
            width = max(len(name) for name in counts)
            for name, count in counts.items():
                print(f"{name.ljust(width)}  {count}")
    except DataDirectoryError as unusable:
        print(str(unusable), file=sys.stderr)
        return EXIT_ENVIRONMENT_UNFIT

    checks = run_doctor()
    print()
    for check in checks:
        print(f"{'ok  ' if check.ok else 'FAIL'}  {check.name}: {check.detail}")
    return 0 if all(check.ok for check in checks) else EXIT_ENVIRONMENT_UNFIT


if __name__ == "__main__":
    raise SystemExit(main())
