# Progress

Phase log required by specification Section 0, rule 11.

## Phase 0: Minimal bootstrap and public model skeleton — complete (2026-07-31)

### Completed

- Repository skeleton, `pyproject.toml` with the `wcc` console script, `uv.lock`,
  and `src/wcc/__init__.py`
- Ruff (format + lint), Pyright, pytest, and `scripts/test.sh`
- `AGENTS.md` summarizing specification Section 0
- Static fixtures: `fixtures/search-page/` and `fixtures/product-list/`
- The eight models the driver interface needs: `OpenSessionRequest`, `SessionInfo`,
  `PageSnapshot`, `NormalizedElement`, `LocatorCandidate`, `CollectionSnapshot`,
  `PlannedAction`, `ActionObservation`

Per the phase instructions, CI and generated schema artifacts were **not** added;
they belong to Phase 3.

### Acceptance criteria

| Criterion | Result |
|---|---|
| `uv run wcc --help` works | Pass — prints usage and exits 0 |
| `scripts/test.sh` passes | Pass — ruff format, ruff lint, pyright (0 errors), 28 tests |
| Fixtures serve locally under the developer private-network allowance | Pass — loopback-only server in `tests/conftest.py` |
| Runtime modules outside `driver/` cannot import Playwright | Pass — AST guard in `tests/test_driver_isolation.py`, verified to fail on a deliberate violation |

### Decisions worth carrying forward

- The CLI registers the full command surface from specification 5.1, but every
  unimplemented command exits with code 3 and names the phase that will implement
  it. A silent no-op would look like success.
- `WCCModel` sets `extra="forbid"` and `frozen=True`, mirroring the
  `additionalProperties: false` rule that specification 11.1 applies at execution.
- `NormalizedElement` carries `value_omitted_for_safety` so a skipped password,
  payment, token, or file value is visible as a deliberate omission rather than an
  absent field.
- The Playwright guard walks the AST instead of grepping, so prose mentioning
  Playwright in a docstring does not trip it.

### Unresolved issues

- None blocking Phase 1.
- `uv.lock` currently pins only the Phase 0 dependency set. Playwright and the MCP
  SDK enter the lock in Phases 1 and 5.

### Next

Phase 1: `Driver` protocol and Playwright implementation, session/document/snapshot
identity, URL policy and redirect revalidation, element normalization and label
resolution, locator candidates and unique re-resolution, collection and JSON-LD
extraction, the three fingerprints, settle logic and resource limits, plus
`wcc install-browser` and `wcc doctor`.
