import sqlite3
from typing import Optional


CURRENT_SCHEMA_VERSION = "10.0.0"


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
    if row:
        return str(row[0])
    from datetime import datetime, timezone
    conn.execute(
        "INSERT INTO schema_version (version, applied_at) VALUES (?, ?)",
        (CURRENT_SCHEMA_VERSION, datetime.now(timezone.utc).strftime("%Y-%m-%d %H:%M:%S")),
    )
    conn.commit()
    return CURRENT_SCHEMA_VERSION


def apply_migrations(db_path: str) -> str:
    conn = sqlite3.connect(db_path, timeout=15)
    try:
        version = ensure_schema_version(conn)
        conn.commit()
        return version
    finally:
        conn.close()