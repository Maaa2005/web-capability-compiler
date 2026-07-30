# Instructions for coding agents

The implementation source of truth is
[`docs/spec/WCC_AGENT_IMPLEMENTATION_SPEC_v0.2.1.md`](docs/spec/WCC_AGENT_IMPLEMENTATION_SPEC_v0.2.1.md).
Read it before changing anything. This file summarizes Section 0 of that document;
where the two disagree, the specification wins.

Implement WCC in the phase order of specification Section 13. Do not broaden the
scope without an explicit instruction from the project owner.

## Rules

1. Complete one phase and its acceptance criteria before starting the next phase.
2. Prefer deterministic parsing over LLM inference whenever the same result can be
   obtained from HTML, ARIA, JSON-LD, or browser state.
3. Do not implement irreversible actions such as purchase confirmation, account
   deletion, message sending, or state-changing form submission in MVP v0.1.
4. Never execute a capability whose risk level is above `low`. Higher levels may be
   listed but must be blocked.
5. Preserve evidence for every generated entity and capability. The system must be
   able to explain which DOM element or page signal produced each result.
6. Add or update tests with every implementation change.
7. Do not silently change schemas. Schema changes require a version increment and
   migration notes.
8. Do not add an independent autonomous agent. WCC is a compilation and execution
   layer used by external agents.
9. Keep the LLM provider optional and replaceable. Core extraction and execution
   must run without an LLM.
10. All browser access goes through the `Driver` interface. Do not call Playwright
    APIs outside `src/wcc/driver/`.
11. At the end of each phase, update `docs/progress.md` with completed work, test
    results, unresolved issues, and the next phase.
12. Do not collapse `CapabilityTemplate` and `BoundCapability` into one model.
    Templates are portable and reusable; bound capabilities are session-scoped.
13. Do not treat a structural hash as a reusable page signature. Implement the three
    fingerprints of specification 7.3 with their specified roles.
14. Do not add dynamic MCP tool registration in MVP v0.1. Use the six stable tools
    of specification Section 12.

## Working commands

```bash
uv sync              # install the environment
uv run wcc --help    # console script
./scripts/test.sh    # ruff format check, ruff lint, pyright, pytest
```

## Current state

Phase 0 is complete. See `docs/progress.md` for what exists and what comes next.
