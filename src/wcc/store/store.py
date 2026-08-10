"""Reading and writing the models (specification 17).

Every write goes through the same three steps, in this order: validate the model,
check it carries nothing that may never be persisted, then write the searchable
columns and the payload together in one statement. Every read validates again on
the way out, and checks that the columns and the payload still agree.

Reading is where that last check earns its place. A row whose ``page_signature``
column says one thing while its payload says another has been edited by something
that was not this code, and a template looked up by a column value but *executed*
from its payload would then bind against the wrong page class. Cheap to check,
impossible to notice otherwise.
"""

from __future__ import annotations

import hashlib
import json
import sqlite3
from collections.abc import Sequence
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from pydantic import ValidationError

from wcc.models.capability import BindingStatus, BoundCapability, CapabilityTemplate
from wcc.models.common import SCHEMA_VERSION, WCCModel
from wcc.models.execution import ExecutionResult
from wcc.models.ir import PageIR
from wcc.models.session import PageSnapshot, SessionInfo
from wcc.store.database import SchemaVersionError, connect, migrate, transaction
from wcc.store.guard import assert_no_sensitive_values
from wcc.store.paths import database_path


class StoreError(Exception):
    """A payload could not be stored or read back as the model it claims to be."""


def content_hash(template: CapabilityTemplate) -> str:
    """Identify a template by what it does, not by the observation that produced it.

    ``evidence`` is excluded, and that is the whole point. It names the snapshot and
    the elements a particular inspection saw, both minted fresh every time, so a
    hash including it changed on every recompile and the deduplication it exists for
    never once fired. The first version of this test passed only because it reused
    one hand-written evidence list.

    ``provenance`` is included, for the opposite reason. An imported template and a
    locally compiled one may describe the same capability, and collapsing them would
    silently relabel imported work as our own — specification 17 requires imported
    templates to keep their provenance.
    """
    payload = template.model_dump(
        mode="json",
        exclude={"template_id", "created_at", "evidence"},
    )
    encoded = json.dumps(payload, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
    return "sha256:" + hashlib.sha256(encoded.encode("utf-8")).hexdigest()


def _dump(model: WCCModel, described_as: str) -> tuple[str, dict[str, Any]]:
    payload = model.model_dump(mode="json")
    assert_no_sensitive_values(payload, described_as=described_as)
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":")), payload


def _load[ModelT: WCCModel](
    model_type: type[ModelT], row: sqlite3.Row, *, columns: Sequence[str]
) -> ModelT:
    """Rebuild a model, refusing a payload that disagrees with its own columns."""
    stored_version = row["schema_version"]
    if stored_version != SCHEMA_VERSION:
        raise SchemaVersionError(
            f"a stored {model_type.__name__} is schema {stored_version} and this build "
            f"reads {SCHEMA_VERSION}; no migration is defined, so it will not be read "
            f"rather than read with the wrong meaning"
        )
    try:
        payload = json.loads(row["payload_json"])
    except json.JSONDecodeError as exc:
        raise StoreError(f"a stored {model_type.__name__} is not valid JSON: {exc}") from exc

    try:
        model = model_type.model_validate(payload)
    except ValidationError as exc:
        raise StoreError(
            f"a stored {model_type.__name__} does not match the current model: {exc}"
        ) from exc

    for column in columns:
        if payload.get(column) != row[column]:
            raise StoreError(
                f"a stored {model_type.__name__} disagrees with its own index: column "
                f"{column}={row[column]!r} but payload {column}={payload.get(column)!r}"
            )
    return model


class Store:
    """The SQLite persistence layer of specification 17."""

    def __init__(self, path: Path | None = None) -> None:
        self._path = path or database_path()
        self._connection = connect(self._path)
        migrate(self._connection)

    @property
    def path(self) -> Path:
        return self._path

    def close(self) -> None:
        self._connection.close()

    def __enter__(self) -> Store:
        return self

    def __exit__(self, *_: object) -> None:
        self.close()

    # -- sessions and observations ----------------------------------------

    def save_session(self, session: SessionInfo) -> None:
        payload_json, _ = _dump(session, "a session")
        self._connection.execute(
            # ``INSERT OR REPLACE`` would DELETE the existing row and insert a new
            # one, and ``ON DELETE CASCADE`` follows that delete: re-saving a
            # session to update its status silently removed its documents,
            # snapshots, IRs, and bindings. Upserting touches only the columns
            # named, so nothing downstream is disturbed.
            "INSERT INTO sessions "
            "(session_id, initial_url, created_at, status, schema_version, payload_json) "
            "VALUES (?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(session_id) DO UPDATE SET "
            "initial_url = excluded.initial_url, created_at = excluded.created_at, "
            "status = excluded.status, schema_version = excluded.schema_version, "
            "payload_json = excluded.payload_json",
            (
                session.session_id,
                session.initial_url,
                session.created_at.isoformat(),
                str(session.status),
                SCHEMA_VERSION,
                payload_json,
            ),
        )

    def save_snapshot(self, snapshot: PageSnapshot) -> None:
        payload_json, _ = _dump(snapshot, f"snapshot {snapshot.snapshot_id}")
        with transaction(self._connection):
            self._connection.execute(
                "INSERT OR IGNORE INTO documents "
                "(document_id, session_id, origin, normalized_route_pattern, first_seen_at) "
                "VALUES (?, ?, ?, ?, ?)",
                (
                    snapshot.document_id,
                    snapshot.session_id,
                    snapshot.origin,
                    snapshot.normalized_route_pattern,
                    snapshot.captured_at.isoformat(),
                ),
            )
            self._connection.execute(
                "INSERT INTO page_snapshots "
                "(snapshot_id, session_id, document_id, origin, normalized_route_pattern, "
                " page_signature, structural_hash, captured_at, schema_version, payload_json) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?) "
                "ON CONFLICT(snapshot_id) DO UPDATE SET "
                "page_signature = excluded.page_signature, "
                "structural_hash = excluded.structural_hash, "
                "captured_at = excluded.captured_at, "
                "schema_version = excluded.schema_version, "
                "payload_json = excluded.payload_json",
                (
                    snapshot.snapshot_id,
                    snapshot.session_id,
                    snapshot.document_id,
                    snapshot.origin,
                    snapshot.normalized_route_pattern,
                    snapshot.page_signature,
                    snapshot.structural_hash,
                    snapshot.captured_at.isoformat(),
                    SCHEMA_VERSION,
                    payload_json,
                ),
            )

    def load_snapshot(self, snapshot_id: str) -> PageSnapshot | None:
        row = self._connection.execute(
            "SELECT * FROM page_snapshots WHERE snapshot_id = ?", (snapshot_id,)
        ).fetchone()
        if row is None:
            return None
        return _load(PageSnapshot, row, columns=("page_signature", "structural_hash", "origin"))

    def save_ir(self, ir: PageIR) -> None:
        payload_json, _ = _dump(ir, f"IR {ir.ir_id}")
        self._connection.execute(
            "INSERT INTO page_irs "
            "(ir_id, session_id, snapshot_id, origin, normalized_route_pattern, "
            " page_signature, built_at, schema_version, payload_json) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(ir_id) DO UPDATE SET "
            "page_signature = excluded.page_signature, built_at = excluded.built_at, "
            "schema_version = excluded.schema_version, payload_json = excluded.payload_json",
            (
                ir.ir_id,
                ir.session_id,
                ir.snapshot_id,
                ir.origin,
                ir.normalized_route_pattern,
                ir.page_signature,
                ir.built_at.isoformat(),
                SCHEMA_VERSION,
                payload_json,
            ),
        )

    def load_ir(self, ir_id: str) -> PageIR | None:
        row = self._connection.execute(
            "SELECT * FROM page_irs WHERE ir_id = ?", (ir_id,)
        ).fetchone()
        if row is None:
            return None
        return _load(PageIR, row, columns=("page_signature", "origin"))

    # -- templates ---------------------------------------------------------

    def save_template(self, template: CapabilityTemplate) -> CapabilityTemplate:
        """Store a template, or return the equivalent one already stored.

        Recompiling a page that has not changed produces an identical template with
        a new ID. Storing both would leave lookup choosing between duplicates and
        would split the usage statistics that specification 17 keeps, so the
        existing row wins and its history stays whole.
        """
        payload_json, _ = _dump(template, f"template {template.name}")
        digest = content_hash(template)

        # Inside one transaction: a bare SELECT-then-INSERT lets two sessions both
        # find nothing and both insert, and the loser gets a constraint error
        # instead of the "returns the equivalent stored template" contract.
        with transaction(self._connection):
            return self._insert_or_reuse(template, digest, payload_json)

    def _insert_or_reuse(
        self, template: CapabilityTemplate, digest: str, payload_json: str
    ) -> CapabilityTemplate:
        existing = self._connection.execute(
            "SELECT * FROM capability_templates WHERE origin = ? AND "
            "normalized_route_pattern = ? AND page_signature = ? AND content_hash = ?",
            (
                template.origin,
                template.normalized_route_pattern,
                template.page_signature,
                digest,
            ),
        ).fetchone()
        if existing is not None:
            return _load(
                CapabilityTemplate,
                existing,
                columns=("origin", "normalized_route_pattern", "page_signature", "name"),
            )

        self._connection.execute(
            "INSERT INTO capability_templates "
            "(template_id, origin, normalized_route_pattern, page_signature, name, category, "
            " provenance, content_hash, schema_version, created_at, payload_json) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            (
                template.template_id,
                template.origin,
                template.normalized_route_pattern,
                template.page_signature,
                template.name,
                str(template.category),
                template.provenance,
                digest,
                SCHEMA_VERSION,
                template.created_at.isoformat(),
                payload_json,
            ),
        )
        return template

    def find_templates(
        self, origin: str, normalized_route_pattern: str
    ) -> list[CapabilityTemplate]:
        """Every candidate for this page class (specification 7.10 step 1).

        Ordering is the binder's job, not SQL's: specification 7.10 step 2 states
        the preference, and leaving it here would put half the rule in a query
        string where no test would look for it.
        """
        rows = self._connection.execute(
            "SELECT * FROM capability_templates WHERE origin = ? AND normalized_route_pattern = ?",
            (origin, normalized_route_pattern),
        ).fetchall()
        return [
            _load(
                CapabilityTemplate,
                row,
                columns=("origin", "normalized_route_pattern", "page_signature", "name"),
            )
            for row in rows
        ]

    def record_bind_outcome(self, template_id: str, *, succeeded: bool) -> None:
        """Update the usage statistics specification 17 requires.

        Incremented in SQL rather than read-modify-written in Python: two sessions
        binding the same template at once would otherwise both read the old count
        and write the same new one, losing an outcome.
        """
        now = datetime.now(UTC).isoformat()
        if succeeded:
            self._connection.execute(
                "UPDATE capability_templates SET success_count = success_count + 1, "
                "last_successful_bind_at = ? WHERE template_id = ?",
                (now, template_id),
            )
        else:
            self._connection.execute(
                "UPDATE capability_templates SET failure_count = failure_count + 1 "
                "WHERE template_id = ?",
                (template_id,),
            )

    def record_successful_use(self, template_id: str) -> None:
        """Note that a template's capability ran and its effect was observed.

        Separate from :meth:`record_bind_outcome` because specification 17 keeps
        the two apart, and they answer different questions. A template that binds
        every time and never verifies is finding its targets and doing the wrong
        thing with them, and a single counter would hide exactly that.
        """
        self._connection.execute(
            "UPDATE capability_templates SET last_successful_use_at = ? WHERE template_id = ?",
            (datetime.now(UTC).isoformat(), template_id),
        )

    def usage_of(self, template_id: str) -> sqlite3.Row | None:
        return self._connection.execute(
            "SELECT success_count, failure_count, last_successful_bind_at, "
            "last_successful_use_at FROM capability_templates WHERE template_id = ?",
            (template_id,),
        ).fetchone()

    # -- bindings ----------------------------------------------------------

    def save_binding(self, binding: BoundCapability) -> None:
        payload_json, _ = _dump(binding, f"binding {binding.capability_id}")
        self._connection.execute(
            "INSERT INTO bound_capabilities "
            "(capability_id, template_id, session_id, document_id, bound_snapshot_id, "
            " status, bound_at, schema_version, payload_json) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(capability_id) DO UPDATE SET "
            "status = excluded.status, bound_at = excluded.bound_at, "
            "schema_version = excluded.schema_version, payload_json = excluded.payload_json",
            (
                binding.capability_id,
                binding.template_id,
                binding.session_id,
                binding.document_id,
                binding.bound_snapshot_id,
                str(binding.status),
                binding.bound_at.isoformat(),
                SCHEMA_VERSION,
                payload_json,
            ),
        )

    def load_binding(self, capability_id: str) -> BoundCapability | None:
        """Return the *record* of a binding, which is never itself permission.

        Whether this may be executed is a question about a live session, and
        :class:`~wcc.binding.registry.SessionRegistry` is the only thing that can
        answer it. The status is left exactly as stored so the registry, not this
        method, decides.
        """
        row = self._connection.execute(
            "SELECT * FROM bound_capabilities WHERE capability_id = ?", (capability_id,)
        ).fetchone()
        if row is None:
            return None
        return _load(BoundCapability, row, columns=("template_id", "session_id", "document_id"))

    def last_page_of(self, session_id: str) -> tuple[str, str] | None:
        """The page class a session last observed, as origin and route.

        Read from the snapshots table rather than from a live session, because
        exporting is something you do *after* the browsing: specification 7.13
        exports templates, and templates outlive the session that compiled them.
        """
        row = self._connection.execute(
            "SELECT origin, normalized_route_pattern FROM page_snapshots "
            "WHERE session_id = ? ORDER BY captured_at DESC LIMIT 1",
            (session_id,),
        ).fetchone()
        if row is None:
            return None
        return row["origin"], row["normalized_route_pattern"]

    def counts(self) -> dict[str, int]:
        """How many of each thing is stored — for diagnostics, never content.

        Specification 16 requires diagnostics not to reveal page content, and a
        function that only ever runs ``COUNT(*)`` cannot, whatever a later edit
        adds to the payloads.
        """
        tables = (
            "sessions",
            "documents",
            "page_snapshots",
            "page_irs",
            "capability_templates",
            "bound_capabilities",
            "executions",
        )
        return {
            table: int(self._connection.execute(f"SELECT COUNT(*) FROM {table}").fetchone()[0])
            for table in tables
        }

    # -- executions --------------------------------------------------------

    def save_execution(self, result: ExecutionResult) -> None:
        """Record what an execution did, including the ones that did nothing.

        Specification 16 asks for every action and observation to be recorded, and
        a refusal is an observation: "this capability was asked for and declined
        because the risk had risen" is the line an operator needs, and it is
        exactly the line a store that only kept successes would not have.
        """
        stored = result.redacted()
        payload_json, _ = _dump(stored, f"execution {stored.execution_id}")
        if not self._session_exists(stored.session_id):
            # A refusal aimed at a session this process never opened has nowhere to
            # go: ``executions.session_id`` references ``sessions``, and the insert
            # would raise an IntegrityError *instead of* returning the refusal the
            # caller asked for. The refusal is the answer; failing to file it must
            # not replace it.
            return
        with transaction(self._connection):
            self._save_execution(stored, payload_json)

    def _session_exists(self, session_id: str) -> bool:
        return (
            self._connection.execute(
                "SELECT 1 FROM sessions WHERE session_id = ?", (session_id,)
            ).fetchone()
            is not None
        )

    def _save_execution(self, result: ExecutionResult, payload_json: str) -> None:
        self._connection.execute(
            "INSERT INTO executions "
            "(execution_id, session_id, capability_id, template_id, status, "
            " started_at, completed_at, schema_version, payload_json) "
            "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?) "
            "ON CONFLICT(execution_id) DO UPDATE SET "
            "status = excluded.status, completed_at = excluded.completed_at, "
            "schema_version = excluded.schema_version, payload_json = excluded.payload_json",
            (
                result.execution_id,
                result.session_id,
                result.capability_id,
                result.template_id,
                str(result.status),
                result.started_at.isoformat(),
                result.completed_at.isoformat(),
                SCHEMA_VERSION,
                payload_json,
            ),
        )
        # Specification 17 names ``execution_steps`` beside ``executions``. The
        # steps are in the payload too, and that is not the same thing: a payload
        # cannot be queried for "every step that failed this week" without reading
        # and parsing every execution ever recorded.
        self._connection.execute(
            "DELETE FROM execution_steps WHERE execution_id = ?", (result.execution_id,)
        )
        self._connection.executemany(
            "INSERT INTO execution_steps "
            "(execution_id, step_index, action, target_ref, parameter, ran, error_code) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            [
                (
                    result.execution_id,
                    step.index,
                    str(step.action),
                    step.target_ref,
                    step.parameter,
                    1 if step.ran else 0,
                    step.error.code if step.error else None,
                )
                for step in result.steps
            ],
        )

    def load_execution(self, execution_id: str, *, session_id: str) -> ExecutionResult | None:
        """A stored result, and only to the session that produced it.

        Ownership is part of the query. Fetching first and comparing afterwards
        gives the same answer and leaks a different one: a caller that can tell
        "not yours" from "no such execution" can enumerate another session's work.
        """
        row = self._connection.execute(
            "SELECT * FROM executions WHERE execution_id = ? AND session_id = ?",
            (execution_id, session_id),
        ).fetchone()
        if row is None:
            return None
        return _load(ExecutionResult, row, columns=("session_id", "capability_id", "status"))

    def executions_of(self, session_id: str) -> list[ExecutionResult]:
        rows = self._connection.execute(
            "SELECT * FROM executions WHERE session_id = ? ORDER BY started_at",
            (session_id,),
        ).fetchall()
        return [
            _load(ExecutionResult, row, columns=("session_id", "capability_id", "status"))
            for row in rows
        ]

    def expire_bindings_from_previous_runs(self) -> int:
        """Mark every binding left active by a dead process as stale.

        A binding names a browser session, and no browser session survives the
        process that opened it. Anything still marked active at startup was
        interrupted, and leaving it that way would let a restarted server hand back
        a binding whose page closed hours ago.
        """
        with transaction(self._connection):
            cursor = self._connection.execute(
                "UPDATE bound_capabilities SET status = ?, "
                "payload_json = json_set(payload_json, '$.status', ?) WHERE status = ?",
                (
                    str(BindingStatus.STALE),
                    str(BindingStatus.STALE),
                    str(BindingStatus.ACTIVE),
                ),
            )
            return cursor.rowcount
