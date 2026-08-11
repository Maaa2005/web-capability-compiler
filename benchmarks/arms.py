"""The two tool surfaces being compared (specification 14.1).

This is the whole experiment. Both arms drive the same fixture in the same
browser through the same loop; the only thing that differs is what the model is
handed. The baseline arm is what an agent has today — navigate, look, click, type,
with the page arriving as text and elements named by selector. The WCC arm is the
six stable tools, where looking returns typed capabilities and acting names one of
them.

The baseline is written to be *fair rather than flattering*. It would be easy to
inflate it: dump raw HTML on every snapshot, or force a snapshot after every
click. Neither is what a competent low-level agent does, and a benchmark that
wins by making its opponent stupid measures nothing. So the baseline snapshot is
a trimmed, structured listing of the interactive elements and the visible text —
close to what Playwright MCP's accessibility snapshot provides — and the model
decides when to take one.

What the baseline cannot avoid is the shape of its own contract: element refs are
only valid for the snapshot that produced them, so every interaction is preceded
by a snapshot, and every snapshot is page-sized. That cost is the finding, not a
handicap imposed here.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Protocol

from playwright.async_api import Page

#: Trim so one snapshot cannot swamp a context window. Generous enough that the
#: fixture pages arrive whole — a truncated baseline would understate its cost,
#: which is the direction of bias this benchmark must avoid.
MAX_SNAPSHOT_CHARACTERS = 20_000


class Arm(Protocol):
    """One tool surface, as the loop sees it."""

    name: str

    def tools(self) -> list[dict[str, Any]]:
        """Tool definitions in Anthropic Messages API shape."""

    async def call(self, name: str, arguments: dict[str, Any]) -> str:
        """Run one tool and return what the model should see."""


# -- baseline: low-level browser tools ------------------------------------

BASELINE_TOOLS: list[dict[str, Any]] = [
    {
        "name": "browser_navigate",
        "description": "Navigate to a URL and return the new page's title and address.",
        "input_schema": {
            "type": "object",
            "properties": {"url": {"type": "string"}},
            "required": ["url"],
        },
    },
    {
        "name": "browser_snapshot",
        "description": (
            "Return the current page: its address, its visible text, and every "
            "interactive element with a ref you can pass to browser_click and "
            "browser_type. Refs are only valid until the page changes."
        ),
        "input_schema": {"type": "object", "properties": {}},
    },
    {
        "name": "browser_type",
        "description": "Type text into the element with this ref, from the latest snapshot.",
        "input_schema": {
            "type": "object",
            "properties": {"ref": {"type": "string"}, "text": {"type": "string"}},
            "required": ["ref", "text"],
        },
    },
    {
        "name": "browser_click",
        "description": "Click the element with this ref, from the latest snapshot.",
        "input_schema": {
            "type": "object",
            "properties": {"ref": {"type": "string"}},
            "required": ["ref"],
        },
    },
]


@dataclass
class BaselineArm:
    """Playwright-MCP-style tools over one page."""

    page: Page
    name: str = "baseline"

    def __post_init__(self) -> None:
        self._handles: dict[str, Any] = {}

    def tools(self) -> list[dict[str, Any]]:
        return BASELINE_TOOLS

    async def call(self, name: str, arguments: dict[str, Any]) -> str:
        if name == "browser_navigate":
            await self.page.goto(str(arguments["url"]), wait_until="domcontentloaded")
            return f"Navigated to {self.page.url}\nTitle: {await self.page.title()}"
        if name == "browser_snapshot":
            return await self._snapshot()
        if name in {"browser_click", "browser_type"}:
            return await self._interact(name, arguments)
        return f"Error: there is no tool named {name}"

    async def _snapshot(self) -> str:
        """List the interactive elements, keeping a handle for each.

        Handles rather than synthesized selectors. The first version built a CSS
        selector per element and fell back, for anything without an id or a name,
        to appending ``:nth-of-type`` to a comma-separated selector list — which
        binds to the last term only, so ``locator(...).first`` matched the first
        link on the page. A submit button became the "Home" link and the baseline
        arm quietly failed its own task. Playwright MCP holds handles; so does
        this.
        """
        handles = await self.page.query_selector_all(_INTERACTIVE)
        self._handles = {}
        described: list[str] = []
        for index, handle in enumerate(handles, start=1):
            ref = f"e{index}"
            self._handles[ref] = handle
            role, label = await handle.evaluate(_DESCRIBE)
            described.append(f"  {ref}: {role} {label!r}".rstrip())

        lines = [
            f"URL: {self.page.url}",
            f"Title: {await self.page.title()}",
            "",
            "Interactive elements:",
            *described,
            "",
            "Visible text:",
            await self.page.evaluate("() => (document.body.innerText || '').trim()"),
        ]
        rendered = "\n".join(lines)
        if len(rendered) > MAX_SNAPSHOT_CHARACTERS:
            # Reported rather than silently trimmed: a benchmark that quietly
            # shrank its baseline's input would under-report the baseline's cost.
            rendered = (
                rendered[:MAX_SNAPSHOT_CHARACTERS]
                + f"\n[snapshot truncated at {MAX_SNAPSHOT_CHARACTERS} characters]"
            )
        return rendered

    async def _interact(self, name: str, arguments: dict[str, Any]) -> str:
        handle = self._handles.get(str(arguments.get("ref")))
        if handle is None:
            return "Error: no such ref in the latest snapshot. Take a browser_snapshot first."
        if name == "browser_type":
            await handle.fill(str(arguments.get("text", "")))
            return f"Typed into {arguments['ref']}"
        await handle.click()
        await self.page.wait_for_load_state("domcontentloaded")
        return f"Clicked {arguments['ref']}. The page is now {self.page.url}"


#: What counts as something a low-level agent could act on.
_INTERACTIVE = "a[href], button, input:not([type=hidden]), select, textarea, [role=button]"

#: The role and accessible-ish name of one element, which is what Playwright MCP
#: hands over. Already more than WCC ever returns, and deliberately so: the
#: baseline is meant to be a fair opponent, not a weakened one.
_DESCRIBE = """
(el) => {
  const label = (
    el.getAttribute('aria-label') ||
    (el.labels && el.labels[0] && el.labels[0].textContent.trim()) ||
    el.getAttribute('placeholder') ||
    (el.textContent || '').trim().slice(0, 80) ||
    el.getAttribute('name') || ''
  );
  return [el.getAttribute('role') || el.tagName.toLowerCase(), label];
}
"""
