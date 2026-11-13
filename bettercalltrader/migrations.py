import sqlite3
from typing import Callable, Iterable, Tuple


CURRENT_SCHEMA_VERSION = "10.1.0"

_PENDING_CONFIRMATIONS_SQL = """
CREATE TABLE IF NOT EXISTS pending_confirmations (
    mint TEXT PRIMARY KEY,
    payload TEXT NOT NULL,
    created_at REAL NOT NULL,
    due_at REAL NOT NULL,
    updated_at REAL NOT NULL
)
"""


def _migration_pending_confirmations(conn: sqlite3.Connection) -> None:
    """v10.1.0: entry confirmations survive a process restart."""
    conn.execute(_PENDING_CONFIRMATIONS_SQL)


MIGRATIONS: Tuple[Tuple[str, Callable[[sqlite3.Connection], None]], ...] = (
    ("10.1.0", _migration_pending_confirmations),
)


def _version_tuple(version: str) -> Tuple[int, ...]:
    parts = []
    for chunk in str(version).split("."):
        try:
            parts.append(int(chunk))
        except ValueError:
            parts.append(0)
    return tuple(parts)


def _migrations_after(version: str) -> Iterable[Tuple[str, Callable[[sqlite3.Connection], None]]]:
    current = _version_tuple(version)
    for target, migrate in MIGRATIONS:
        if _version_tuple(target) > current:
            yield target, migrate


def ensure_schema_version(conn: sqlite3.Connection) -> str:
    conn.execute(
        """
        CREATE TABLE IF NOT EXISTS schema_version (
            version TEXT PRIMARY KEY,
            applied_at TEXT NOT NULL
        )
        """
    )
    row = conn.execute("SELECT version FROM schema_version ORDER BY rowid DESC LIMIT 1").fetchone()
    stored = str(row[0]) if row else None

    if stored is None:
        for version, migrate in MIGRATIONS:
            migrate(conn)
            conn.execute(
                "INSERT OR REPLACE INTO schema_version (version, applied_at) VALUES (?, ?)",
                (version, _now()),
            )
        conn.execute(
            "INSERT OR REPLACE INTO schema_version (version, applied_at) VALUES (?, ?)",
            (CURRENT_SCHEMA_VERSION, _now()),
        )
        conn.commit()
        return CURRENT_SCHEMA_VERSION

    applied = stored
    for version, migrate in _migrations_after(stored):
        migrate(conn)
        conn.execute(
            "INSERT OR REPLACE INTO schema_version (version, applied_at) VALUES (?, ?)",
            (version, _now()),
        )
        applied = version
    conn.commit()
    return applied


def _now() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")


def apply_migrations(db_path: str) -> str:
    conn = sqlite3.connect(db_path, timeout=15)
    try:
        version = ensure_schema_version(conn)
        conn.commit()
        return version
    finally:
        conn.close()
