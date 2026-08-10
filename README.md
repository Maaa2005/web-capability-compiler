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

MVP v0.1 is under construction against spec v0.2.1. **Phases 0–4 of 7 are
complete.** The loop closes: WCC reads a live document into a snapshot, builds an
Agent Page IR, compiles typed search, filter, and navigation capabilities with
evidence and a deterministic risk classification, persists them as reusable
templates, binds a stored template on a later session without recompiling, and
executes one — validating arguments against its schema, re-resolving every target,
re-checking risk against the page in front of it, running the plan one allowlisted
action at a time, and verifying afterwards that the intended effect actually
happened.

What is missing is the interface. Everything above is reachable from Python and not
yet from an MCP client; the six stable MCP tools, template export and import, and
the operational CLI are Phase 5.

Two properties are worth stating plainly, because they are what the design is for:
only `low`-risk capabilities ever execute, and an execution is reported as
successful only when its effect was observed.

See [`docs/progress.md`](docs/progress.md) for the phase log, which records the
reasoning behind each decision and the defects found along the way.

## Requirements

- Python 3.12 or later
- [uv](https://docs.astral.sh/uv/)

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
