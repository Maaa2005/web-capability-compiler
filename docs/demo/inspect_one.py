"""One page in, one typed capability list out — the demo the GIF records.

``uv run python docs/demo/inspect_one.py``

This exists so the recording in ``docs/demo/demo.tape`` shows the real thing. It
serves the repository's own fixture over loopback, opens it through the same
coordinator the MCP server uses, and prints the inspection. Nothing is staged: if
the compiler stops finding the search form, this stops printing it, and
re-recording the tape produces a GIF that says so.

The output is deliberately trimmed for legibility on a terminal-sized image —
identifiers and evidence counts are dropped, because a demo has to fit on a
screen. It is trimming, not editing: no field shown here is altered, and the
untrimmed response is what ``inspect_page`` returns.
"""

from __future__ import annotations

import asyncio
import functools
import http.server
import threading
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from tempfile import TemporaryDirectory

from wcc.driver.base import DriverSettings
from wcc.driver.playwright_driver import PlaywrightDriver
from wcc.enrich.enricher import Enricher
from wcc.mcp.views import capability_view, inspection_view
from wcc.models.session import OpenSessionRequest
from wcc.policy.url_policy import UrlPolicy
from wcc.runtime.coordinator import Coordinator
from wcc.store.store import Store

FIXTURES = Path(__file__).resolve().parent.parent.parent / "fixtures"

GREEN = "\033[32m"
RED = "\033[31m"
DIM = "\033[2m"
BOLD = "\033[1m"
OFF = "\033[0m"


class _Quiet(http.server.SimpleHTTPRequestHandler):
    def log_message(self, format: str, *args: object) -> None:  # noqa: A002
        """The demo shows WCC's output, not the fixture server's access log."""


@contextmanager
def fixture_server() -> Iterator[str]:
    handler = functools.partial(_Quiet, directory=str(FIXTURES))
    server = http.server.ThreadingHTTPServer(("127.0.0.1", 0), handler)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        yield f"http://{server.server_address[0]}:{server.server_address[1]}"
    finally:
        server.shutdown()
        server.server_close()


async def main() -> None:
    with fixture_server() as base_url, TemporaryDirectory() as data_directory:
        url = f"{base_url}/search-page/"
        print(f"{DIM}$ inspect {url}{OFF}\n")

        driver = PlaywrightDriver(UrlPolicy(), DriverSettings(allow_private_network=True))
        store = Store(Path(data_directory) / "wcc.sqlite3")
        try:
            coordinator = Coordinator(driver, store, enricher=Enricher())
            session = await coordinator.open_session(OpenSessionRequest(url=url))
            inspection = await coordinator.inspect_page(session.session_id)
            view = inspection_view(
                inspection.snapshot,
                inspection.ir,
                [
                    capability_view(binding, inspection.templates[binding.template_id])
                    for binding in inspection.capabilities
                ],
                reused=inspection.reused,
                report=inspection.report,
            )
        finally:
            store.close()
            await driver.shutdown()

    print(f"{BOLD}{GREEN}capabilities{OFF} — typed, with a schema, safe to run\n")
    for capability in view.capabilities:
        print(f"  {GREEN}✓{OFF} {BOLD}{capability.name}{OFF}  {DIM}({capability.category}){OFF}")
        print(f"    risk: {capability.risk.level}  side effect: {capability.risk.side_effect}")
        properties = capability.parameters.get("properties", {})
        if isinstance(properties, dict):
            for name, schema in properties.items():
                kind = schema.get("type", "?") if isinstance(schema, dict) else "?"
                note = schema.get("description", "") if isinstance(schema, dict) else ""
                print(f"      {name}: {kind}  {DIM}{note}{OFF}")
        print()

    print(f"{BOLD}{RED}refused{OFF} — reported so the agent knows, never offered\n")
    for refusal in view.refused:
        level = refusal.risk.level if refusal.risk else "unknown"
        print(f"  {RED}✗{OFF} {refusal.candidate_key}  {DIM}risk: {level}{OFF}")
        print(f"    {refusal.message}")
    print()

    payload = view.model_dump_json()
    for selector in ("product-search", "#keyword", "form>input"):
        assert selector not in payload, selector
    print(f"{DIM}no selector, structural path, or coordinate appears above —{OFF}")
    print(f"{DIM}checked against this page's own, as the test suite does{OFF}")


if __name__ == "__main__":
    asyncio.run(main())
