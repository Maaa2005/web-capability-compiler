"""The environment questions only the browser backend can answer.

This lives in ``driver/`` for the same reason everything else here does: whether a
Chromium build exists is a fact about Playwright, and specification 4.1 keeps
Playwright behind this package so a future Chrome-extension driver can answer the
same question differently without the CLI changing.

Nothing here launches a browser, and nothing here imports Playwright. ``wcc doctor``
runs on machines where the browser is missing or broken, which is precisely when
launching one would hang or crash instead of reporting the problem — so the
questions are put to Playwright's own installer as a subprocess, and the answers
are read off its output.
"""

from __future__ import annotations

import subprocess
import sys
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from pathlib import Path

#: Injectable so a test can prove the right command is issued without spending a
#: few hundred megabytes of download to find out.
Runner = Callable[[Sequence[str]], int]

#: Same idea, for the command whose output is read rather than watched.
Reader = Callable[[Sequence[str]], tuple[int, str]]


@dataclass(frozen=True)
class BrowserStatus:
    """Whether the required browser build is usable, and how it is known."""

    installed: bool
    detail: str


def _run(command: Sequence[str]) -> int:
    # Output is not captured: an install is slow, and a progress bar the user can
    # watch is worth more than a transcript printed once it is already over.
    return subprocess.run(list(command), check=False).returncode


def _read(command: Sequence[str]) -> tuple[int, str]:
    completed = subprocess.run(
        list(command), check=False, capture_output=True, text=True, timeout=120
    )
    return completed.returncode, completed.stdout + completed.stderr


def install_command() -> list[str]:
    """The command that installs the browser build.

    ``sys.executable -m`` rather than a bare ``playwright``: the console script may
    be missing from ``PATH`` in a virtual environment that was never activated, and
    installing into a different interpreter than the one running is the failure
    this avoids.
    """
    return [sys.executable, "-m", "playwright", "install", "chromium"]


def install_chromium(runner: Runner = _run) -> int:
    """Install the Chromium build Playwright expects, returning its exit code."""
    return runner(install_command())


#: Names the Chromium entry in ``playwright install --dry-run`` output and, by
#: being this exact, excludes ``chromium-headless-shell``, which is a separate
#: download that the driver never launches.
_CHROMIUM_MARKER = "(playwright chromium v"
_LOCATION_LABEL = "Install location:"


def status_command() -> list[str]:
    """The command that reports where the browser build would live."""
    return [sys.executable, "-m", "playwright", "install", "--dry-run", "chromium"]


def parse_install_location(output: str) -> Path | None:
    """Pull Chromium's install location out of ``--dry-run`` output.

    Parsing a CLI is not free of risk, so the failure mode is chosen: an
    unrecognised format returns ``None`` and the caller reports that it could not
    tell, rather than guessing a path and declaring the browser missing.
    """
    lines = output.splitlines()
    for index, line in enumerate(lines):
        if _CHROMIUM_MARKER not in line:
            continue
        for following in lines[index + 1 :]:
            if _LOCATION_LABEL in following:
                return Path(following.split(_LOCATION_LABEL, 1)[1].strip())
            if following.strip() and not following.startswith(" "):
                break  # the next entry started; this one had no location
    return None


def chromium_status(reader: Reader | None = None) -> BrowserStatus:
    """Report whether the Chromium build is present, without starting it.

    Asking Playwright's own installer where the build belongs, then looking, is
    what keeps this honest across platforms and cache locations. Starting a
    browser to find out would fail on exactly the machines this is meant to
    diagnose, and reimplementing Playwright's path layout here would drift the
    first time it changed.
    """
    read = reader or _read
    try:
        code, output = read(status_command())
    except OSError as exc:
        return BrowserStatus(False, f"could not run the Playwright installer: {exc}")
    if code != 0:
        return BrowserStatus(False, f"the Playwright installer reported an error: {output.strip()}")

    location = parse_install_location(output)
    if location is None:
        return BrowserStatus(False, "could not tell where Playwright keeps its Chromium build")
    if not location.exists():
        return BrowserStatus(False, f"no Chromium build at {location}; run 'wcc install-browser'")
    return BrowserStatus(True, str(location))
