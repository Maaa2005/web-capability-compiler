"""``schemas/`` is a build product, and these tests defend the two claims it makes.

That it matches the models, and that generating it twice gives the same bytes.
The second is not pedantry: a schema that reorders itself between runs makes every
real change invisible inside the diff noise, which is how specification section 0
rule 7 — do not silently change schemas — gets broken while CI stays green.
"""

from __future__ import annotations

import importlib.util
import json
import sys
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parent.parent


def _gen_schemas():
    spec = importlib.util.spec_from_file_location(
        "gen_schemas", REPO_ROOT / "scripts" / "gen_schemas.py"
    )
    assert spec is not None and spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["gen_schemas"] = module
    spec.loader.exec_module(module)
    return module


def test_the_committed_schemas_match_the_models() -> None:
    assert _gen_schemas().generate(check=True) == 0


def test_generation_is_byte_identical_across_runs() -> None:
    module = _gen_schemas()
    for model in module.EXPORTED.values():
        assert module.render(model) == module.render(model)


@pytest.mark.parametrize("name", ["capability-template", "compilation-report"])
def test_every_exported_schema_is_valid_json_with_a_title(name: str) -> None:
    path = REPO_ROOT / "schemas" / f"{name}.schema.json"
    schema = json.loads(path.read_text(encoding="utf-8"))
    assert schema["type"] == "object"
    assert schema["title"]


def test_a_template_schema_forbids_unknown_properties() -> None:
    """Specification 11.1 step 4 validates arguments with ``additionalProperties: false``.

    The generated model schemas carry the same rule because the base model sets
    ``extra="forbid"``. A schema that accepted unknown keys would let an imported
    capability file smuggle a field past validation and have it ignored rather
    than rejected.
    """
    schema = json.loads(
        (REPO_ROOT / "schemas" / "capability-template.schema.json").read_text(encoding="utf-8")
    )
    assert schema["additionalProperties"] is False
