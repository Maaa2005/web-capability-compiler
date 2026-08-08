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

### T2: session/document/snapshot identity — done (2026-08-02)

- `src/wcc/compiler/route.py`: `origin_of` and `normalize_route_pattern`, the
  deterministic normalization specification 7.3 requires tests for.
- `src/wcc/driver/base.py`: `DocumentIdentity` and a `PageSignals` protocol.
- `src/wcc/driver/playwright_driver.py`: a per-document token stamped by an init
  script, `identify()`, and a `SessionInfo` that now carries the first document and
  snapshot IDs.
- Tests: `tests/test_route_normalization.py` plus six identity tests driving real
  navigations, reloads, and a `pushState`.
- `scripts/test.sh`: ruff format, ruff lint, pyright 0 errors, 73 tests passed.

Decisions worth carrying forward:

- **Document identity comes from a token stamped into the page, not from navigation
  events.** Playwright's `framenavigated` also fires for same-document history
  updates, so trusting it would hand out a new `document_id` for a `pushState` that
  never replaced the document — the exact confusion specification 7.2 forbids. The
  init script runs before page scripts and defines the property non-writable and
  non-configurable, so a hostile page cannot forge a stale document's identity
  (specification 10.3).
- **`PageSignals` sits beside `Driver` rather than inside it.** Specification 4.1
  states the driver interface as exactly five methods; observation duties that the
  runtime needs (identity, navigation events) are a separate protocol so the stated
  interface is not quietly widened.
- **Query keys are kept, query values are dropped.** `/search?keyword=` and
  `/search?category=` are different page classes, so collapsing all queries would
  merge templates that must stay apart, while keeping values would break acceptance
  criterion 6. Path segments are replaced only for four closed shapes — all-digits,
  UUID, ULID, and long hex — because a segment that merely contains a digit, such
  as `v2`, names a real page class.
- **The Chromium tests fail rather than skip when the browser is missing.** A skip
  would let an unverified driver report success.

Unresolved:

- `snapshot()` still raises: assembling a `PageSnapshot` needs the elements of T4
  and the fingerprints of T7. Identity is the part the driver can report today.

### T3: URL policy and redirect revalidation — done (2026-08-02)

- `src/wcc/policy/url_policy.py`: `UrlPolicy` implementing the specification 10.2
  default policy — scheme allowlist, credential rejection, cloud-metadata refusal by
  name and by address, and post-resolution rejection of loopback, private,
  link-local, multicast, unspecified, and reserved ranges.
- `src/wcc/driver/playwright_driver.py`: popups, tabs, and windows are closed on
  arrival; downloads are cancelled; file choosers are left unanswered. Each refusal
  is recorded as a blocked `NavigationEvent`.
- Tests: `tests/test_url_policy.py` with a fake resolver, plus real-browser tests
  for the popup and download refusals. The driver tests now run against the real
  `UrlPolicy` rather than a permissive stub.
- `scripts/test.sh`: ruff format, ruff lint, pyright 0 errors, 108 tests passed.

Decisions worth carrying forward:

- **Resolution is repeated on every check and never cached.** Specification 10.2
  wants the resolved address validated at navigation time, so a cache would reopen
  the DNS-rebinding window the rule exists to close. A test drives a name that
  answers publicly once and with loopback the second time.
- **Every resolved address is checked, not just the first.** A split-horizon name
  answering with one public and one private address is refused.
- **The private-network allowance never opens cloud metadata.** The developer flag
  exists so the loopback fixture server is reachable; a metadata endpoint is a
  credential source, so it stays refused even with the flag on.
- **Redirect hops are validated in the routing layer.** Each hop is its own document
  request, so the guard sees it before the browser commits to it.

#### Codex counter-review of T1–T3 (2026-08-02)

`~/.claude-sync/scripts/codex-review.sh` against commits `3a209f2..fbfb7d1`. Every
claim below was checked against the code before it was accepted or rejected.

Accepted and fixed in `70a03be`:

| Finding | Verification | Fix |
|---|---|---|
| Shared CGNAT space `100.64.0.0/10` is admitted | `ipaddress` reports it `is_private=False`, `is_reserved=False` — confirmed | `_restricted_category` now ends in deny-unless-global; a list of known-bad ranges fails open on whatever the list missed |
| Deprecated IPv6 site-local `fec0::/10` is admitted | `is_global=True` — confirmed | Named outright, since deny-unless-global does not catch it |
| IPv4-mapped metadata addresses evade the metadata set | `::ffff:100.100.100.200` unwraps into CGNAT space and was admitted — confirmed | `_normalize_address` unwraps `ipv4_mapped` before every check |
| An empty resolver answer is admitted | The per-address loop simply does not run — confirmed | An empty answer is refused |
| `origin_of` drops IPv6 brackets | `https://[2606:4700::1]/x` produced `https://2606:4700::1` — confirmed | Literals are re-bracketed; a round-trip assertion covers it |
| `accept_downloads` defaults to true, so a small download wins the race against `cancel()` | Confirmed from the Playwright default | Contexts are created with `accept_downloads=False`; the test now asserts `download.path()` raises |
| The session limit races | `len(self._sessions)` was read before several awaits — confirmed | Slots are reserved under an `asyncio.Lock`; a four-way concurrent open test asserts exactly two succeed |
| A failed open leaks its context | No `finally` covered `goto`/`identify` failure — confirmed | `except BaseException` closes the context and clears the reservation; a 1 ms-timeout test covers it |
| Browser launch races | `_browser` had no lock — confirmed | Serialized under the same lock |
| Background cleanups outlive `shutdown` | Tasks lived in a module-level set nothing awaited — confirmed | Per-driver `_cleanups` with `drain_cleanups()`, awaited by `shutdown()` |

Rejected:

- **"`::ffff:169.254.169.254` is allowed when `allow_private_network=True`."** It is
  not. The address reports `is_link_local=True`, so it is categorized link-local, and
  the allowance only opens loopback and private. The underlying concern was real
  through a different address, so the mapped-address fix landed anyway.

Deferred, with reasons:

- **Check-to-connect DNS rebinding.** Validating at check time cannot bind the
  address Chromium later connects to. Closing this needs connection-level pinning or
  a trusted proxy, which is an architecture decision rather than a Phase 1 fix.
- **Non-network scheme commits (`window.location = "about:blank"`, `data:`,
  `blob:`, `srcdoc`) bypass the route handler.** Real. Needs a frame-commit check
  that fails the session closed, and it belongs with the settle work of T8.
- **Sensitive-value enforcement in the model layer, and a sentinel-based test.**
  Correct, and it is exactly what T4 must deliver; recorded as a T4 requirement.
- **Percent-encoding canonicalization in route normalization** (`/products/123` vs
  `/products/%31%32%33`). Recorded for T7, where `page_signature` is assembled.
- **Fixed `asyncio.sleep` in the popup test.** The download test now synchronizes on
  `expect_download` and `drain_cleanups`; the popup test still sleeps.
- **Multi-hop redirect integration tests.** The fixture server serves static files
  only; adding redirect routes to it is worth doing alongside T7.

`scripts/test.sh` after the fixes: ruff format, ruff lint, pyright 0 errors,
121 tests passed.

#### Codex counter-review, second pass (2026-08-02)

The first review was captured through `tail -120`, which truncated its three
highest-severity findings. It was re-run with the output written to a file. The
re-run examined the pre-fix tree, so its finding 5 (the session-limit race) was
already fixed by `70a03be`. Three findings were new, and all three were confirmed.

Accepted and fixed in `d05c779`:

| Finding | Verification | Fix |
|---|---|---|
| **Critical.** The private-network allowance was agent-supplied. `OpenSessionRequest.allow_private_network` reached `UrlPolicy` unchanged | Specification 10.2 calls it a *developer-only* flag and 12.1 gives `open_session` a URL and nothing else — confirmed against the specification | The field is removed from `OpenSessionRequest` and lives in `DriverSettings`, which comes from trusted local configuration. A test asserts the field cannot be set on a request at all |
| **Critical.** Only document requests passed the guard; `resource_type != "document"` returned early | Confirmed in the route handler | Every request is judged. A test drives `fetch('http://169.254.169.254/...')` from a loopback-allowed page and asserts it is refused and recorded as a blocked `subresource` |
| **Critical.** Service-worker traffic can bypass the routing layer | Playwright routes service-worker requests separately — confirmed from the context option | Contexts are created with `service_workers="block"` |
| **High.** `identify()` read token, URL, language, and title across separate awaits | Confirmed: a navigation landing between them yields document A's ID beside document B's URL | One `evaluate` gathers all four, the token is re-read to confirm the document did not move, and the read is retried up to three times |

Still deferred, and now the most important open risk:

- **Check-to-connect DNS rebinding.** `UrlPolicy` resolves with `socket.getaddrinfo`
  while Chromium resolves independently, so the address checked is not provably the
  address connected to. Repeating resolution does not close this; the existing
  rebinding tests only prove the policy re-resolves, not that the browser connected
  where the policy looked. Closing it needs the connected peer address — Playwright
  exposes `response.server_addr()` — checked against the same rules, with the session
  failed closed on a mismatch, or host-to-IP pinning at browser launch. Recorded as a
  T8 requirement rather than improvised here.
- **Non-network scheme commits** (`window.location = "about:blank"`, `data:`,
  `blob:`, `srcdoc`) still bypass the route handler; the frame-commit check belongs
  with the same T8 work.

`scripts/test.sh`: ruff format, ruff lint, pyright 0 errors, 122 tests passed.

### T4: element normalization and label resolution — done (2026-08-02)

- `src/wcc/driver/page_script.py`: the DOM extraction script, kept as data. It
  reports signals and decides nothing, except one refusal — see below.
- `src/wcc/driver/normalize.py`: label priority (8.1), eligibility (8.2), control
  classification, confidence (7.4), and locator-candidate collection (7.6).
- `src/wcc/driver/playwright_driver.py`: `extract_elements()`.
- `fixtures/sensitive-form/`: a checkout form where every sensitive control holds a
  distinct sentinel value.
- Tests: `tests/test_normalize.py` (browser-free rules) and four browser tests
  covering sentinel absence, deliberate omission, hidden-input exclusion, and the
  label coverage acceptance criterion 1 asks for.
- `scripts/test.sh`: ruff format, ruff lint, pyright 0 errors, 164 tests passed.

**A Phase 0 defect found and fixed here.** `LabelSource` had the specification 8.1
order wrong: `aria-labelledby` outranked a wrapping `<label>`, and the eighth member
was `title`, which specification 8.1 does not list at all. The correct eighth source
is a nearby heading or fieldset legend. Members are now in specification order and
`TITLE` is replaced by `LEGEND_OR_HEADING`. Nothing consumed the old order yet, so
no migration is needed beyond this note.

Schema change (specification rule 7): `ControlType` gains `password`, `file`, and
`hidden`. These are additive; a reader written against the earlier list must treat
an unknown control type as `other`. `SCHEMA_VERSION` is deliberately **not** bumped:
it tracks the specification document version, and this adds no field. The reason for
the change is that specification 7.2 forbids capturing these values, and a type that
cannot be named cannot be checked.

Decisions worth carrying forward:

- **Sensitive values are refused twice, independently.** The page script never reads
  `.value` for a password, payment, security-code, token-like, or file control, so
  the value never enters the process; `NormalizedElement` then rejects any such value
  outright. One barrier is a convention a later edit can quietly break.
- **The sentinel test asserts absence across the whole serialized payload**, not the
  `value` field alone, so a leak through a label, a locator key, or any future field
  fails it and names the sentinel. The earlier fixture test only checked that inputs
  existed, which a careless extractor would also pass.
- **Hidden inputs are excluded entirely.** Specification 8.2 permits keeping a safe
  semantic value, but nothing in Phase 1 reads one, and an element retained without
  a reader is a value waiting to be recorded by accident.
- **Element IDs are minted per observation, never derived from the DOM.** A
  DOM-derived ID would leak page structure to an agent that must never receive a
  selector (specification 10.3).

Unresolved:

- Locator chains are collected but not yet re-resolved against a live page; that is
  T5, together with `execute_action`.

### T5: locator candidates and unique re-resolution — done (2026-08-02)

- `src/wcc/driver/resolve.py`: in-page re-resolution for each candidate kind.
- `src/wcc/driver/playwright_driver.py`: `resolve()` walking the chain in priority
  order, and `execute_action()` for the six allowlisted actions of specification 10.4.
- `fixtures/ambiguous-controls/`: two forms whose controls share a name and a label,
  so only the id distinguishes them.
- Tests: re-resolution after reload (acceptance criterion 8), ambiguity handling,
  staleness after removal, and one test per action shape.
- `scripts/test.sh`: ruff format, ruff lint, pyright 0 errors, 171 tests passed,
  run twice to confirm stability.

Decisions worth carrying forward:

- **An ambiguous candidate is worth exactly as much as one that matched nothing.**
  Both move to the next candidate. Choosing the closest, first, or most visible
  match is how automation types into the wrong field with full confidence, and
  specification 7.7 forbids it outright.
- **Re-resolution happens per action, not once per observation.** The page may have
  changed since it was described, and specification 11.2 wants a stale target
  reported rather than guessed at.
- **The resolved element is tagged with a random in-page marker.** No selector
  crosses the driver boundary in either direction, which keeps the guarantee of
  specification 10.3 that an agent can never supply or receive one.
- **Fixed `asyncio.sleep` is gone from the browser tests.** The popup test failed
  once under load — exactly the flakiness the counter-review predicted — and now
  synchronizes on `expect_event`. The suite was run twice to confirm.

The test asserting `execute_action` was unimplemented has been retired, since it now
is. The equivalent assertion for `snapshot` remains until T7 assembles it.

### T6: collection and JSON-LD extraction — done (2026-08-02)

- `src/wcc/driver/collection_script.py`: repeated-structure detection for tables,
  lists, search-result blocks, and cards.
- `src/wcc/compiler/collections.py`: the per-record identity projection and
  `collection_hash`.
- `src/wcc/driver/playwright_driver.py`: `extract_collections()`.
- Tests: `tests/test_collections.py` (browser-free) plus three browser tests
  covering a real DOM edit changing the hash and an unchanged page not changing it.
- `scripts/test.sh`: ruff format, ruff lint, pyright 0 errors, 183 tests passed.

Decisions worth carrying forward:

- **Repetition is judged on structure, never on text.** Two cards are the same kind
  of thing because they are built the same way. Grouping on text would split one
  collection in two the moment a product name changed, which is precisely the event
  `collection_hash` exists to report.
- **The record count is hashed alongside the identities.** A result set that grew or
  shrank is then always a change, independent of how the identities sort.
- **Order affects the hash only where order is declared meaningful.** Tables, lists,
  and search results say yes; a card grid does not, so a reflow is not a result change.
- **Long field values are truncated to 200 characters for identity.** Free text is
  display data; an edited description should not read as a different record.
- **Malformed JSON-LD is skipped, not fatal.** Page content is untrusted data under
  specification 10.3, and one broken block must not fail an entire snapshot.

### T7: the three fingerprints — done (2026-08-02)

- `src/wcc/compiler/fingerprints.py`: `structural_hash`, `page_signature`, count
  buckets, and the control fingerprint.
- `src/wcc/driver/page_script.py`: landmark extraction that skips ad-like regions.
- `src/wcc/driver/playwright_driver.py`: `snapshot()` now assembles a full
  `PageSnapshot`.
- New fixtures: `filter-page/` (same controls, different page class) and
  `dynamic-results/` (an ad-like promo region plus a mutable result list).
- Tests: `tests/test_fingerprints.py` plus browser tests for acceptance criteria
  3, 4, 5, and 6.
- `scripts/test.sh`: ruff format, ruff lint, pyright 0 errors, 202 tests passed.

Decisions worth carrying forward:

- **`structural_hash` keeps element order; `page_signature` sorts it.** Moving a
  control from the header into the main form is worth re-inspecting, but two
  renderings that differ only in the order the server emitted two fields are the
  same page class.
- **`page_signature` excludes buttons and links.** Their labels track content — a
  result card's link text, a paging button's number — so counting them would give
  every search term its own page class and defeat template reuse entirely.
- **Ad-like regions are dropped at landmark extraction**, matched on id, class, and
  `data-ad*` attributes, which is what keeps criterion 4 true.

A test bug found here, not a product bug: the first version of the criterion-6 test
compared a page with no query against one with `?q=cables`. Criterion 6 is about two
*different search terms*, so the test now pushes `?q=earbuds` before the first
snapshot. The distinction is real and deliberate: query **keys** are part of the
route pattern, so `/search` and `/search?q=` are different page classes, while
`?q=earbuds` and `?q=cables` are the same one.

### Acceptance criteria status

| # | Criterion | Status |
|---|---|---|
| 1 | 95% of labelled fixture controls have correct labels and sources | Pass (T4) |
| 2 | Password, payment, token, and file values never included | Pass (T4, sentinel test) |
| 3 | Unchanged reload produces an identical structural hash | Pass (T7) |
| 4 | Ad-like changes do not change the structural hash | Pass (T7) |
| 5 | Form-structure changes change the structural hash | Pass (T7) |
| 6 | Search-term changes preserve the page signature | Pass (T7) |
| 7 | Collection changes change the collection hash | Pass (T6) |
| 8 | Every executable target re-resolves uniquely after reload | Pass (T5) |
| 9 | Disallowed schemes and private-network navigation rejected by default | Pass (T3) |

All nine pass. T8 (settle logic and resource limits, plus the two deferred security
items) and T9 (`wcc install-browser`, `wcc doctor`) remain before Phase 1 is closed.

---

## Positioning decision (2026-08-02)

Deliberated with GPT-5.6 Sol over two rounds. Full record:
`Vault/Decisions/2026-08-02-wcc-positioning.md`.

**Decided.** WCC is a compile / bind / verify layer for sites that have not declared
agent capabilities:

> Compile observable web controls into typed, evidence-backed capabilities that can
> be safely rebound and verified across sessions.

**Withdrawn.** Centring the pitch on exporting generated capabilities as WebMCP
declarations. WebMCP's imperative API registers a site's own JavaScript functions as
`execute` callbacks; DOM observation cannot recover the right internal function, its
authorization conditions, or its transaction boundary. Export survives only as a
future authoring scaffold — lint, audit, migration help, CI conformance.

**Claims moved.** Token reduction drops from "our technique" to "a benefit we
measure". The distinctive claim is the whole binding lifecycle — page-class lookup,
unique re-resolution of every target, risk re-evaluation, postcondition
verification, fail-closed on mismatch — not `page_signature` alone. "WebMCP
compatible" weakens to "WebMCP inspired" until proven.

**An implementation defect to fix before Phase 2.** `page_signature` excludes buttons
and links unconditionally (`src/wcc/compiler/fingerprints.py`), while specification
7.3 requires a normalized form/control fingerprint. The argument that unique
re-resolution contains the risk is incomplete: a submit button *replaced* by a
different-purpose button with the same id, accessible name, and in-form structure
still re-resolves uniquely. Uniqueness is not semantic equivalence.

Fix by separating structure from content rather than by including everything:
form-associated submits enter as type, role, form association, method, and action
kind; result links leave the signature and live in the collection hash; pagination
contributes the presence and kind of a navigation control with its number label
normalized away. Regression tests: a changed result link leaves the signature alone,
while a removed submit or a GET-to-POST change moves it. Doing this before Phase 2
persists any template avoids a data migration.

**Decision gates, fixed before results are seen.** Phase 4: binding success and
false-bind rates under real page variation. Phase 5: whether an external MCP agent
finds it useful. Phase 7: same-conditions comparison including Stagehand. The Phase 7
task set and evaluation conditions are frozen before the first run.

**Project goal: learning and portfolio, with the intent that it actually runs and
attracts users.** That reorders the roadmap. WCC only runs in someone else's
environment once it is an MCP server, so Phase 5 is the priority milestone and
Phases 2–4 are the shortest path to it. Phase 6 (optional semantic enrichment) waits.

**Unverified.** Stagehand's reported 80–90% input-token and ~30% cost reductions come
from a research summary, not from primary sources, and they measure a different
quantity than WCC would claim: sending a semantic tree instead of raw HTML makes each
call lighter, whereas warm binding aims to remove the call. Confirming what those
numbers actually measure is a prerequisite for designing the Phase 7 benchmark.

---

## The `page_signature` defect, fixed (2026-08-08)

Closed before Phase 2 so that no persisted template needs migrating, as the
positioning decision above required.

**What was wrong.** `page_signature` dropped every button and link
unconditionally. The exclusion was written to protect template reuse — a result
link's text and a paging button's number are content, and counting them would give
every search term its own page class — but it took the submit button with them.
A submit is the one control that says what the form *does*, so a page whose search
button had been replaced by a differently-purposed one carried an unchanged
signature and was served the old template. Unique re-resolution does not contain
this: a replacement keeping the same id, accessible name, and in-form structure
still resolves to exactly one element. Uniqueness is not semantic equivalence.

**The fix separates structure from content** rather than including everything.
Controls now fall into three groups. Ordinary fields enter whole, as before. A
form-associated submit enters as its destination — tag, role, control type, form
association, method, normalized action route, and origin relationship — and
deliberately without its label, because the label is precisely the field a
repurposed button keeps. Result links still leave the signature entirely, and
paging controls collapse to one presence marker per kind, so whether the page pages
counts while how many pages it has today does not.

**This needed the extractor, not only `fingerprints.py`.** `NormalizedElement` knew
`form_id` and nothing else about the form, and the page script never read a form's
`method` or `action`. Both now do, along with `rel` and whether a control sits
inside a navigation landmark, which is what tells a page number from a quantity
without reading the digits as content. The action is resolved against the document
base in the page and normalized to a route pattern the moment it reaches Python, so
a query value never enters the model — specification 7.2's rule about not holding
page content does not stop at input values. An action that will not parse reads as
`unknown` rather than as a guess; two unknown destinations then compare equal, which
can merge page classes that differ but never splits one that does not.

**Regressions covering it.** Unit: a changed result link leaves the signature alone,
a removed submit moves it, GET-to-POST moves it, a changed action route moves it, a
retitled submit does not, page count does not, the appearance of paging does. Driver:
the same repurposing through a real DOM, a cross-origin action, and a sentinel query
value proved absent from the whole snapshot. 223 tests green, ruff and pyright clean.

`control_fingerprint` was removed along the way — the rewrite left it with no caller.

---

### T8: settle logic and resource limits — done (2026-08-08)

**The full settle definition.** `wait_settled` waited for network idle, which
reports a page settled at the moment the last response arrives — before the
framework that asked for it has rendered anything. A snapshot taken there describes
the old page and calls it the new one. Settled now means three things at once: the
document has finished loading, nothing is in flight, and the DOM has not changed
for a quiet period. All three must hold together, so a render that triggers another
fetch restarts the wait rather than banking the quiet instant between them.

The DOM half comes from a `MutationObserver` installed as an init script, reporting
when the page last changed. The clock stays writable — it has to, to be a clock —
and a page that lies about it can only make itself look busy, which the settle
timeout already bounds. In-flight requests are counted from context events rather
than from the route handler, so a request the handler aborts is still seen through
to its failure.

**Resource limits (specification 10.5)** move into `wcc/limits.py` as
`ResourceLimits`, and `DriverSettings` keeps only the one setting that is a
permission rather than a budget. The module sits above both packages that need it:
`policy.url_policy` already imports `driver.base` for its error type, so a
`driver.base` importing anything from `policy` closes the loop through the package
`__init__` files.

The limits divide by what specification 10.5 permits on exceeding them. Executable
evidence raises `LimitExceededError` — the element count, and a redirect chain
longer than the allowance, which is refused at the hop and walked from the request
so two navigations in one session are two chains. Display data truncates and says
so in `PageSnapshot.warnings`: collections and their records. A truncated
collection keeps its original `record_count` and `collection_hash`, because both
describe the page rather than the trim, and a postcondition comparing them must not
read our truncation as the result set having shrunk.

**Both deferred security items are closed, and both fail the session closed.** A
breach is only a boundary if surviving it is impossible: a page that reached a
refused address or committed an unchecked document has already had its effect, and
continuing to observe it would let a capability rest on evidence gathered after the
fact. `_session` raises `SessionFailedError` from then on, so every entry point is
covered by one check.

- *Check-to-connect DNS rebinding.* The policy resolves with `getaddrinfo` while
  Chromium resolves independently, so the address checked was never provably the
  address reached, and re-resolving only proves the policy keeps getting the same
  answer. The connected peer settles it: `response.server_addr()` is judged by the
  same guard, as a bare address, so no second policy can drift out of step with the
  first. In practice the breach lands on the first response, so `open_session`
  refuses rather than returning a session that already reached the address.
- *Non-network document commits.* `window.location = "about:blank"`, and the same
  move with `data:`, `blob:`, or `srcdoc`, commits a document without a network
  request, so the route handler — a network boundary — never sees it. A
  `framenavigated` check on the main frame catches the commit. `about:blank` is
  exempt only before the session's first document exists, since that is where every
  context starts.

Tests: DOM quiet is waited for and an endless animation times out; the element
limit refuses a snapshot while records truncate with a stated warning; an
`about:blank` commit and a rebinding guard each fail the session closed. 230 tests
green, ruff and pyright clean.

Phase 1 now has only T9 (`wcc install-browser`, `wcc doctor`) outstanding.
