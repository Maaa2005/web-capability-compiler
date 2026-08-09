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

MVP v0.1 is under construction against spec v0.2.1. **Phases 0–3 of 7 are
complete.** A page can now be opened, observed, and compiled: WCC reads a live
document into a snapshot, builds an Agent Page IR from it, and produces typed
search, filter, and navigation capabilities with evidence, confidence, and a
deterministic risk classification, persisting them as reusable templates.

Nothing executes yet. A capability is compiled and bound, but running one — argument
validation, stepwise allowlisted actions, and post-action verification — is Phase 4,
and the MCP server that would expose all of this to an agent is Phase 5.

See [`docs/progress.md`](docs/progress.md) for the phase log.

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
