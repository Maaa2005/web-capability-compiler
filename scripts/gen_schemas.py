#!/usr/bin/env python3
"""Generate ``schemas/`` from the Pydantic models (specification 5, Phase 3).

The models are the source of truth and these files are a build product, so the
only property worth defending is that they agree. ``--check`` exists for CI: it
regenerates in memory and fails on any difference, which turns "someone changed a
model and forgot the schema" from a discovery made months later by a downstream
reader into a failed build on the commit that caused it.

Determinism is enforced rather than hoped for. Keys are sorted and the file ends
with a newline, so two runs on two machines produce byte-identical output and a
diff means a model actually changed. Specification section 0 rule 7 forbids
silently changing schemas; a build product that reordered itself would make every
real change invisible inside the noise.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path

from pydantic import BaseModel

from wcc.models.capability import BoundCapability, CapabilityTemplate
from wcc.models.compilation import CompilationReport
from wcc.models.element import CollectionSnapshot, NormalizedElement
from wcc.models.ir import Entity, PageIR
from wcc.models.session import OpenSessionRequest, PageSnapshot, SessionInfo

REPO_ROOT = Path(__file__).resolve().parent.parent
SCHEMAS_DIR = REPO_ROOT / "schemas"

#: The models an external reader is entitled to depend on: the MCP-facing shapes
#: and the persisted ones. Internal models are deliberately absent — publishing a
#: schema is a promise not to change it quietly, and locator chains are still
#: moving.
EXPORTED: dict[str, type[BaseModel]] = {
    "bound-capability": BoundCapability,
    "capability-template": CapabilityTemplate,
    "collection-snapshot": CollectionSnapshot,
    "compilation-report": CompilationReport,
    "entity": Entity,
    "normalized-element": NormalizedElement,
    "open-session-request": OpenSessionRequest,
    "page-ir": PageIR,
    "page-snapshot": PageSnapshot,
    "session-info": SessionInfo,
}


def render(model: type[BaseModel]) -> str:
    schema = model.model_json_schema(mode="serialization")
    return json.dumps(schema, indent=2, sort_keys=True, ensure_ascii=False) + "\n"


def generate(check: bool) -> int:
    SCHEMAS_DIR.mkdir(exist_ok=True)
    stale: list[str] = []

    for name, model in sorted(EXPORTED.items()):
        path = SCHEMAS_DIR / f"{name}.schema.json"
        rendered = render(model)
        if check:
            current = path.read_text(encoding="utf-8") if path.exists() else ""
            if current != rendered:
                stale.append(path.name)
            continue
        path.write_text(rendered, encoding="utf-8")

    if check and stale:
        print(
            "schemas/ is out of date with the models: " + ", ".join(stale) + "\n"
            "run: uv run python scripts/gen_schemas.py",
            file=sys.stderr,
        )
        return 1
    if not check:
        print(f"wrote {len(EXPORTED)} schemas to {SCHEMAS_DIR}")
    return 0


def main() -> int:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "--check",
        action="store_true",
        help="fail instead of writing when the committed schemas differ",
    )
    return generate(parser.parse_args().check)


if __name__ == "__main__":
    raise SystemExit(main())
