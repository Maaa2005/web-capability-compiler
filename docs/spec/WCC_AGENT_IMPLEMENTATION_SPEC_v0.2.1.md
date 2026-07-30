# WCC Agent Implementation Specification

**Project:** Web Capability Compiler (WCC)
**Spec version:** 0.2.1 (implementation clarification)
**Product version target:** MVP v0.1
**Primary audience:** Codex, Claude Code, and other coding agents
**Document role:** This file is the implementation source of truth for the MVP.

**Major changes from spec v0.2:**

- The public positioning is corrected from “typed MCP tools” to **typed, MCP-callable capabilities**. MVP v0.1 exposes six stable MCP tools; generated capabilities are data executed through `execute_capability`, not dynamically registered MCP tools.
- Capability persistence is split into reusable `CapabilityTemplate` objects and session-scoped `BoundCapability` objects.
- `structural_hash`, `page_signature`, and `collection_hash` are separated and given distinct roles. Cross-session cache binding is specified.
- Browser identity is separated into `document_id` and `snapshot_id`; navigation and same-document updates are no longer conflated.
- MCP execution is explicitly synchronous for MVP. `get_execution_result` now also requires `session_id`, and time-based capability TTL is removed.
- URL validation, private-network blocking, redirect revalidation, popup/download restrictions, and resource limits are added.
- Packaging and clean-install behavior are specified, including `pyproject.toml`, `uv.lock`, `wcc install-browser`, `wcc doctor`, and pre-PyPI `uvx --from git+...` usage.
- Locator re-resolution now continues past zero-match and ambiguous candidates until a unique candidate is found.
- POST submissions are always `high` by default in MVP; no fixture-only read-only POST exception exists.
- Benchmark methodology is tightened: a minimal model API loop, at least three runs per condition, median/range reporting, separate sessions for cold/warm runs, and different inputs within the same task class.

---

## 0. Instructions for the coding agent

Implement WCC in the phase order defined in this document. Do not broaden the scope without an explicit instruction from the project owner.

Rules:

1. Complete one phase and its acceptance criteria before starting the next phase.
2. Prefer deterministic parsing over LLM inference whenever the same result can be obtained from HTML, ARIA, JSON-LD, or browser state.
3. Do not implement irreversible actions such as purchase confirmation, account deletion, message sending, or state-changing form submission in MVP v0.1.
4. Never execute a capability whose risk level is above `low`. `medium`, `high`, and `critical` capabilities may be listed but must be blocked.
5. Preserve evidence for every generated entity and capability. The system must be able to explain which DOM element or page signal produced each result.
6. Add or update tests with every implementation change.
7. Do not silently change schemas. Schema changes require a version increment and migration notes.
8. Do not add an independent autonomous agent. WCC is a compilation and execution layer used by external agents.
9. Keep the LLM provider optional and replaceable. The core extraction and execution pipeline must run without an LLM.
10. All browser access goes through the `Driver` interface. Do not call Playwright APIs outside the driver package.
11. At the end of each phase, update `docs/progress.md` with completed work, test results, unresolved issues, and the next phase.
12. Do not collapse `CapabilityTemplate` and `BoundCapability` into one model. Templates are portable and reusable; bound capabilities are session-scoped and executable.
13. Do not treat a structural hash as a reusable page signature. Implement the three fingerprints in Section 7.3 with their specified roles.
14. Do not add dynamic MCP tool registration in MVP v0.1. Use the six stable tools in Section 12.

---

## 1. Positioning and objective

### 1.1 One-line positioning

**Compile existing websites into reusable, typed capabilities for MCP agents.**

WCC is not another browser-use agent. It is a **compilation, binding, and caching layer** between an existing website and an external AI agent:

- Existing browser agents (browser-use, Playwright MCP, computer-use) typically re-interpret low-level UI elements for every task. This consumes tokens, reduces repeatability, and binds workflows to transient visual or DOM details.
- WCC inspects a page, compiles observable controls into a small set of semantic, typed, low-risk capability templates, binds compatible templates to the current session, and lets an MCP-compatible agent execute them through a stable interface.
- On repeated use, WCC attempts to bind a persisted template to the current page before recompiling, allowing the agent to skip low-level DOM interpretation.
- Documentation framing: WCC provides **WebMCP-style capabilities for sites that do not yet declare them**. Native WebMCP import is future work.

MVP clarification: generated capabilities such as `search_products` are **not dynamically registered as MCP tools**. WCC exposes six stable MCP tools, and `execute_capability` executes a selected typed capability. Dynamic MCP registration is explicitly out of scope for v0.1.

### 1.2 Input

An ordinary existing web page that has not been modified for agents.

### 1.3 Output

A structured Agent Page IR containing:

- session, document, snapshot, and page metadata
- observable page state
- semantic entities
- reusable capability templates and session-bound executable capabilities
- JSON parameter schemas
- evidence and confidence
- risk classification
- execution and verification results

### 1.4 Example transformation

Human-oriented operations:

```text
Find the search input
Type "wireless earbuds"
Find the search button
Click it
```

WCC capability:

```json
{
  "name": "search_products",
  "description": "Search products by keyword",
  "parameters": {
    "type": "object",
    "properties": {
      "keyword": { "type": "string" }
    },
    "required": ["keyword"],
    "additionalProperties": false
  },
  "risk": {
    "level": "low",
    "side_effect": "none",
    "requires_confirmation": false
  }
}
```

The external agent selects this capability through `list_capabilities` and invokes it through `execute_capability`.

---

## 2. Success criteria

The MVP is successful when both of the following hold.

### 2.1 Functional success

An external MCP agent can:

1. open a session on a page,
2. obtain generated capabilities,
3. call a typed capability through the stable MCP interface with structured parameters,
4. observe a verified result,
5. do so without using raw click/type operations, screenshots, coordinates, or CSS selectors.

### 2.2 Benchmark success (primary public claim)

On a fixed benchmark task set (Section 14), WCC measures agent-side token usage and tool-call count versus low-level browser operation for the **second and subsequent executions** of the same task class. A reduction is reported only when the task also succeeds.

The headline metric of the project is **token reduction on repeated tasks**, not task success rate. The benchmark script and raw results must live in the repository and be reproducible with one command.

---

## 3. MVP scope

### 3.1 Supported capability categories

MVP v0.1 supports only:

- `search`
- `filter`
- `navigate` (pagination, opening a detail link from a result collection)

Deferred to post-MVP: `select_item` as a distinct category (covered by `navigate` in v0.1), `fill_form` (blocked by the risk model until user-approved medium-risk execution exists).

### 3.2 Supported page structures

- standard HTML forms
- input, select, textarea, button, and anchor elements
- labeled controls using `label`, `aria-label`, `aria-labelledby`, `name`, and placeholder
- simple lists, tables, cards, and search-result collections
- JSON-LD and Schema.org metadata when present
- dynamically updated pages (observed via driver-side wait/settle logic)

### 3.3 Explicit non-goals for v0.1

Do not implement:

- a Chrome extension (v0.2 driver; the `Driver` interface must make this possible without core changes)
- purchase confirmation or payment
- account creation or deletion
- sending messages, applications, posts, or emails
- any state-changing form submission
- CAPTCHA bypass, anti-bot evasion, login credential storage
- mobile browser support
- long-running autonomous planning, multi-agent negotiation
- a universal cross-industry ontology
- a capability registry service (export/import of local JSON files only)
- unrestricted JavaScript execution received from an external agent

---

## 4. System architecture

```text
External Agent (Claude Code, Codex, any MCP client)
    |
    | MCP (stdio)
    v
MCP Server: six stable tools
    |
    v
Local Runtime
    |-- Session manager
    |-- URL/network policy
    |-- Page IR service
    |-- Capability compiler
    |-- Template cache and binder
    |-- Risk classifier
    |-- Execution coordinator
    |-- Result verifier
    |-- Capability export/import
    |-- SQLite storage
    |
    | Driver interface (in-process)
    v
Playwright Driver (v0.1)           [Chrome Extension Driver: post-MVP]
    |-- page snapshot extractor
    |-- label/locator resolver
    |-- collection extractor
    |-- settle/wait logic
    |-- allowlisted action executor
    v
Existing Web Page
```

### 4.1 Driver interface

All browser access is behind a `Driver` protocol (Python `Protocol` / ABC):

```python
class Driver(Protocol):
    async def open_session(self, request: OpenSessionRequest) -> SessionInfo: ...
    async def close_session(self, session_id: str) -> None: ...
    async def snapshot(self, session_id: str) -> PageSnapshot: ...
    async def execute_action(
        self,
        session_id: str,
        action: PlannedAction,
    ) -> ActionObservation: ...
    async def wait_settled(self, session_id: str, timeout_ms: int) -> None: ...
```

Constraints:

- The runtime never imports Playwright directly; only the driver package does.
- `execute_action` accepts only allowlisted action types (Section 10.4).
- The driver resolves internal element IDs to live elements using the locator strategy in Section 7.7. It never accepts raw selectors from outside.
- URL and redirect validation occur before navigation is committed. The driver must surface attempted redirects, downloads, popups, and new pages to the runtime policy layer.

### 4.2 Component responsibilities

**Playwright driver:** launch a browser context, collect page signals, normalize interactive elements, detect settle state, execute approved low-level actions, and return observations. It must not compile capabilities or decide policy.

**Local runtime:** validate URLs, manage sessions, validate driver payloads, construct Agent Page IR, compile templates, bind compatible templates to sessions, classify risk, coordinate execution, verify postconditions, persist fingerprints and templates, and handle local export/import.

**MCP server:** expose WCC through the six stable tools in Section 12. It must not dynamically add generated capabilities as MCP tool definitions in MVP v0.1.

---

## 5. Technology decisions

- Python 3.12 or later, single language for MVP
- `uv` for environment, locking, packaging, and execution
- Pydantic v2 for all public and persistence-bound models; JSON Schemas in `schemas/` are generated from Pydantic models
- SQLModel + SQLite
- Playwright async API for the v0.1 driver and integration tests
- official MCP Python SDK with stdio transport
- pytest, Ruff, and Pyright-compatible typing
- no FastAPI or network listener in v0.1; MCP server and runtime communicate in-process

### 5.1 Required packaging files and commands

The repository must contain:

- `pyproject.toml` with the `wcc` console-script entry point
- `uv.lock`
- `src/wcc/__init__.py`
- `.github/workflows/ci.yml` by Phase 3

Supported commands:

```bash
uv run wcc --help
uv run wcc serve
uv run wcc install-browser
uv run wcc doctor
uv run wcc export --session <session_id> --output <file>
uv run wcc import <file>
uv run wcc clear-data
uv run wcc diagnostics
```

Before a PyPI release, the documented one-command installation is:

```bash
uvx --from git+https://github.com/<owner>/<repository>.git wcc serve
```

After a PyPI release, the shorter form may be documented:

```bash
uvx wcc serve
```

`wcc install-browser` installs the required Playwright Chromium build. `wcc doctor` validates Python compatibility, browser availability, SQLite access, data-directory permissions, and configuration without opening an arbitrary website.

Dropped from spec v0.1: TypeScript, Vite, Manifest V3, WebSocket/HTTP extension protocol, and localhost endpoint tables.

---

## 6. Repository structure

```text
web-capability-compiler/
├── AGENTS.md
├── README.md
├── LICENSE
├── pyproject.toml
├── uv.lock
├── .gitignore
├── .github/
│   └── workflows/
│       └── ci.yml
├── docs/
│   ├── architecture.md
│   ├── progress.md
│   ├── security.md
│   ├── comparison.md
│   └── schemas.md
├── schemas/
├── src/wcc/
│   ├── __init__.py
│   ├── main.py
│   ├── session.py
│   ├── policy/
│   │   ├── url_policy.py
│   │   └── limits.py
│   ├── models/
│   ├── driver/
│   │   ├── base.py
│   │   └── playwright_driver.py
│   ├── compiler/
│   │   ├── page_ir_builder.py
│   │   ├── deterministic_compiler.py
│   │   ├── semantic_enricher.py
│   │   └── risk_classifier.py
│   ├── binding/
│   │   ├── template_cache.py
│   │   └── binder.py
│   ├── executor/
│   │   ├── coordinator.py
│   │   └── verifier.py
│   ├── mcp/
│   │   ├── server.py
│   │   └── tools/
│   ├── storage/
│   └── portability/
│       ├── exporter.py
│       └── importer.py
├── fixtures/
│   ├── search-page/
│   ├── product-list/
│   ├── filter-page/
│   ├── pagination-page/
│   ├── dynamic-results/
│   └── post-contact-form/
├── benchmarks/
│   ├── run_benchmark.py
│   ├── agent_loop.py
│   ├── tasks.yaml
│   └── results/
└── scripts/
    ├── dev.sh
    ├── test.sh
    └── gen_schemas.py
```

`AGENTS.md` must summarize Section 0 and link to this specification as the source of truth.

---

## 7. Core data models

All IDs are opaque strings generated by WCC. Public models never expose CSS selectors.

### 7.1 Session

```json
{
  "session_id": "sess_01J...",
  "created_at": "2026-07-31T04:00:00+09:00",
  "initial_url": "https://example.com/products",
  "current_document_id": "doc_01J...",
  "current_snapshot_id": "snap_01J...",
  "status": "active"
}
```

One session equals one Playwright browser context with one active page. Every document, snapshot, IR, bound capability, and execution references its session.

### 7.2 Document and Page Snapshot

`document_id` identifies the currently loaded browser document and changes after a committed navigation to a new document. Same-document DOM updates do not change it.

`snapshot_id` identifies one observation and changes on every snapshot, including same-document updates.

```json
{
  "schema_version": "0.2.1",
  "session_id": "sess_01J...",
  "document_id": "doc_01J...",
  "snapshot_id": "snap_01J...",
  "captured_at": "2026-07-31T04:00:00+09:00",
  "url": "https://example.com/products",
  "origin": "https://example.com",
  "normalized_route_pattern": "/products",
  "title": "Product Search",
  "language": "en",
  "elements": [],
  "collections": [],
  "structured_data": [],
  "structural_hash": "sha256:...",
  "page_signature": "sha256:..."
}
```

Each normalized element must include: internal element ID, tag name, role, resolved label, accessible name, label source, control type, current value only when safe, required/disabled/readonly/checked state, visibility, bounding box, form association, and locator chain.

Never collect password values, payment-card values, security codes, hidden authentication tokens, or file input paths.

### 7.3 Fingerprints and normalization

WCC uses three separate fingerprints. They are not interchangeable.

#### `structural_hash`

Represents the current normalized page structure and is used to detect inspection changes.

Include:

- tag names and roles of relevant elements
- resolved labels and control types
- form structure
- landmark structure
- collection item-count buckets: `{0, 1, 2-10, 11-50, 50+}`

Exclude:

- ordinary non-interactive text content
- `style`, `class`, and `data-*`
- timestamps and random IDs
- ad-like or tracking regions
- hidden inputs and token-like values

#### `page_signature`

Represents a reusable page class for capability-template lookup. Compute from:

```text
origin
+ normalized_route_pattern
+ normalized form/control fingerprint
+ normalized landmark structure
```

`normalized_route_pattern` removes fragments and query values and replaces clearly dynamic path segments, such as numeric IDs or UUID-like segments, with placeholders. The exact normalization algorithm must be deterministic and covered by tests.

A `page_signature` must remain stable across different search terms and result contents when the underlying controls remain compatible.

#### `collection_hash`

Represents the normalized records of one extracted collection and is used for postcondition verification. It must be insensitive to irrelevant rendering attributes but sensitive to record identity, order when order is meaningful, and result-count changes.

A structural-hash change alone does not invalidate a capability. Executability is decided by template binding and locator re-resolution.

### 7.4 Confidence

`confidence` is an enum: `high` | `medium` | `low`.

| Signal situation | Confidence |
|---|---|
| Explicit semantic marker (`type="search"`, `role="search"`, JSON-LD action, `<label for>`) | high |
| Strong heuristic (label keyword match plus matching form structure) | medium |
| Placeholder-only or positional inference | low |

Only `medium` and `high` candidates may become capability templates. `low` candidates appear in IR warnings and are never executable.

### 7.5 Entity

```json
{
  "id": "entity_01J...",
  "type": "product",
  "label": "Wireless Earbuds",
  "properties": { "price_text": "4,980 yen" },
  "evidence": [
    { "kind": "dom", "element_id": "el_01J...", "attribute": "text_content" }
  ],
  "confidence": "high",
  "available_actions": [
    {
      "capability_id": "cap_01J...",
      "arguments": { "entity_id": "entity_01J..." }
    }
  ]
}
```

### 7.6 Locator candidate model

Each element stores all applicable locator candidates plus a primary candidate. Candidate kinds, in priority order:

1. unique `id` attribute
2. `name` plus form association
3. accessible name plus role
4. associated label text
5. structural path from the nearest landmark or form ancestor

Structural paths may include tag-index information, but execution must never fall back to a global nth-child-only selector, bounding box, screenshot coordinate, or agent-supplied selector.

### 7.7 Locator re-resolution

At execution or binding time, try locator candidates in priority order:

- zero matches: continue to the next candidate
- exactly one match: resolution succeeds
- multiple matches: treat that candidate as ambiguous and continue to the next candidate
- no candidate yields exactly one match: resolution fails and the element is stale/unbindable

Never choose the “closest” element from an ambiguous result.

### 7.8 CapabilityTemplate

A `CapabilityTemplate` is portable, persistent, and not directly executable. It must not contain a session ID or live element handle.

```json
{
  "template_id": "tmpl_01J...",
  "schema_version": "0.2.1",
  "origin": "https://example.com",
  "normalized_route_pattern": "/products",
  "page_signature": "sha256:...",
  "name": "search_products",
  "category": "search",
  "description": "Search products by keyword",
  "parameters": {
    "type": "object",
    "properties": {
      "keyword": { "type": "string", "description": "Product keyword" }
    },
    "required": ["keyword"],
    "additionalProperties": false
  },
  "execution_plan_template": [
    { "action": "set_value", "target_ref": "search_input", "parameter": "keyword" },
    { "action": "activate", "target_ref": "search_submit" }
  ],
  "target_locators": {
    "search_input": [],
    "search_submit": []
  },
  "expected_effect": {
    "type": "page_state_change",
    "signals": ["url_change", "results_collection_change"]
  },
  "risk": {
    "level": "low",
    "side_effect": "none",
    "reversible": true,
    "requires_confirmation": false
  },
  "evidence": [],
  "confidence": "high"
}
```

### 7.9 BoundCapability

A `BoundCapability` is executable only inside one active session after every required target has been uniquely re-resolved.

```json
{
  "capability_id": "cap_01J...",
  "template_id": "tmpl_01J...",
  "session_id": "sess_01J...",
  "document_id": "doc_01J...",
  "bound_snapshot_id": "snap_01J...",
  "resolved_targets": {
    "search_input": "el_01J...",
    "search_submit": "el_01K..."
  },
  "bound_at": "2026-07-31T04:03:00+09:00",
  "status": "active"
}
```

### 7.10 Template lookup and binding

Before compilation, WCC attempts cache reuse:

1. Query templates by origin and normalized route pattern.
2. Prefer templates whose `page_signature` equals the current signature.
3. Re-resolve every required target locator against the current document.
4. Re-run deterministic risk classification against current page signals.
5. If all required targets resolve uniquely and risk has not increased, create a `BoundCapability`.
6. If binding fails, do not partially bind. Compile a new template from the current snapshot.

A changed `structural_hash` does not prevent reuse when binding succeeds.

### 7.11 Navigation capability arguments

Do not generate one capability per result item. Generate a shared navigation capability that accepts an entity reference:

```json
{
  "name": "open_result_item",
  "parameters": {
    "type": "object",
    "properties": {
      "entity_id": { "type": "string" }
    },
    "required": ["entity_id"],
    "additionalProperties": false
  }
}
```

The runtime resolves `entity_id` only from entities issued in the current session and snapshot lineage.

### 7.12 Execution result

```json
{
  "execution_id": "exec_01J...",
  "session_id": "sess_01J...",
  "capability_id": "cap_01J...",
  "status": "succeeded",
  "started_at": "2026-07-31T04:05:00+09:00",
  "completed_at": "2026-07-31T04:05:01+09:00",
  "steps": [],
  "observed_effects": ["url_change", "results_collection_change"],
  "verification": {
    "passed": true,
    "signals_matched": 2,
    "signals_expected": 2
  },
  "document_id_after": "doc_01K...",
  "snapshot_id_after": "snap_01K...",
  "result_entities": [],
  "error": null
}
```

`execute_capability` returns this complete result synchronously in MVP v0.1. `get_execution_result` retrieves a previously stored result.

### 7.13 Portable capability file

`wcc export --session <session_id>` exports selected `CapabilityTemplate` objects, never session-bound capabilities.

An import pipeline must perform:

1. JSON and schema validation
2. schema-version compatibility check
3. action-allowlist validation
4. origin and route-pattern validation
5. deterministic risk recomputation at bind time
6. locator re-resolution before execution

Imported JSON is never executable merely because it was successfully parsed. This feature is local-file-only; there is no registry or network sharing service in v0.1.

---

## 8. Deterministic extraction rules

Implement these before any LLM integration.

### 8.1 Accessible-name priority

Resolve a control label in this order, and store both the resolved label and the source used:

1. associated `<label for>`
2. wrapping `<label>`
3. `aria-labelledby`
4. `aria-label`
5. button or link visible text
6. `name`
7. placeholder
8. nearby heading or fieldset legend

### 8.2 Interactive element eligibility

Include an element when one or more conditions hold: native interactive element; ARIA interactive role; focusable and visible; associated with a form.

Exclude: invisible or zero-area elements; disabled controls; decorative elements; tracking pixels; hidden inputs except safe semantic values required to understand a form.

### 8.3 Form compilation

A form becomes a capability candidate when it has one or more eligible controls, an identifiable submission or activation mechanism, and its purpose resolves to confidence `medium` or higher (Section 7.4).

Deterministic categories: search form, filter form. (Generic data-entry forms are detected and reported in warnings but not compiled in v0.1.)

Search signals: `type="search"`; labels containing search-related words (maintain a small multilingual keyword list incl. English and Japanese); form `role="search"`; action URL containing search or query semantics.

### 8.4 Collection extraction

Detect repeated siblings or descendants with similar structure: lists, tables, repeated cards, and search-result blocks. Each collection preserves the mapping from extracted records to evidence elements and, where supported, maps item navigation targets to entity IDs. Collection records must include a deterministic per-record identity projection for `collection_hash`.

---

## 9. Capability compilation

### 9.1 Compilation stages

```text
Page Snapshot
  -> persisted-template lookup
  -> safe binding attempt
  -> deterministic candidates when no template binds
  -> semantic naming
  -> parameter schema generation
  -> risk classification
  -> execution-plan-template generation
  -> schema validation
  -> template persistence
  -> session binding
```

### 9.2 Naming rules

Capability names must use `snake_case`, begin with an action verb, describe domain semantics where confidence permits, and remain generic when domain inference is uncertain.

Preferred: `search_products`, `filter_results`, `navigate_next_page`, `open_result_item`
Fallback: `submit_search_form`, `apply_filter_form`

Never invent a capability that is unsupported by observable page evidence.

### 9.3 Optional semantic enricher (Phase 6)

The enricher may use an LLM only for: page-type classification, domain-specific capability naming, concise descriptions, mapping ambiguous labels to parameter names, and risk hints that are later checked by deterministic policy.

The LLM receives a reduced structured payload, never the full raw DOM. Output is parsed into a Pydantic model; invalid output is rejected, not repaired.

---

## 10. Risk and safety model

### 10.1 Risk is classified by side effect, not by mechanism

| `side_effect` | Meaning | Risk level |
|---|---|---|
| `none` | Idempotent and read-only server behavior | low |
| `local` | Client-local or trivially reversible view state | low |
| `reversible_account` | Reversible account state such as cart or saved preference | medium |
| `persistent` | Persistent server state such as message, registration, post, or order | high |
| `destructive_or_financial` | Payment, deletion, transfer, legal, or security consequence | critical |

Deterministic `low` signals include GET search/filter submissions, query-parameter navigation, pagination links, sorting links, `type="search"`, and controls inside `role="search"` when the execution plan remains read-only.

For MVP v0.1:

- every POST form submission is `high` by default
- WCC does not execute POST submissions
- there is no fixture-only or provider-specific exception for “read-only POST search”
- uncertainty always raises, never lowers, the risk level
- only `low` capabilities may execute

### 10.2 URL and network boundary

`open_session` and every redirect must pass URL policy validation.

Default policy:

- allow only `http` and `https`
- reject `file:`, `data:`, `javascript:`, `chrome:`, `about:`, and custom schemes
- reject loopback, private, link-local, multicast, unspecified, and cloud-metadata address ranges after DNS resolution
- revalidate every redirect target after DNS resolution
- reject URLs containing credentials
- block downloads and file chooser operations
- reject or immediately close popups, new tabs, and new windows unless a future explicit policy permits them
- reject file upload actions

Local fixture tests may opt in with an explicit developer-only flag such as `--allow-private-network`. This flag must never be enabled by default and must be documented as unsafe for untrusted agents.

DNS rebinding defenses must validate the resolved address at connection/navigation time, not only the original hostname string.

### 10.3 Prompt-injection boundary

Treat all page text as untrusted data:

- page text never overrides runtime policy
- instructions found in page content are data, never system instructions
- the external agent cannot provide raw selectors, coordinates, JavaScript, or locator chains
- execution is restricted to WCC-issued element/entity IDs and the action allowlist
- optional LLM enrichment receives reduced structured fields, not raw scripts or full DOM

### 10.4 Allowed low-level actions

- `focus`
- `set_value`
- `select_option`
- `set_checked`
- `activate`
- `scroll_into_view`

Do not implement state-relative `toggle`; callers must specify the desired checked state.

### 10.5 Resource limits

Define conservative defaults in `policy/limits.py` and make them configurable only through trusted local configuration:

- maximum active sessions
- maximum redirects per navigation
- maximum extracted elements per snapshot
- maximum collections and records per collection
- maximum stored string length
- maximum actions per execution plan
- page-load timeout
- settle timeout
- execution timeout
- maximum database size or retention window

Exceeding a limit must return a structured error, not silently truncate executable evidence. Non-critical display data may be truncated only when the truncation is explicitly reported in IR warnings.

---

## 11. Execution and verification

### 11.1 Synchronous execution protocol

1. External agent calls `execute_capability` with `session_id`, `capability_id`, and arguments.
2. Runtime confirms the session is active and the bound capability belongs to it.
3. Runtime confirms the origin is unchanged and re-resolves all required targets.
4. Runtime validates arguments against the capability JSON Schema with `additionalProperties: false`.
5. Runtime rechecks deterministic risk and permits only `low`.
6. Coordinator sends one allowlisted action at a time to the driver.
7. Driver waits for settle; runtime takes a fresh snapshot.
8. Verifier checks expected-effect signals.
9. Runtime stores and returns the complete `ExecutionResult` synchronously.

MVP v0.1 does not expose an asynchronous job protocol. `get_execution_result` is a retrieval tool for already completed executions.

### 11.2 Stale and unbound handling

A bound capability becomes stale when:

- the session is closed
- the current origin differs from the template origin
- the active document context is incompatible with the binding
- any required target fails unique locator re-resolution
- an entity argument is not part of the current snapshot lineage
- deterministic risk is now above the stored risk or above `low`

There is no time-based capability TTL in MVP v0.1.

On stale detection:

1. stop before executing any action
2. return status `stale`
3. trigger or request a fresh inspection
4. attempt normal template lookup and binding
5. never guess a replacement target during the failed execution

### 11.3 Verification signals

Supported signals:

- URL changed
- title changed
- document changed
- target collection hash changed
- collection item count changed
- element appeared or disappeared
- control value reached an expected value
- loading settled
- selected or checked state reached an expected value

A capability succeeds only when its expected effect is observed or an explicitly defined no-change success condition passes. A token reduction from a failed task is never counted as benchmark success.

---

## 12. MCP interface

MVP v0.1 exposes exactly six stable MCP tools over stdio. Every tool except `open_session` requires `session_id`. Generated capabilities are returned as typed data and executed through `execute_capability`; they are not dynamically registered MCP tools.

### 12.1 `open_session`

Input:

```json
{ "url": "https://example.com/products" }
```

Output: session ID, document ID, snapshot ID, and initial page metadata. URL and redirect policy are enforced before the session is returned.

### 12.2 `inspect_page`

Input:

```json
{ "session_id": "sess_01J...", "refresh": true }
```

Output: page metadata, page signature, entity summary, bound-capability summary, and warnings. Inspection first attempts persisted-template binding, then compiles when binding is unavailable.

### 12.3 `list_capabilities`

Input:

```json
{
  "session_id": "sess_01J...",
  "category": null,
  "max_risk": "low"
}
```

Output: bound capability IDs, template IDs, names, descriptions, parameter schemas, risk, and confidence.

### 12.4 `execute_capability`

Input:

```json
{
  "session_id": "sess_01J...",
  "capability_id": "cap_01J...",
  "arguments": { "keyword": "wireless earbuds" }
}
```

Output: complete synchronous `ExecutionResult`, including validation errors, verification details, resulting snapshot IDs, and `result_entities`.

### 12.5 `get_execution_result`

Input:

```json
{
  "session_id": "sess_01J...",
  "execution_id": "exec_01J..."
}
```

Output: the stored execution result only when it belongs to the supplied session.

### 12.6 `close_session`

Input:

```json
{ "session_id": "sess_01J..." }
```

Output: final session status. Closing is idempotent.

CLI handles export/import in MVP. Additional MCP tools such as `explain_capability`, dynamic tool registration, or MCP-native export remain post-MVP.

---

## 13. Development phases

## Phase 0: Minimal bootstrap and public model skeleton

Tasks:

- repository skeleton
- `pyproject.toml`, `uv.lock`, console-script entry point, and `src/wcc/__init__.py`
- Ruff, Pyright configuration, pytest, `scripts/test.sh`
- `AGENTS.md`
- static search and product-list fixtures
- initial Pydantic models required by the driver interface: `OpenSessionRequest`, `SessionInfo`, `PageSnapshot`, `NormalizedElement`, `LocatorCandidate`, `CollectionSnapshot`, `PlannedAction`, `ActionObservation`

Do not add CI or generated schema artifacts yet.

Acceptance:

- `uv run wcc --help` works
- `scripts/test.sh` passes
- fixtures serve locally under the explicit developer private-network allowance
- runtime modules outside `driver/` cannot import Playwright, enforced by a test or lint rule

## Phase 1: Driver, URL policy, and snapshots

Tasks:

- `Driver` protocol and Playwright implementation
- session/document/snapshot identity
- URL policy and redirect revalidation
- element normalization and label resolution
- locator candidates and unique re-resolution
- collection and JSON-LD extraction
- `structural_hash`, `page_signature`, `collection_hash`
- settle logic and resource limits
- `wcc install-browser` and `wcc doctor`

Acceptance:

- at least 95% of labeled fixture controls have correct labels and sources
- password, payment, token, and file-input values are never included
- unchanged reload produces identical structural hash
- ad-like changes do not change structural hash
- form-structure changes change structural hash
- search-term/result changes preserve page signature when controls remain compatible
- collection changes change collection hash
- every executable target has a locator chain that uniquely re-resolves after reload
- disallowed schemes/private-network navigation are rejected by default

## Phase 2: Agent Page IR, templates, binding, and persistence

Tasks:

- complete Pydantic models
- page state, entities, evidence, warnings
- `CapabilityTemplate` and `BoundCapability`
- SQLite persistence for sessions, documents, snapshots, IR, templates, and bindings
- template lookup and binding algorithm

Acceptance:

- every entity and template has evidence
- invalid driver payloads are rejected with actionable errors
- repeated unchanged inspection produces semantically equivalent IR
- a saved search template binds in a new session with a different search term
- an incompatible locator structure refuses binding and causes recompilation
- no persisted template is directly executable without a bound capability

## Phase 3: Deterministic compiler, risk, schemas, and CI

Tasks:

- search/filter detection
- pagination and entity-based result navigation
- parameter schema generation
- deterministic confidence and risk classification
- execution-plan templates
- generated JSON Schemas
- CI for lint, typing, and tests

Acceptance:

- search fixture produces a search template
- filter fixture produces a filter template
- pagination and result-item fixtures produce shared navigation templates
- every template has confidence, evidence, risk, and page signature
- POST contact form is `high`, listed, and not executable
- schema generation is deterministic and CI passes from a clean checkout

## Phase 4: Execution, verification, and cache reuse

Tasks:

- coordinator and argument validation
- stepwise allowlisted execution
- synchronous execution results
- post-action snapshots and verification
- stale handling
- entity-reference validation
- result-entity extraction
- cold compile versus warm bind path

Acceptance:

- generated search capability executes and returns structured results
- wrong argument types fail before driver action
- removed or ambiguous target returns `stale`
- risk increase blocks execution
- each action and observation is recorded
- verifier detects collection changes
- a second session reuses and binds a persisted template without recompiling when compatible

## Phase 5: MCP, portability, and operational CLI

Tasks:

- six MCP tools
- MCP integration tests
- capability-template export/import
- `wcc clear-data`
- `wcc diagnostics`
- Git-based `uvx --from` setup guide and MCP configuration snippets

Acceptance:

- external MCP client completes the fixture flow without raw selectors, screenshots, coordinates, click tools, or type tools
- `execute_capability` returns the complete result synchronously
- `get_execution_result` enforces session ownership
- template export/import round-trips and still requires safe binding
- malformed or unsafe imported plans are rejected
- clean environment setup succeeds using the documented commands

## Phase 6: Optional semantic enrichment

Start only after Phases 0–5 pass.

Tasks: provider-neutral LLM interface, reduced structured payload, naming/description enrichment, cache by page signature.

Acceptance:

- full system works with LLM disabled
- invalid model output is rejected and deterministic output remains usable
- enrichment cannot alter target locator chains, execution actions, or lower deterministic risk

## Phase 7: Token-reduction benchmark and public materials

Tasks:

- implement a deliberately small benchmark model loop in `benchmarks/agent_loop.py`
- use one explicitly versioned model API for both arms
- baseline arm exposes low-level Playwright-MCP-style tools
- WCC arm exposes the six stable WCC tools
- collect provider-reported input/output token usage, tool calls, wall time, and success
- run baseline, WCC cold, and WCC warm in separate clean sessions
- use different input values for cold and warm runs within the same task class
- run every condition at least three times
- report median and range, plus every raw run
- use local fixtures as the reproducible benchmark of record
- publish public-site results only as dated supplementary observations
- create `docs/comparison.md`, README benchmark table, quickstart, and demo GIF

Do not build a general evaluation framework, provider abstraction platform, or autonomous benchmark agent. The benchmark loop exists only to evaluate WCC.

Reporting rules:

- first public run measures; it does not invent achievement targets
- failed tasks remain failures and are not credited for token reduction
- raw logs include model identifiers, timestamps, task input, outcome, and usage
- unauthorized execution above `low` must equal zero
- capability evidence coverage must equal 100%

---

## 14. Benchmark methodology

### 14.1 Arms

- **Baseline:** the model uses low-level Playwright-MCP-style observation and interaction tools.
- **WCC cold:** no compatible persisted template exists; WCC inspects, compiles, binds, and executes.
- **WCC warm:** a compatible persisted template exists; WCC binds and executes without recompiling.

### 14.2 Controls

- identical model and exact model version for all arms
- identical system policy except for tool descriptions required by each arm
- same task class and equivalent wording
- different concrete input values for cold and warm runs to avoid response/browser-cache contamination
- separate browser sessions and controlled storage state
- optional LLM semantic enrichment disabled
- at least three runs per condition; report median and observed range

Example:

```text
Cold task: search for "wireless earbuds"
Warm task: search for "mechanical keyboard"
```

### 14.3 Metrics

Metric of record:

- provider-reported prompt/input tokens
- provider-reported completion/output tokens
- total tokens
- tool-call count
- wall-clock duration
- task success

Token values must come from machine-readable API usage metadata. Do not estimate tokens from UI screenshots or terminal text.

### 14.4 Result rules

- report baseline, cold, and warm independently
- never merge failed and successful runs into one token-reduction figure
- a task must meet its verification condition to count as completed
- publish raw logs under `benchmarks/results/`
- local fixtures are the stable reproducible benchmark
- public sites are supplementary, dated, and accompanied by limitations
- benchmark must run with one documented command

---

## 15. Test strategy

**Unit:** URL/scheme/IP validation; redirect validation; route normalization; label resolution; visibility filtering; form association; collection grouping; all three fingerprints; schema validation; deterministic classification; parameter generation; GET-vs-POST risk; locator ambiguity fallback; template binding; stale detection; entity lineage; result verification.

**Integration (Playwright + local fixtures):** inspect static search form; generate and bind a search template; execute and verify collection change; reuse the template in a second session with a different keyword; inspect dynamic filter page; detect stale capability after DOM replacement; confirm a POST contact-form template is compiled but blocked; confirm private-network fixture access requires the explicit developer flag.

**Regression:** every reported parsing or execution bug adds a minimal fixture and regression test.

---

## 16. Observability

Use structured JSON logs. Each execution log includes:

- request ID
- session ID
- document ID and snapshot ID
- template ID and bound capability ID
- execution ID
- origin and normalized route pattern
- phase and duration
- result status
- risk decision
- binding decision
- verification decision

Never log passwords, payment data, authentication tokens, file paths from upload controls, full sensitive form values, or raw imported capability files.

`wcc diagnostics` prints recent sanitized failures, environment checks, and counts. It must not reveal private page content by default.

---

## 17. Persistence

SQLite tables:

- `sessions`
- `documents`
- `page_snapshots`
- `page_irs`
- `capability_templates`
- `bound_capabilities`
- `executions`
- `execution_steps`

Persist templates with origin, normalized route pattern, page signature, schema version, creation time, last successful bind/use, success count, and failure count.

Never persist live Playwright handles. Never execute a template without creating a session-bound capability. Imported templates are marked with provenance and pass the same binding and risk checks as locally compiled templates.

---

## 18. Privacy requirements

- process raw DOM locally by default
- transmit only reduced structured data to an optional LLM provider
- redact sensitive input types before persistence
- `wcc clear-data` removes all local WCC data, including imported templates and benchmark logs unless explicitly excluded by a documented option
- document exactly what WCC reads from pages
- do not persist raw DOM by default after normalized extraction succeeds
- document private-network blocking and the risks of the developer override

---

## 19. Definition of done for MVP v0.1

- [ ] Playwright browser access exists only inside the driver package.
- [ ] URL policy blocks disallowed schemes and private/loopback/link-local destinations by default, including redirects.
- [ ] Driver produces valid session, document, and snapshot models.
- [ ] Runtime computes distinct structural hashes, page signatures, and collection hashes.
- [ ] Runtime builds valid Agent Page IR with evidence and enum confidence.
- [ ] Search, filter, and entity-based navigation templates are generated where supported.
- [ ] Reusable `CapabilityTemplate` and session-scoped `BoundCapability` are separate models.
- [ ] Compatible templates bind and execute in a new session; incompatible templates recompile rather than guess.
- [ ] Risk policy blocks `medium`, `high`, and `critical`; a POST fixture proves the default-high rule.
- [ ] Generated search capability executes and returns verified `result_entities`.
- [ ] Postconditions, stale handling, locator ambiguity handling, and entity lineage work.
- [ ] Six stable MCP tools work through an external client; generated capabilities are not dynamically registered tools.
- [ ] `execute_capability` is synchronous and `get_execution_result` requires session ownership.
- [ ] No raw selector, screenshot, coordinate, JavaScript, generic click, or generic type operation is required from the external agent.
- [ ] Template export/import round-trips and imported data cannot execute before validation and binding.
- [ ] `wcc install-browser`, `wcc doctor`, `wcc diagnostics`, and `wcc clear-data` work.
- [ ] Clean setup works with `uv run` and documented pre-PyPI `uvx --from git+...` usage.
- [ ] Benchmark runs with one command and stores machine-readable raw usage data.
- [ ] README includes positioning, benchmark table, limitations, quickstart, and demo GIF.
- [ ] `docs/comparison.md`, architecture, privacy, and security limitations are documented.
- [ ] Tests, lint, typing, generated-schema checks, and CI pass from a clean checkout.

---

## 20. Required demonstration

The first public README GIF must show:

1. WCC starts using the documented command. Before PyPI release, use `uvx --from git+https://github.com/<owner>/<repository>.git wcc serve`; after release, `uvx wcc serve` is acceptable.
2. Agent calls `open_session` with an allowed search-page URL.
3. Agent calls `inspect_page`; WCC either compiles or safely binds a search template.
4. Agent calls `list_capabilities`; WCC returns a typed `search_*` bound capability.
5. Agent calls `execute_capability` with a keyword.
6. WCC fills the input, activates the GET search, verifies the result collection change, and returns structured entities.
7. A second clean session uses a different keyword and demonstrates template binding without recompilation.
8. Side-by-side baseline shows the same task class through low-level operations, with successful-run tool calls and provider-reported token usage overlaid.

The demo must not require the agent to inspect a screenshot, choose coordinates, provide selectors, execute JavaScript, or call generic `click`/`type` tools.

---

## 21. Future work, not part of MVP

- Chrome extension driver for user-owned logged-in browser sessions
- native WebMCP import and fallback selection
- dynamic MCP tool registration where client support is reliable
- shared capability registry / community site definitions
- user-approved `medium` risk execution (enables `fill_form`)
- capability reuse across structurally similar sites and cross-site normalization
- explicitly approved POST-based read-only operations after a stronger effect-verification model
- visual context paging for structure-poor interfaces

These items remain outside v0.1 until the definition of done is met.

---

## 22. Project summary for an external coding agent

Build a local-first compilation and binding layer that opens allowed ordinary web pages through a Playwright driver, compiles visible and accessible controls into reusable `CapabilityTemplate` objects, safely binds compatible templates into session-scoped `BoundCapability` objects, exposes them through six stable MCP tools, executes only deterministic `low`-risk plans through an allowlisted action set, and verifies resulting page state.

Prove value with a small reproducible benchmark comparing a low-level browser-tool baseline, WCC cold compilation, and WCC warm binding using provider-reported token usage and successful task outcomes.

Prioritize deterministic extraction, typed models, evidence, safe cache reuse, URL/network boundaries, risk escalation, and testability. Do not build a general autonomous browser agent, do not add dynamic MCP tools or a Chrome extension in MVP v0.1, and do not automate any operation above `low` risk.
