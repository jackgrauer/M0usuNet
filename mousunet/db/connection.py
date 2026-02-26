"""SQLite connection management."""

import sqlite3
from contextlib import contextmanager
from typing import Generator

from ..constants import DB_PATH
from .schema import MIGRATIONS


@contextmanager
def get_connection(timeout: float = 5.0) -> Generator[sqlite3.Connection, None, None]:
    """Get a read-write connection to the MousuNet database.

    WAL mode for safe concurrent access (TUI reading while relay writes).
    """
    conn = sqlite3.connect(str(DB_PATH), timeout=timeout, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute(f"PRAGMA busy_timeout = {int(timeout * 1000)}")
    try:
        yield conn
    finally:
        conn.close()


def ensure_schema() -> None:
    """Run all pending migrations."""
    with get_connection() as conn:
        conn.execute(
            "CREATE TABLE IF NOT EXISTS schema_version ("
            "  version INTEGER PRIMARY KEY,"
            "  applied_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP"
            ")"
        )
        row = conn.execute("SELECT MAX(version) AS v FROM schema_version").fetchone()
        current = row["v"] or 0

        for version, sql in MIGRATIONS:
            if version > current:
                conn.executescript(sql)
                conn.execute("INSERT INTO schema_version (version) VALUES (?)", (version,))
                conn.commit()
