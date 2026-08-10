"""The console script: a working ``--help``, and commands that do not pretend."""

from __future__ import annotations

import argparse
import sys
from pathlib import Path

import pytest

from wcc.driver.environment import (
    BrowserStatus,
    install_chromium,
    parse_install_location,
    status_command,
)
from wcc.environment import Check, check_playwright_package, check_python, run_doctor
from wcc.main import IMPLEMENTED_COMMANDS, PENDING_COMMANDS, build_parser, main


def test_help_exits_cleanly(capsys: pytest.CaptureFixture[str]) -> None:
    with pytest.raises(SystemExit) as excinfo:
        main(["--help"])
    assert excinfo.value.code == 0
    assert "Web Capability Compiler" in capsys.readouterr().out


def test_bare_invocation_prints_help(capsys: pytest.CaptureFixture[str]) -> None:
    assert main([]) == 0
    assert "usage: wcc" in capsys.readouterr().out


def test_documented_command_surface_is_registered() -> None:
    """Specification 5.1 fixes the command list; keep the parser in step with it."""
    parser = build_parser()
    registered = {
        choice
        for action in parser._actions  # noqa: SLF001
        if isinstance(action, argparse._SubParsersAction)  # noqa: SLF001
        for choice in action.choices
    }
    assert registered == set(PENDING_COMMANDS) | set(IMPLEMENTED_COMMANDS)


def test_no_command_is_both_pending_and_implemented() -> None:
    """A command left in both lists would report a phase it has already passed."""
    assert not set(PENDING_COMMANDS) & set(IMPLEMENTED_COMMANDS)


def test_every_command_the_specification_names_is_implemented() -> None:
    """Phase 5 completes the surface specification 5.1 fixes.

    The pending-command mechanism stays even though it is now empty: the
    convention it encodes — unimplemented surface names the phase that will
    implement it, rather than failing vaguely — is what kept the CLI honest for
    five phases, and the next specification version will need it again.
    """
    assert PENDING_COMMANDS == {}


# -- T9: install-browser and doctor -------------------------------------------


_DRY_RUN_OUTPUT = """\
Chrome for Testing 151.0.7922.34 (playwright chromium v1234)
  Install location:    /caches/ms-playwright/chromium-1234
  Download url:        https://example.invalid/chromium.zip

FFmpeg (playwright ffmpeg v1011)
  Install location:    /caches/ms-playwright/ffmpeg-1011

Chrome Headless Shell 151.0.7922.34 (playwright chromium-headless-shell v1234)
  Install location:    /caches/ms-playwright/chromium_headless_shell-1234
"""


def test_the_chromium_location_is_told_apart_from_the_headless_shell() -> None:
    """They are separate downloads, and only one of them is the browser we launch."""
    location = parse_install_location(_DRY_RUN_OUTPUT)
    assert location is not None
    assert location.name == "chromium-1234"


def test_an_unrecognised_installer_format_reports_nothing_rather_than_guessing() -> None:
    assert parse_install_location("Playwright rewrote its output format") is None


def test_install_browser_targets_the_running_interpreter() -> None:
    """A bare ``playwright`` on PATH can belong to a different environment."""
    issued: list[list[str]] = []

    def runner(command: object) -> int:
        issued.append(list(command))  # type: ignore[arg-type]
        return 0

    assert install_chromium(runner) == 0
    assert issued == [[sys.executable, "-m", "playwright", "install", "chromium"]]
    assert status_command()[0] == sys.executable


def test_a_missing_build_is_reported_rather_than_raised(monkeypatch: pytest.MonkeyPatch) -> None:
    from wcc.driver import environment

    def reader(command: object) -> tuple[int, str]:
        del command
        return 0, _DRY_RUN_OUTPUT

    status = environment.chromium_status(reader)
    assert status.installed is False
    assert "wcc install-browser" in status.detail
    monkeypatch.undo()


def test_an_installer_that_fails_is_reported_rather_than_raised() -> None:
    from wcc.driver import environment

    status = environment.chromium_status(lambda command: (1, "no such option: --dry-run"))
    assert status.installed is False
    assert "reported an error" in status.detail


def test_doctor_checks_every_question_before_answering() -> None:
    """Stopping at the first problem turns one fix into several round trips."""
    names = [check.name for check in run_doctor()]
    assert names == ["python", "playwright package", "chromium build"]


def test_the_running_interpreter_passes_its_own_version_check() -> None:
    assert check_python().ok is True
    assert check_playwright_package().ok is True


def test_doctor_exits_nonzero_when_something_is_wrong(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import wcc.main

    monkeypatch.setattr(
        wcc.main,
        "run_doctor",
        lambda: [
            Check("python", True, "fine"),
            Check("chromium build", False, "no Chromium build; run 'wcc install-browser'"),
        ],
    )
    assert main(["doctor"]) == 1

    captured = capsys.readouterr()
    assert "FAIL  chromium build" in captured.out
    assert "wcc install-browser" in captured.out


def test_doctor_exits_zero_when_the_environment_is_fit(
    monkeypatch: pytest.MonkeyPatch, capsys: pytest.CaptureFixture[str]
) -> None:
    import wcc.main

    monkeypatch.setattr(wcc.main, "run_doctor", lambda: [Check("python", True, "fine")])
    assert main(["doctor"]) == 0
    assert "FAIL" not in capsys.readouterr().out


def test_install_browser_passes_the_installers_exit_code_through(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """A failed download must not look like a successful install."""
    from wcc.driver import environment

    monkeypatch.setattr(environment, "install_chromium", lambda: 7)
    assert main(["install-browser"]) == 7


def test_browser_status_reports_a_present_build(tmp_path: pytest.TempPathFactory) -> None:
    from wcc.driver import environment

    present = Path(str(tmp_path)) / "chromium-1234"
    present.mkdir()
    output = f"Chrome for Testing (playwright chromium v1234)\n  Install location:    {present}\n"

    status = environment.chromium_status(lambda command: (0, output))
    assert status == BrowserStatus(True, str(present))
