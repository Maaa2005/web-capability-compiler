# Web Capability Compiler (WCC)

**Compile existing websites into reusable, typed capabilities for MCP agents.**

WCC is not another browser-use agent. It is a compilation, binding, and caching
layer that sits between an existing website and an external AI agent.

Browser agents typically re-interpret low-level UI elements on every task, which
costs tokens, reduces repeatability, and ties workflows to transient DOM detail.
WCC inspects a page once, compiles the observable controls into a small set of
semantic, typed, low-risk capability templates, binds compatible templates to the
current session, and lets an MCP client execute them through a stable interface.
On repeated use it tries to bind a persisted template before recompiling, so the
agent can skip low-level DOM interpretation entirely.

The headline metric is **token reduction on repeated tasks**, not task success rate.

## Status

MVP v0.1 is under construction against spec v0.2.1. **Phases 0–5 of 7 are
complete, and it runs as an MCP server today.** The loop closes: WCC reads a live document into a snapshot, builds an
Agent Page IR, compiles typed search, filter, and navigation capabilities with
evidence and a deterministic risk classification, persists them as reusable
templates, binds a stored template on a later session without recompiling, and
executes one — validating arguments against its schema, re-resolving every target,
re-checking risk against the page in front of it, running the plan one allowlisted
action at a time, and verifying afterwards that the intended effect actually
happened.

All of that is reachable from any MCP client over stdio, through six stable tools.
What remains is Phase 6 (optional LLM enrichment, which the system works without)
and Phase 7 (the token-reduction benchmark).

Three properties are worth stating plainly, because they are what the design is
for: only `low`-risk capabilities ever execute; an execution is reported as
successful only when its effect was observed; and no selector, screenshot, or
coordinate crosses the MCP boundary in either direction — a test asserts the last
one by searching every tool response for the fixtures' own selectors.

See [`docs/progress.md`](docs/progress.md) for the phase log, which records the
reasoning behind each decision and the defects found along the way.

## Quickstart

No clone required. [uv](https://docs.astral.sh/uv/) and Python 3.12+ are the only
prerequisites.

```bash
# Install the Chromium build the driver uses (once)
uvx --from git+https://github.com/Maaa2005/web-capability-compiler.git wcc install-browser

# Check the machine before pointing a client at it
uvx --from git+https://github.com/Maaa2005/web-capability-compiler.git wcc doctor

# Run the MCP server over stdio
uvx --from git+https://github.com/Maaa2005/web-capability-compiler.git wcc serve
```

### Connecting an MCP client

**Claude Code:**

```bash
claude mcp add wcc -- uvx --from git+https://github.com/Maaa2005/web-capability-compiler.git wcc serve
```

**Claude Desktop** (`claude_desktop_config.json`) **or any client that takes a
command:**

```json
{
  "mcpServers": {
    "wcc": {
      "command": "uvx",
      "args": [
        "--from",
        "git+https://github.com/Maaa2005/web-capability-compiler.git",
        "wcc",
        "serve"
      ]
    }
  }
}
```

Then ask the agent to open a page and inspect it. It will get back a list of typed
capabilities rather than a DOM.

### The six tools

| Tool | What it does |
|---|---|
| `open_session` | Open a browser session on a URL, subject to the URL policy |
| `inspect_page` | Compile the page — or rebind a stored template — and report capabilities, entities, and refusals |
| `list_capabilities` | Filter what the inspection found by category and maximum risk |
| `execute_capability` | Run one capability and return the complete verified result |
| `get_execution_result` | Retrieve a stored result, only for the session that produced it |
| `close_session` | Close the session; idempotent |

Generated capabilities are returned as typed data, not registered as MCP tools —
so the client's tool list never changes as the agent browses.

## Configuration

Set by whoever launches the server, never by an agent over the wire.

| Variable | Effect |
|---|---|
| `WCC_DATA_DIR` | Where templates and history are stored. Absolute paths only; defaults to the OS per-user data directory. |
| `WCC_ALLOW_PRIVATE_NETWORK` | Set to `1` to allow loopback and private addresses. **Development only** — it switches off the SSRF boundary of specification 10.2. |

## Operating it

```bash
uv run wcc diagnostics                        # sanitized counts and environment checks
uv run wcc export --session <id> --output caps.json
uv run wcc import caps.json                   # checked on the way in, re-verified at bind time
uv run wcc clear-data                         # remove all local WCC data
```

An imported capability is stored, never trusted: it re-resolves its targets and is
re-classified for risk against the live page before it can run, exactly as a
locally compiled one is.

## Development

```bash
uv sync
uv run wcc --help
./scripts/test.sh
```

## Scope of MVP v0.1

Supported capability categories are `search`, `filter`, and `navigate`. Only `low`
risk capabilities are executable. Purchases, account changes, message sending, and
any state-changing form submission are out of scope, as are CAPTCHA bypass,
anti-bot evasion, and credential storage.

The full scope, data model, risk model, and benchmark methodology live in
[`docs/spec/WCC_AGENT_IMPLEMENTATION_SPEC_v0.2.1.md`](docs/spec/WCC_AGENT_IMPLEMENTATION_SPEC_v0.2.1.md),
which is the source of truth for implementation.

## License

MIT
