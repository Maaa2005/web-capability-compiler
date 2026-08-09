# Schemas

`schemas/` holds JSON Schema documents generated from the Pydantic models. The
models are the source of truth; these files are a build product, published so that
an external reader can depend on a shape without importing Python.

## Regenerating

```bash
uv run python scripts/gen_schemas.py          # write
uv run python scripts/gen_schemas.py --check  # fail if the committed files differ
```

CI runs `--check`. A job that regenerated the artifact it is meant to police would
report success on a commit where a model and its published schema had come apart.

Output is deterministic: keys are sorted and every file ends with a newline, so two
runs on two machines produce identical bytes and any diff means a model actually
changed. Specification section 0, rule 7 forbids changing schemas silently, and an
artifact that reordered itself between runs would bury every real change in noise.

## What is published, and what is not

| File | Model | Why it is public |
|---|---|---|
| `open-session-request.schema.json` | `OpenSessionRequest` | MCP tool input |
| `session-info.schema.json` | `SessionInfo` | MCP tool output |
| `page-snapshot.schema.json` | `PageSnapshot` | persisted observation |
| `normalized-element.schema.json` | `NormalizedElement` | inside a snapshot |
| `collection-snapshot.schema.json` | `CollectionSnapshot` | inside a snapshot |
| `page-ir.schema.json` | `PageIR` | persisted reading of a snapshot |
| `entity.schema.json` | `Entity` | referenced by `entity_id` arguments |
| `capability-template.schema.json` | `CapabilityTemplate` | the portable capability file of 7.13 |
| `bound-capability.schema.json` | `BoundCapability` | MCP tool output |
| `compilation-report.schema.json` | `CompilationReport` | what one compilation produced |

Internal models are deliberately absent. `LocatorChain` and `LocatorCandidate` are
never returned through MCP (specification 7.6) and are still moving, and publishing
a schema is a promise not to change it quietly.

## `additionalProperties: false`

Every generated schema carries it, because `WCCModel` sets `extra="forbid"`. This
is the same rule specification 11.1 step 4 applies when validating execution
arguments. A schema that tolerated unknown keys would let an imported capability
file carry a field past validation and have it ignored rather than rejected —
and specification 7.13 is explicit that imported JSON is never executable merely
because it parsed.

## Versioning

Portable models carry `schema_version` (currently `0.2.1`), which moves when a
model's shape changes. The SQLite database carries its own `user_version`, which
moves when the DDL changes. They are separate on purpose: tying an index change to
the version stamped on exported templates would force a migration on readers who
were never affected.
