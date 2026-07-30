"""Phase 0 acceptance: only ``wcc.driver`` may reach Playwright.

Specification 4.1 puts all browser access behind the ``Driver`` interface so the
post-MVP Chrome-extension driver can replace Playwright without touching the core.
An AST walk is used rather than a text search so that a mention inside a docstring
or comment does not register as a violation.
"""

from __future__ import annotations

import ast
from pathlib import Path

import pytest

SOURCE_ROOT = Path(__file__).resolve().parent.parent / "src" / "wcc"
DRIVER_PACKAGE = SOURCE_ROOT / "driver"


def _imported_modules(tree: ast.AST) -> set[str]:
    modules: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            modules.update(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module and node.level == 0:
            modules.add(node.module)
    return modules


def _runtime_modules() -> list[Path]:
    return [
        path for path in sorted(SOURCE_ROOT.rglob("*.py")) if DRIVER_PACKAGE not in path.parents
    ]


@pytest.mark.parametrize("path", _runtime_modules(), ids=lambda p: p.name)
def test_runtime_module_does_not_import_playwright(path: Path) -> None:
    tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
    offenders = {m for m in _imported_modules(tree) if m.split(".")[0] == "playwright"}
    assert not offenders, f"{path} imports {sorted(offenders)}; use the Driver interface"


def test_the_guard_actually_scans_something() -> None:
    """A rename that empties the scan list would make the guard silently vacuous."""
    assert _runtime_modules()
