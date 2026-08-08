"""What ``wcc doctor`` checks, as data rather than as printed text.

Each check returns a result instead of writing to the terminal, so the same set
can be rendered by the CLI now and by ``wcc diagnostics`` in Phase 5 without the
checks being written twice — and so a test can assert on outcomes rather than on
string formatting.

Every check reports rather than raises, including the ones that fail. A doctor
that stops at the first problem tells the user about one of the three things wrong
with their machine, which turns one fix into three round trips.
"""

from __future__ import annotations

import importlib.util
import sys
from dataclasses import dataclass

#: Matches ``requires-python`` in ``pyproject.toml``. Duplicated deliberately: the
#: installed package cannot read its own source metadata reliably, and a check
#: that silently stopped checking would be worse than one that needs updating
#: alongside the two-line version bump.
MINIMUM_PYTHON = (3, 12)


@dataclass(frozen=True)
class Check:
    """One environment question and its answer."""

    name: str
    ok: bool
    detail: str


def check_python() -> Check:
    version = sys.version_info
    running = f"{version.major}.{version.minor}.{version.micro}"
    minimum = ".".join(str(part) for part in MINIMUM_PYTHON)
    if version[: len(MINIMUM_PYTHON)] < MINIMUM_PYTHON:
        return Check("python", False, f"{running} is older than the required {minimum}")
    return Check("python", True, f"{running} (minimum {minimum})")


def check_playwright_package() -> Check:
    """Look for the package without importing it.

    ``find_spec`` answers the question that matters — is it installed — without
    executing the package, which keeps this module free of the Playwright import
    that specification 4.1 confines to ``wcc.driver``.
    """
    if importlib.util.find_spec("playwright") is None:
        return Check("playwright package", False, "not installed; run 'uv sync'")
    return Check("playwright package", True, "installed")


def check_browser() -> Check:
    """Ask the driver package whether the browser build is there."""
    if importlib.util.find_spec("playwright") is None:
        return Check("chromium build", False, "cannot be checked without the playwright package")

    from wcc.driver.environment import chromium_status

    status = chromium_status()
    return Check("chromium build", status.installed, status.detail)


def run_doctor() -> list[Check]:
    """Run every check, in the order a user would have to fix them."""
    return [check_python(), check_playwright_package(), check_browser()]
