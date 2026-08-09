"""Where WCC keeps its data.

The MCP server is started by whatever client launches it, from whatever working
directory that client happens to have. A relative path would scatter one database
per directory, and ``wcc clear-data`` — which specification 18 requires to remove
*all* local WCC data — could not then find them all. So the location is derived
from the operating system rather than from the process's surroundings.

``WCC_DATA_DIR`` overrides it, absolute paths only. A relative override would
reintroduce exactly the problem the default avoids, and silently: it would work in
testing, where the shell sits in the project, and scatter databases in production,
where the client does not.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

DATA_DIR_ENV = "WCC_DATA_DIR"
DATABASE_NAME = "wcc.sqlite3"

#: Every file WCC creates inside its data directory. ``clear-data`` removes these
#: rather than the directory, because the override may point somewhere shared and
#: deleting a directory WCC did not create is not ours to do.
OWNED_FILES = (DATABASE_NAME, f"{DATABASE_NAME}-wal", f"{DATABASE_NAME}-shm")


class DataDirectoryError(Exception):
    """The configured data directory cannot be used, and why."""


def default_data_dir() -> Path:
    """The OS-conventional per-user data directory for this application."""
    if sys.platform == "darwin":
        return Path.home() / "Library" / "Application Support" / "wcc"
    if sys.platform == "win32":
        base = os.environ.get("LOCALAPPDATA") or str(Path.home() / "AppData" / "Local")
        return Path(base) / "wcc"
    xdg = os.environ.get("XDG_DATA_HOME")
    base = Path(xdg) if xdg else Path.home() / ".local" / "share"
    return base / "wcc"


def resolve_data_dir(environ: dict[str, str] | None = None) -> Path:
    """Return the data directory, honouring ``WCC_DATA_DIR``.

    Rejects rather than repairs. An override that is blank, relative, or a file is
    a configuration mistake, and quietly falling back to the default would hide it
    until someone wondered where their templates went.
    """
    env = os.environ if environ is None else environ
    raw = env.get(DATA_DIR_ENV)
    if raw is None:
        return default_data_dir()

    if not raw.strip():
        raise DataDirectoryError(f"{DATA_DIR_ENV} is set but empty; unset it or give a path")
    candidate = Path(raw).expanduser()
    if not candidate.is_absolute():
        raise DataDirectoryError(
            f"{DATA_DIR_ENV}={raw!r} must be an absolute path: the server is started "
            "from an arbitrary working directory, so a relative path names a "
            "different place each time"
        )
    if candidate.exists() and not candidate.is_dir():
        raise DataDirectoryError(f"{DATA_DIR_ENV}={raw!r} exists and is not a directory")
    return candidate


def database_path(environ: dict[str, str] | None = None) -> Path:
    """The database file, creating its directory if needed."""
    directory = resolve_data_dir(environ)
    try:
        directory.mkdir(parents=True, exist_ok=True)
    except OSError as exc:
        raise DataDirectoryError(f"cannot create {directory}: {exc}") from exc
    return directory / DATABASE_NAME


def owned_paths(environ: dict[str, str] | None = None) -> list[Path]:
    """Every file ``clear-data`` may remove."""
    directory = resolve_data_dir(environ)
    return [directory / name for name in OWNED_FILES]
