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

---

## Phase 1: Driver, URL policy, and snapshots — in progress

Tasks are tracked as T1–T9 in specification Section 13 order. Each task lands with
`scripts/test.sh` green before the next one starts.

### T1: `Driver` protocol and Playwright implementation — done (2026-08-02)

- `src/wcc/driver/base.py`: the five-method `Driver` protocol of specification 4.1,
  a `NavigationGuard` protocol, `DriverSettings`, and an error taxonomy whose stable
  `code` values let an agent tell blocked from stale from missing (specification 11.2).
- `src/wcc/driver/playwright_driver.py`: Chromium via the Playwright async API.
  Session lifecycle, `wait_settled`, guard-checked navigation, and navigation-event
  surfacing are implemented.
- `src/wcc/identifiers.py`: opaque prefixed IDs from `secrets`.
- Tests: `tests/test_driver_contract.py` (no browser) and
  `tests/test_playwright_driver.py` (Chromium against the loopback fixture server).
- `scripts/test.sh`: ruff format, ruff lint, pyright 0 errors, 42 tests passed.

Decisions worth carrying forward:

- **The navigation guard is a required constructor argument, not an optional one.**
  Specification 4.2 forbids the driver from deciding policy, and a default-permissive
  guard would have made an unguarded driver the easy path. T3 supplies the real policy.
- **Validation happens in Playwright's routing layer, not after `goto` returns.**
  Specification 4.1 requires validation *before* a navigation is committed; routing
  sees each redirect hop as its own document request, so a refused hop is aborted
  rather than loaded and then regretted.
- **`snapshot` and `execute_action` raise `DriverNotReadyError` naming T2/T4 and T5.**
  This follows the Phase 0 convention that unimplemented surface must not look like
  success. `execute_action` genuinely cannot land before T5, because addressing an
  element requires the locator re-resolution of specification 7.7.
- **Driver-side budgets live in `DriverSettings` for now.** T8 moves the full
  specification 10.5 limit set into `policy/limits.py`.

Unresolved:

- `wait_settled` currently waits for network idle only. The full settle definition
  arrives with T8.
- Chromium is installed out-of-band today; `wcc install-browser` arrives with T9.
