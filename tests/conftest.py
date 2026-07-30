"""Shared test fixtures.

The fixture server binds to the loopback interface only. Reaching it counts as the
explicit developer private-network allowance described in specification 10.2; it is
never enabled by default in the runtime.
"""

from __future__ import annotations

import functools
import http.server
import threading
from collections.abc import Iterator
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent
FIXTURES_DIR = REPO_ROOT / "fixtures"


class _QuietHandler(http.server.SimpleHTTPRequestHandler):
    def log_message(self, format: str, *args: object) -> None:  # noqa: A002
        """Keep pytest output readable."""


@pytest.fixture(scope="session")
def repo_root() -> Path:
    return REPO_ROOT


@pytest.fixture(scope="session")
def fixture_server() -> Iterator[str]:
    """Serve ``fixtures/`` over loopback and yield its base URL."""
    handler = functools.partial(_QuietHandler, directory=str(FIXTURES_DIR))
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    try:
        host, port = server.server_address[0], server.server_address[1]
        yield f"http://{host}:{port}"
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)
