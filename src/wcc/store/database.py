"""The SQLite schema and how a version of it is opened (specification 17).

Two versions live here and they are not the same thing.

``SCHEMA_REVISION`` is the shape of the database — the tables and columns. It moves
when the DDL moves, and it is stored in SQLite's own ``user_version``.

``SCHEMA_VERSION`` (from :mod:`wcc.models.common`) is the shape of the models
stored inside those tables. It moves when a portable, exportable model changes.

Keeping one number for both would tie them together for no reason: adding an index
would invalidate exported templates, and adding a model field would look like a
DDL change. They are versioned separately because they are changed by different
work for different reasons.
"""

from __future__ import annotations

import sqlite3
from collections.abc import Callable
from pathlib import Path

#: The DDL below. Bump when a table or column changes, and add a migration.
SCHEMA_REVISION = 2

_EXECUTIONS_DDL = """
CREATE TABLE IF NOT EXISTS executions (
    execution_id    TEXT PRIMARY KEY,
    session_id      TEXT NOT NULL REFERENCES sessions(session_id) ON DELETE CASCADE,
    capability_id   TEXT NOT NULL,
    template_id     TEXT NOT NULL,
    status          TEXT NOT NULL,
    started_at      TEXT NOT NULL,
    completed_at    TEXT NOT NULL,
    schema_version  TEXT NOT NULL,
    payload_json    TEXT NOT NULL
);

-- Specification 12.5 retrieves a result by ID and enforces session ownership, so
-- both columns are in the index the lookup uses. Checking ownership after the
-- fetch would work and would also mean a caller learning that someone else's
-- execution exists.
CREATE INDEX IF NOT EXISTS idx_executions_owner
    ON executions (session_id, execution_id);
"""


def _add_executions(connection: sqlite3.Connection) -> None:
    """Revision 2: record every execution (specification 11.1 step 9, 16)."""
    for statement in (part.strip() for part in _EXECUTIONS_DDL.split(";")):
        if statement:
            connection.execute(statement)


#: ``revision -> migration``. The mechanism was built empty at revision 1 rather
#: than added later, under pressure, next to real user data — which is how
#: migrations get skipped. This is its first use.
MIGRATIONS: dict[int, Callable[[sqlite3.Connection], None]] = {2: _add_executions}

# Column choices follow one rule: SQL holds what is searched, owned, counted, or
# related; the JSON payload holds the model. Specification 17 names the template
# columns outright, including the usage statistics, and those are columns rather
# than JSON fields precisely because they are updated on their own — rewriting a
# whole template document to increment a counter would make every successful bind
# a chance to corrupt the template.
_DDL = """
CREATE TABLE IF NOT EXISTS sessions (
    session_id      TEXT PRIMARY KEY,
    initial_url     TEXT NOT NULL,
    created_at      TEXT NOT NULL,
    status          TEXT NOT NULL,
    schema_version  TEXT NOT NULL,
    payload_json    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS documents (
    document_id     TEXT PRIMARY KEY,
    session_id      TEXT NOT NULL REFERENCES sessions(session_id) ON DELETE CASCADE,
    origin          TEXT NOT NULL,
    normalized_route_pattern TEXT NOT NULL,
    first_seen_at   TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS page_snapshots (
    snapshot_id     TEXT PRIMARY KEY,
    session_id      TEXT NOT NULL REFERENCES sessions(session_id) ON DELETE CASCADE,
    document_id     TEXT NOT NULL,
    origin          TEXT NOT NULL,
    normalized_route_pattern TEXT NOT NULL,
    page_signature  TEXT NOT NULL,
    structural_hash TEXT NOT NULL,
    captured_at     TEXT NOT NULL,
    schema_version  TEXT NOT NULL,
    payload_json    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS page_irs (
    ir_id           TEXT PRIMARY KEY,
    session_id      TEXT NOT NULL REFERENCES sessions(session_id) ON DELETE CASCADE,
    snapshot_id     TEXT NOT NULL REFERENCES page_snapshots(snapshot_id) ON DELETE CASCADE,
    origin          TEXT NOT NULL,
    normalized_route_pattern TEXT NOT NULL,
    page_signature  TEXT NOT NULL,
    built_at        TEXT NOT NULL,
    schema_version  TEXT NOT NULL,
    payload_json    TEXT NOT NULL
);

CREATE TABLE IF NOT EXISTS capability_templates (
    template_id     TEXT PRIMARY KEY,
    origin          TEXT NOT NULL,
    normalized_route_pattern TEXT NOT NULL,
    page_signature  TEXT NOT NULL,
    name            TEXT NOT NULL,
    category        TEXT NOT NULL,
    provenance      TEXT NOT NULL,
    content_hash    TEXT NOT NULL,
    schema_version  TEXT NOT NULL,
    created_at      TEXT NOT NULL,
    last_successful_bind_at TEXT,
    last_successful_use_at  TEXT,
    success_count   INTEGER NOT NULL DEFAULT 0,
    failure_count   INTEGER NOT NULL DEFAULT 0,
    payload_json    TEXT NOT NULL
);

-- Specification 7.10 queries by origin and route, preferring an exact signature.
CREATE INDEX IF NOT EXISTS idx_templates_lookup
    ON capability_templates (origin, normalized_route_pattern, page_signature);

-- Recompiling the same page must reuse the existing row rather than pile up
-- near-duplicates that then need an arbitrary tiebreak at lookup time.
CREATE UNIQUE INDEX IF NOT EXISTS idx_templates_identity
    ON capability_templates (origin, normalized_route_pattern, page_signature, content_hash);

CREATE TABLE IF NOT EXISTS bound_capabilities (
    capability_id   TEXT PRIMARY KEY,
    template_id     TEXT NOT NULL REFERENCES capability_templates(template_id) ON DELETE CASCADE,
    session_id      TEXT NOT NULL REFERENCES sessions(session_id) ON DELETE CASCADE,
    document_id     TEXT NOT NULL,
    bound_snapshot_id TEXT NOT NULL,
    status          TEXT NOT NULL,
    bound_at        TEXT NOT NULL,
    schema_version  TEXT NOT NULL,
    payload_json    TEXT NOT NULL
);

CREATE INDEX IF NOT EXISTS idx_bindings_session
    ON bound_capabilities (session_id, status);
"""

_DDL += _EXECUTIONS_DDL


class SchemaVersionError(Exception):
    """The database or a stored payload is a version this build cannot read.

    Actionable by construction: the message says which version was found, which is
    expected, and what the user can do. A version error that only says "incompatible"
    leaves someone with a data directory and no next step.
    """


def connect(path: Path) -> sqlite3.Connection:
    """Open the database with the settings this store depends on.

    ``foreign_keys`` is off by default in SQLite, so the references declared above
    would be documentation rather than constraints without this. WAL lets a read
    proceed during a write, which matters because one process holds several
    sessions. ``busy_timeout`` turns a momentary lock into a wait instead of an
    error the caller would have to retry by hand.
    """
    connection = sqlite3.connect(path, isolation_level=None)
    connection.row_factory = sqlite3.Row
    connection.execute("PRAGMA foreign_keys = ON")
    connection.execute("PRAGMA journal_mode = WAL")
    connection.execute("PRAGMA busy_timeout = 5000")
    return connection


def migrate(connection: sqlite3.Connection) -> None:
    """Bring the database up to :data:`SCHEMA_REVISION`, or refuse to touch it.

    Each step runs inside a transaction so an interrupted migration leaves the
    database at the revision it started from. A half-migrated database that still
    claims its old revision would be read with the wrong assumptions on the next
    start, which is worse than a migration that has to be retried.
    """
    found = int(connection.execute("PRAGMA user_version").fetchone()[0])

    if found > SCHEMA_REVISION:
        raise SchemaVersionError(
            f"the data directory was written by a newer WCC (database revision "
            f"{found}, this build reads {SCHEMA_REVISION}). Upgrade WCC, or point "
            f"WCC_DATA_DIR at a different directory."
        )

    if found == 0:
        with _transaction(connection):
            # Statement by statement rather than ``executescript``: that method
            # commits whatever is open before it runs, which would silently end the
            # transaction this creation is supposed to be atomic inside.
            for statement in (part.strip() for part in _DDL.split(";")):
                if statement:
                    connection.execute(statement)
            connection.execute(f"PRAGMA user_version = {SCHEMA_REVISION}")
        return

    while found < SCHEMA_REVISION:
        migration = MIGRATIONS.get(found + 1)
        if migration is None:
            raise SchemaVersionError(
                f"no migration from database revision {found} to {found + 1}; this "
                f"data directory cannot be upgraded by this build"
            )
        with _transaction(connection):
            migration(connection)
            connection.execute(f"PRAGMA user_version = {found + 1}")
        found += 1


class _transaction:
    """An explicit transaction, since the connection is in autocommit mode.

    Autocommit is the right default here — most reads want no transaction at all —
    which makes the writes that *must* be atomic worth marking, rather than
    inheriting atomicity by accident from a mode someone may change later.
    """

    def __init__(self, connection: sqlite3.Connection) -> None:
        self._connection = connection

    def __enter__(self) -> sqlite3.Connection:
        self._connection.execute("BEGIN IMMEDIATE")
        return self._connection

    def __exit__(self, exc_type: object, exc: object, tb: object) -> None:
        if exc_type is None:
            self._connection.execute("COMMIT")
        else:
            self._connection.execute("ROLLBACK")


transaction = _transaction
