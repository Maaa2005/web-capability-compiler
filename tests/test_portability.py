"""Export and import of portable capability files (specification 7.13).

Most of these are refusals, which is the right proportion. Export is a
serialization; import is an admission decision about a document someone else
wrote, and the specification's own sentence for it is that parsing successfully
is not permission to run.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from factories import a_template
from wcc.models.capability import Risk, RiskLevel, SideEffect
from wcc.models.common import SCHEMA_VERSION
from wcc.portability.exporter import FILE_FORMAT, export_templates, write_file
from wcc.portability.importer import ImportRefused, import_file


def _written(tmp_path: Path, **overrides: object) -> Path:
    template = a_template(**overrides)
    path = tmp_path / "capabilities.json"
    write_file(path, export_templates([template]))
    return path


def _tampered(tmp_path: Path, mutate) -> Path:  # type: ignore[no-untyped-def]
    """Write a valid file, then edit the JSON the way a hostile author would."""
    path = _written(tmp_path)
    payload = json.loads(path.read_text())
    mutate(payload)
    path.write_text(json.dumps(payload))
    return path


def test_a_file_round_trips(tmp_path: Path) -> None:
    """Phase 5 acceptance: export and import round-trip."""
    original = a_template()
    path = tmp_path / "capabilities.json"
    write_file(path, export_templates([original]))

    imported = import_file(path)

    assert len(imported) == 1
    restored = imported[0]
    assert restored.name == original.name
    assert restored.execution_plan_template == original.execution_plan_template
    assert restored.target_locators == original.target_locators


def test_an_imported_template_is_marked_as_someone_else_s_work(tmp_path: Path) -> None:
    """Specification 17: provenance survives, and is not taken from the file."""
    path = _tampered(tmp_path, lambda p: p["templates"][0].update({"provenance": "compiled"}))

    assert import_file(path)[0].provenance == "imported"


def test_two_exports_of_one_set_are_byte_identical(tmp_path: Path) -> None:
    """Sorted keys, so a diff on export means the templates changed."""
    template = a_template()
    first, second = tmp_path / "a.json", tmp_path / "b.json"
    exported = export_templates([template])
    write_file(first, exported)
    write_file(second, exported)

    assert first.read_text() == second.read_text()


def test_a_file_mixing_origins_is_refused_at_export(tmp_path: Path) -> None:
    with pytest.raises(ValueError, match="one origin at a time"):
        export_templates([a_template(), a_template(origin="https://other.example")])


def test_malformed_json_is_refused(tmp_path: Path) -> None:
    """Phase 5 acceptance: malformed imported plans are rejected."""
    path = tmp_path / "broken.json"
    path.write_text("{not json")

    with pytest.raises(ImportRefused, match="not valid JSON"):
        import_file(path)


def test_a_file_from_another_schema_version_is_refused(tmp_path: Path) -> None:
    path = _tampered(tmp_path, lambda p: p.update({"schema_version": "0.1.0"}))

    with pytest.raises(ImportRefused, match="schema"):
        import_file(path)


def test_a_file_of_another_format_is_refused(tmp_path: Path) -> None:
    path = _tampered(tmp_path, lambda p: p.update({"format": "something-else/1"}))

    with pytest.raises(ImportRefused, match="format"):
        import_file(path)


def test_a_plan_naming_an_action_outside_the_allowlist_is_refused(tmp_path: Path) -> None:
    """Phase 5 acceptance: unsafe imported plans are rejected.

    The step is edited in the JSON rather than built with the model, because the
    model would refuse it — and a file is exactly the path that bypasses the
    model's constructor.
    """
    path = _tampered(
        tmp_path,
        lambda p: p["templates"][0]["execution_plan_template"][0].update({"action": "evaluate"}),
    )

    with pytest.raises(ImportRefused, match="does not match the capability file schema"):
        import_file(path)


def test_a_template_claiming_a_different_origin_than_its_file_is_refused(
    tmp_path: Path,
) -> None:
    path = _tampered(
        tmp_path, lambda p: p["templates"][0].update({"origin": "https://elsewhere.example"})
    )

    with pytest.raises(ImportRefused, match="judged as one thing"):
        import_file(path)


@pytest.mark.parametrize(
    "origin",
    [
        "https://shop.example/products",
        "https://user:pw@shop.example",
        "ftp://shop.example",
        "https://",
    ],
)
def test_an_origin_that_is_not_scheme_and_host_is_refused(tmp_path: Path, origin: str) -> None:
    """An origin field carrying anything else makes every later comparison wrong."""
    path = _tampered(
        tmp_path,
        lambda p: (p.update({"origin": origin}), p["templates"][0].update({"origin": origin})),
    )

    with pytest.raises(ImportRefused):
        import_file(path)


@pytest.mark.parametrize("route", ["search", "/search?q=x", "/search#top"])
def test_a_route_pattern_that_is_not_normalized_is_refused(tmp_path: Path, route: str) -> None:
    path = _tampered(
        tmp_path, lambda p: p["templates"][0].update({"normalized_route_pattern": route})
    )

    with pytest.raises(ImportRefused, match="route pattern"):
        import_file(path)


def test_a_template_that_could_never_run_is_refused_at_the_door(tmp_path: Path) -> None:
    """Importable-and-unrunnable is a trap; say so once, when it can be acted on."""
    risky = a_template(
        risk=Risk(
            level=RiskLevel.HIGH,
            side_effect=SideEffect.PERSISTENT,
            reversible=False,
            requires_confirmation=True,
        )
    )
    path = tmp_path / "risky.json"
    write_file(path, export_templates([risky]))

    with pytest.raises(ImportRefused, match="never run"):
        import_file(path)


def test_the_file_declares_what_it_is(tmp_path: Path) -> None:
    payload = json.loads(_written(tmp_path).read_text())

    assert payload["format"] == FILE_FORMAT
    assert payload["schema_version"] == SCHEMA_VERSION
    assert payload["origin"] == "https://shop.example"
