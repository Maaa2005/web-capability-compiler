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

MVP v0.1 is under construction against spec v0.2.1. **Phases 0–6 of 7 are
complete, and it runs as an MCP server today.** The loop closes: WCC reads a live document into a snapshot, builds an
Agent Page IR, compiles typed search, filter, and navigation capabilities with
evidence and a deterministic risk classification, persists them as reusable
templates, binds a stored template on a later session without recompiling, and
executes one — validating arguments against its schema, re-resolving every target,
re-checking risk against the page in front of it, running the plan one allowlisted
action at a time, and verifying afterwards that the intended effect actually
happened.

All of that is reachable from any MCP client over stdio, through six stable tools.
An optional LLM pass can improve the names and descriptions, and is off by default —
nothing in the loop above depends on it. What remains is Phase 7, the
token-reduction benchmark.

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
| `WCC_ENRICHMENT` | Set to `1` to let an LLM improve capability names and descriptions. Off by default; needs `pip install 'wcc[llm]'` and `ANTHROPIC_API_KEY`. |
| `WCC_ENRICHMENT_MODEL` | Which model does that naming. Defaults to `claude-opus-5`. |

### Optional semantic enrichment

Off unless you turn it on, and the compiler is the product either way.

**Exactly what is sent**, and nothing else: the origin, the normalized route
pattern, and for each compiled capability its generated name, category, risk
level, side effect, and its parameters' names, types, and labels. Never the DOM,
a selector, the page's text, the page title, or a form value.

Two of those fields are written by the page — parameter labels, and a route
pattern that normalizes digits and UUIDs but not a tenant slug. **On an
authenticated site those go to a third party.** That is the trade; it is why this
is off by default.

**What comes back cannot do much.** A provider returns a capability name and a
short noun phrase; WCC composes the sentence around it, so model-authored prose
never reaches your agent directly. It cannot change a locator, an execution plan,
a category, or the shape of an argument schema, and a risk hint is only ever
applied upward. If the provider is unreachable, slow, or answers with something
invalid, the deterministic result is used unchanged and the reason is logged.

Responses are cached, keyed by the page signature together with the request, the
model, and the prompt version — so one page class costs one call, and changing any
of those is a new question rather than a stale hit.

## Benchmark

```bash
uv run python -m benchmarks.run --dry-run   # builds everything, calls nothing
uv run python -m benchmarks.run             # needs ANTHROPIC_API_KEY; costs money
```

Three arms — a baseline with Playwright-MCP-style low-level tools, WCC cold, and
WCC warm — driven by the same loop against the local fixture site, at least three
runs each. Token counts come from the provider's usage metadata; a run that fails
its verification condition is never credited with the tokens it saved by failing.
Raw runs are written to `benchmarks/results/`.

**No agent-loop results yet.** The harness is complete and tested against a
scripted client; the measuring run has not been made.

What *has* been measured, at no cost and with no API key, is the size of a single
observation:

```bash
uv run python -m benchmarks.measure_payloads
```

| Page | Size | Baseline snapshot | WCC inspection | WCC / baseline |
|---|---|---:|---:|---:|
| `search-page` | 6 controls | 563 | 1,787 | 3.17× |
| `large-catalog` | 150 products | 15,234 | 2,527 | **0.17×** |

Characters, counted exactly — not tokens, and not a token-reduction figure. (With
an API key the same command reports provider-counted tokens, and still costs
nothing: `count_tokens` runs no inference.) A
snapshot grows with the page; an inspection grows with the controls the page
offers, so the two diverge as pages get realistic. **WCC costs more on trivial
pages and about six times less on a 150-product catalog.**

What this does *not* show is how many observations a task takes, which is where
most of the saving is supposed to come from and which needs the agent loop above.
[docs/comparison.md](docs/comparison.md) states both halves.

## What this does not defend against

Stated plainly, because a security boundary that is described only by what it
catches is describing itself dishonestly.

**DNS rebinding.** The URL policy resolves a hostname and checks every address it
gets; Chromium then resolves the same hostname independently when it connects.
An attacker who controls a domain can answer the first resolution with a public
address and the second with `169.254.169.254`. WCC audits the peer address
Playwright reports and kills the session when it is private — but that happens
after the connection, so the request has already been sent, and a service that
never answers produces no response to audit at all.

No amount of re-checking inside the process closes this: it would only prove the
policy keeps getting the same answer. **If you are running WCC anywhere that
internal services are reachable, put it behind an egress proxy or a firewall that
blocks private, loopback, link-local, and metadata ranges.** The URL policy is a
guard against mistakes and obvious attacks; it is not a substitute for network
isolation.

Everything else in the same family was checked and is closed: decimal, octal and
hex IP forms, IPv4-mapped IPv6, IPv6 loopback and unspecified, `0.0.0.0`,
`localhost` and its aliases, trailing dots, credentials in the URL, non-HTTP
schemes, redirects to a blocked address, and subresource and `fetch` requests
made by the page.

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
