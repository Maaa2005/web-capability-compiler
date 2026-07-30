"""Phase 0 acceptance: the console script exposes a working ``--help``."""

from __future__ import annotations

import argparse

import pytest

from wcc.main import PENDING_COMMANDS, build_parser, main


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
    assert registered == set(PENDING_COMMANDS)


@pytest.mark.parametrize("command", ["serve", "doctor", "install-browser"])
def test_unimplemented_commands_report_their_phase(
    command: str, capsys: pytest.CaptureFixture[str]
) -> None:
    """An unimplemented command must fail loudly rather than appear to succeed."""
    assert main([command]) == 3
    assert PENDING_COMMANDS[command] in capsys.readouterr().err
