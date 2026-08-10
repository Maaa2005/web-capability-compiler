"""Persistence (specification 17) and the rules it has to enforce on the way."""

from __future__ import annotations

import json
import sqlite3
from collections.abc import Iterator
from datetime import UTC, datetime
from pathlib import Path

import pytest

from factories import a_binding, a_session, a_snapshot, a_template, an_ir
from wcc.models.capability import BindingStatus
from wcc.models.common import SessionStatus
from wcc.models.ir import Evidence
from wcc.store import (
    DATA_DIR_ENV,
    SCHEMA_REVISION,
    DataDirectoryError,
    SchemaVersionError,
    SensitiveValueError,
    Store,
    StoreError,
    assert_no_sensitive_values,
    content_hash,
    owned_paths,
    resolve_data_dir,
)
from wcc.store.database import BASE_DDL, connect, migrate


@pytest.fixture
def store(tmp_path: Path) -> Iterator[Store]:
    with Store(tmp_path / "wcc.sqlite3") as opened:
        yield opened


# -- where the data lives ------------------------------------------------------


def test_the_data_directory_is_not_relative_to_the_working_directory() -> None:
    """The server is launched from wherever the client happens to be."""
    assert resolve_data_dir({}).is_absolute()


def test_an_override_must_be_absolute() -> None:
    with pytest.raises(DataDirectoryError) as refused:
        resolve_data_dir({DATA_DIR_ENV: "./wcc-data"})
    assert "absolute" in str(refused.value)


@pytest.mark.parametrize("value", ["", "   "])
def test_a_blank_override_is_refused_rather_than_ignored(value: str) -> None:
    """Falling back silently would hide the mistake until data went missing."""
    with pytest.raises(DataDirectoryError):
        resolve_data_dir({DATA_DIR_ENV: value})


def test_an_override_pointing_at_a_file_is_refused(tmp_path: Path) -> None:
    target = tmp_path / "not-a-directory"
    target.write_text("")
    with pytest.raises(DataDirectoryError):
        resolve_data_dir({DATA_DIR_ENV: str(target)})


def test_clear_data_targets_wcc_files_rather_than_the_directory(tmp_path: Path) -> None:
    """The override may point somewhere shared; the directory is not ours to delete."""
    names = {path.name for path in owned_paths({DATA_DIR_ENV: str(tmp_path)})}
    assert names == {"wcc.sqlite3", "wcc.sqlite3-wal", "wcc.sqlite3-shm"}


# -- schema and versions -------------------------------------------------------


def test_a_new_database_is_stamped_with_its_revision(store: Store) -> None:
    with connect(store.path) as connection:
        assert int(connection.execute("PRAGMA user_version").fetchone()[0]) == SCHEMA_REVISION


def test_a_database_from_a_newer_build_is_refused_with_a_way_out(tmp_path: Path) -> None:
    path = tmp_path / "future.sqlite3"
    connection = connect(path)
    connection.execute(f"PRAGMA user_version = {SCHEMA_REVISION + 5}")

    with pytest.raises(SchemaVersionError) as refused:
        migrate(connection)
    message = str(refused.value)
    assert "newer WCC" in message
    assert DATA_DIR_ENV in message
    connection.close()


def test_an_existing_database_is_migrated_rather_than_rebuilt(tmp_path: Path) -> None:
    """Every revision below the current one has a path forward, and it is taken.

    Written against the real chain rather than one step of it: a build that adds a
    revision and forgets its migration passes every other test in this file, and
    fails here the moment someone opens a data directory they already had.
    """
    path = tmp_path / "old.sqlite3"
    connection = connect(path)
    # A revision-1 database as that build actually left one: the base tables, and
    # none of the tables later revisions added. Stamping an empty file would run
    # the same migrations over nothing and prove less than it appears to.
    for statement in (part.strip() for part in BASE_DDL.split(";")):
        if statement:
            connection.execute(statement)
    connection.execute("PRAGMA user_version = 1")
    connection.close()

    with Store(path) as store:
        with connect(path) as opened:
            assert int(opened.execute("PRAGMA user_version").fetchone()[0]) == SCHEMA_REVISION
        # Reachable rather than merely present: a table created by a migration is
        # only migrated if the code that reads it can.
        assert store.load_enrichment("sha256:nothing-stored-yet") is None


def test_foreign_keys_are_enforced(store: Store) -> None:
    """Declared references are documentation until the pragma is on."""
    with pytest.raises(sqlite3.IntegrityError):
        store.save_binding(a_binding(session_id="sess_never_opened"))


def test_a_payload_from_another_schema_version_is_not_read(store: Store) -> None:
    snapshot = a_snapshot()
    store.save_session(a_session(snapshot.session_id))
    store.save_snapshot(snapshot)
    with connect(store.path) as connection:
        connection.execute(
            "UPDATE page_snapshots SET schema_version = '0.1.0' WHERE snapshot_id = ?",
            (snapshot.snapshot_id,),
        )

    with pytest.raises(SchemaVersionError):
        store.load_snapshot(snapshot.snapshot_id)


def test_a_payload_that_disagrees_with_its_index_is_refused(store: Store) -> None:
    """A row looked up by column and executed from payload must not disagree."""
    snapshot = a_snapshot()
    store.save_session(a_session(snapshot.session_id))
    store.save_snapshot(snapshot)
    with connect(store.path) as connection:
        connection.execute(
            "UPDATE page_snapshots SET page_signature = 'sha256:tampered' WHERE snapshot_id = ?",
            (snapshot.snapshot_id,),
        )

    with pytest.raises(StoreError) as refused:
        store.load_snapshot(snapshot.snapshot_id)
    assert "disagrees with its own index" in str(refused.value)


def test_corrupt_json_is_reported_as_such(store: Store) -> None:
    snapshot = a_snapshot()
    store.save_session(a_session(snapshot.session_id))
    store.save_snapshot(snapshot)
    with connect(store.path) as connection:
        connection.execute(
            "UPDATE page_snapshots SET payload_json = '{not json' WHERE snapshot_id = ?",
            (snapshot.snapshot_id,),
        )

    with pytest.raises(StoreError):
        store.load_snapshot(snapshot.snapshot_id)


# -- the third barrier ---------------------------------------------------------


def test_a_password_value_is_refused_before_it_reaches_the_disk() -> None:
    """Specification 7.2's rule, checked once more where documents are written."""
    payload = {
        "elements": [
            {"control_type": "password", "value": "SENTINEL", "element_id": "el_1"},
        ]
    }
    with pytest.raises(SensitiveValueError) as refused:
        assert_no_sensitive_values(payload, described_as="a snapshot")
    assert "password" in str(refused.value)


def test_a_value_marked_omitted_but_present_is_refused() -> None:
    payload = {"control_type": "text", "value": "kept", "value_omitted_for_safety": True}
    with pytest.raises(SensitiveValueError):
        assert_no_sensitive_values(payload, described_as="an element")


def test_an_ordinary_value_is_left_alone() -> None:
    payload = {"control_type": "search", "value": "earbuds", "value_omitted_for_safety": False}
    assert_no_sensitive_values(payload, described_as="an element")


def test_the_guard_looks_inside_nested_documents() -> None:
    """Persistence writes whole documents, not the elements the extractor saw."""
    payload = {"a": {"b": [{"c": {"control_type": "file", "value": "/etc/passwd"}}]}}
    with pytest.raises(SensitiveValueError):
        assert_no_sensitive_values(payload, described_as="an IR")


# -- templates -----------------------------------------------------------------


def test_a_template_round_trips(store: Store) -> None:
    stored = store.save_template(a_template())
    found = store.find_templates(stored.origin, stored.normalized_route_pattern)
    assert [t.template_id for t in found] == [stored.template_id]
    assert found[0].target_locators == stored.target_locators


def test_recompiling_an_unchanged_page_reuses_the_stored_template(store: Store) -> None:
    """Two identical templates with different IDs must not both be kept.

    Keeping both would split the usage statistics and force lookup to tiebreak
    between rows that mean the same thing.
    """
    first = store.save_template(a_template(template_id="tmpl_first"))
    second = store.save_template(a_template(template_id="tmpl_second"))

    assert second.template_id == first.template_id
    assert len(store.find_templates(first.origin, first.normalized_route_pattern)) == 1


def test_a_changed_template_is_kept_alongside_the_old_one(store: Store) -> None:
    """History is evidence; overwriting it would discard the record of what worked."""
    first = store.save_template(a_template(template_id="tmpl_first"))
    changed = store.save_template(a_template(template_id="tmpl_second", name="search_products_v2"))

    assert changed.template_id != first.template_id
    assert len(store.find_templates(first.origin, first.normalized_route_pattern)) == 2


def test_the_content_hash_ignores_identity_and_time() -> None:
    early = a_template(template_id="tmpl_a", created_at=datetime(2026, 1, 1, tzinfo=UTC))
    late = a_template(template_id="tmpl_b", created_at=datetime(2026, 8, 1, tzinfo=UTC))
    assert content_hash(early) == content_hash(late)


def test_usage_statistics_are_recorded(store: Store) -> None:
    """Specification 17 requires success and failure counts and the last bind time."""
    template = store.save_template(a_template())
    store.record_bind_outcome(template.template_id, succeeded=True)
    store.record_bind_outcome(template.template_id, succeeded=True)
    store.record_bind_outcome(template.template_id, succeeded=False)

    usage = store.usage_of(template.template_id)
    assert usage is not None
    assert usage["success_count"] == 2
    assert usage["failure_count"] == 1
    assert usage["last_successful_bind_at"] is not None


# -- bindings ------------------------------------------------------------------


def test_a_binding_read_back_is_a_record_not_a_permission(store: Store) -> None:
    """Phase 2 acceptance: nothing persisted is executable on its own."""
    from wcc.binding import SessionRegistry

    snapshot = a_snapshot()
    store.save_session(a_session(snapshot.session_id))
    template = store.save_template(a_template())
    binding = a_binding(template_id=template.template_id, session_id=snapshot.session_id)
    store.save_binding(binding)

    loaded = store.load_binding(binding.capability_id)
    assert loaded is not None

    registry = SessionRegistry()
    assert registry.executable(loaded) is False

    registry.opened(loaded.session_id)
    assert registry.executable(loaded) is True


def test_bindings_left_active_by_a_dead_process_are_expired(store: Store) -> None:
    """No browser session survives the process that opened it."""
    snapshot = a_snapshot()
    store.save_session(a_session(snapshot.session_id))
    template = store.save_template(a_template())
    binding = a_binding(template_id=template.template_id, session_id=snapshot.session_id)
    store.save_binding(binding)

    assert store.expire_bindings_from_previous_runs() == 1

    reloaded = store.load_binding(binding.capability_id)
    assert reloaded is not None
    assert reloaded.status is BindingStatus.STALE
    # The column and the payload must not drift apart while doing it.
    with connect(store.path) as connection:
        row = connection.execute(
            "SELECT status, payload_json FROM bound_capabilities WHERE capability_id = ?",
            (binding.capability_id,),
        ).fetchone()
    assert row["status"] == json.loads(row["payload_json"])["status"] == "stale"


# -- IR ------------------------------------------------------------------------


def test_an_ir_round_trips(store: Store) -> None:
    snapshot = a_snapshot()
    store.save_session(a_session(snapshot.session_id))
    store.save_snapshot(snapshot)
    ir = an_ir(snapshot)
    store.save_ir(ir)

    loaded = store.load_ir(ir.ir_id)
    assert loaded is not None
    assert loaded.entities == ir.entities


# -- what the counter-review found ---------------------------------------------


def test_re_saving_a_session_does_not_delete_its_history(store: Store) -> None:
    """``INSERT OR REPLACE`` deletes before it inserts, and cascades follow.

    Updating a session's status used to remove every document, snapshot, IR, and
    binding that referenced it. Nothing reported an error; the data was simply gone.
    """
    snapshot = a_snapshot()
    store.save_session(a_session(snapshot.session_id))
    store.save_snapshot(snapshot)
    template = store.save_template(a_template())
    store.save_binding(a_binding(template_id=template.template_id))

    store.save_session(a_session(snapshot.session_id, status=SessionStatus.CLOSED))

    assert store.load_snapshot(snapshot.snapshot_id) is not None
    assert store.load_binding("cap_1") is not None


def test_re_saving_a_snapshot_does_not_delete_the_ir_built_from_it(store: Store) -> None:
    snapshot = a_snapshot()
    store.save_session(a_session(snapshot.session_id))
    store.save_snapshot(snapshot)
    ir = an_ir(snapshot)
    store.save_ir(ir)

    store.save_snapshot(snapshot)

    assert store.load_ir(ir.ir_id) is not None


def test_re_inspecting_the_same_page_reuses_the_template(store: Store) -> None:
    """The deduplication that never fired.

    Every inspection mints new snapshot and element IDs, so evidence differs even
    when the capability does not. Hashing the evidence meant a new row every time.
    """
    monday = store.save_template(
        a_template(
            template_id="tmpl_monday",
            evidence=[Evidence(kind="dom", element_id="el_aaa", snapshot_id="snap_1")],
        )
    )
    tuesday = store.save_template(
        a_template(
            template_id="tmpl_tuesday",
            evidence=[Evidence(kind="dom", element_id="el_bbb", snapshot_id="snap_2")],
        )
    )

    assert tuesday.template_id == monday.template_id
    assert len(store.find_templates(monday.origin, monday.normalized_route_pattern)) == 1


def test_an_imported_template_is_never_absorbed_into_a_compiled_one(store: Store) -> None:
    """Specification 17 keeps imported provenance; collapsing them would erase it."""
    compiled = store.save_template(a_template(template_id="tmpl_mine"))
    imported = store.save_template(a_template(template_id="tmpl_theirs", provenance="imported"))

    assert imported.template_id != compiled.template_id
    stored = store.find_templates(compiled.origin, compiled.normalized_route_pattern)
    assert {t.provenance for t in stored} == {"compiled", "imported"}
