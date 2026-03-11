"""Database: schema, models, connection, contacts, and messages."""

import csv
import sqlite3
from contextlib import contextmanager
from datetime import datetime, timezone
from pathlib import Path
from typing import Generator, Optional

from pydantic import BaseModel

from .constants import DB_PATH


# ── Schema migrations ─────────────────────────────────────

MIGRATIONS: list[tuple[int, str]] = [
    (1, """
        CREATE TABLE IF NOT EXISTS contacts (
            id INTEGER PRIMARY KEY,
            display_name TEXT NOT NULL,
            phone TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS contact_sources (
            id INTEGER PRIMARY KEY,
            contact_id INTEGER NOT NULL REFERENCES contacts(id),
            platform TEXT NOT NULL,
            platform_id TEXT,
            profile_data TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        CREATE INDEX IF NOT EXISTS idx_contact_sources_contact
            ON contact_sources(contact_id);

        CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY,
            contact_id INTEGER NOT NULL REFERENCES contacts(id),
            platform TEXT NOT NULL,
            direction TEXT NOT NULL CHECK (direction IN ('in', 'out')),
            body TEXT NOT NULL,
            sent_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            delivered INTEGER DEFAULT 0,
            relay_output TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_messages_contact_time
            ON messages(contact_id, sent_at DESC);

        CREATE TABLE IF NOT EXISTS suggestions (
            id INTEGER PRIMARY KEY,
            message_id INTEGER NOT NULL REFERENCES messages(id),
            suggested_text TEXT,
            used INTEGER DEFAULT 0,
            edited_text TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
    """),
    (2, """
        CREATE TABLE IF NOT EXISTS sync_state (
            source TEXT PRIMARY KEY,
            last_synced_value TEXT NOT NULL,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        ALTER TABLE messages ADD COLUMN external_guid TEXT;
        CREATE UNIQUE INDEX IF NOT EXISTS idx_messages_external_guid
            ON messages(external_guid) WHERE external_guid IS NOT NULL;
    """),
    (3, """
        ALTER TABLE contacts ADD COLUMN last_viewed_at TIMESTAMP;
    """),
    (4, """
        ALTER TABLE contacts ADD COLUMN pinned INTEGER DEFAULT 0;
    """),
    (5, """
        CREATE TABLE attachments (
            id INTEGER PRIMARY KEY,
            message_id INTEGER NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
            filename TEXT NOT NULL,
            mime_type TEXT,
            total_bytes INTEGER DEFAULT 0,
            local_path TEXT,
            remote_path TEXT,
            download_status TEXT DEFAULT 'pending'
                CHECK (download_status IN ('pending','downloading','done','failed')),
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        CREATE INDEX idx_attachments_message ON attachments(message_id);
        ALTER TABLE messages ADD COLUMN has_attachments INTEGER DEFAULT 0;
    """),
    (6, """
        CREATE TABLE scheduled_messages (
            id INTEGER PRIMARY KEY,
            contact_id INTEGER NOT NULL REFERENCES contacts(id),
            body TEXT NOT NULL,
            scheduled_at TEXT NOT NULL,
            status TEXT DEFAULT 'pending'
                CHECK (status IN ('pending','sent','failed','cancelled')),
            attempts INTEGER DEFAULT 0,
            last_error TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            sent_at TIMESTAMP
        );
        CREATE INDEX idx_scheduled_pending
            ON scheduled_messages(status, scheduled_at)
            WHERE status = 'pending';
    """),
    (7, """
        ALTER TABLE contacts ADD COLUMN muted INTEGER DEFAULT 0;
    """),
]


# ── Models ────────────────────────────────────────────────

class Contact(BaseModel):
    id: int
    display_name: str
    phone: Optional[str] = None
    created_at: Optional[datetime] = None
    last_viewed_at: Optional[datetime] = None
    pinned: bool = False
    muted: bool = False


class Message(BaseModel):
    id: int
    contact_id: int
    platform: str
    direction: str  # "in" or "out"
    body: str
    sent_at: Optional[datetime] = None
    delivered: bool = False
    relay_output: Optional[str] = None
    external_guid: Optional[str] = None
    has_attachments: bool = False


class Attachment(BaseModel):
    id: int
    message_id: int
    filename: str
    mime_type: Optional[str] = None
    total_bytes: int = 0
    local_path: Optional[str] = None
    remote_path: Optional[str] = None
    download_status: str = "pending"


class ScheduledMessage(BaseModel):
    id: int
    contact_id: int
    body: str
    scheduled_at: str
    status: str = "pending"
    attempts: int = 0
    last_error: Optional[str] = None
    created_at: Optional[datetime] = None
    sent_at: Optional[datetime] = None


class ConversationSummary(BaseModel):
    contact_id: int
    display_name: str
    phone: Optional[str] = None
    platform: str
    last_message: str
    last_time: Optional[datetime] = None
    direction: str
    unread_count: int = 0
    pinned: bool = False
    muted: bool = False


# ── Connection ────────────────────────────────────────────

_shared_conn: sqlite3.Connection | None = None


def _init_conn(timeout: float = 30.0) -> sqlite3.Connection:
    conn = sqlite3.connect(str(DB_PATH), timeout=timeout, check_same_thread=False)
    conn.row_factory = sqlite3.Row
    conn.execute("PRAGMA journal_mode = WAL")
    conn.execute("PRAGMA foreign_keys = ON")
    conn.execute(f"PRAGMA busy_timeout = {int(timeout * 1000)}")
    return conn


@contextmanager
def get_connection(timeout: float = 30.0) -> Generator[sqlite3.Connection, None, None]:
    """Get a shared read-write connection to the m0usunet database.

    Reuses a single connection (WAL mode is safe for concurrent reads).
    """
    global _shared_conn
    if _shared_conn is None:
        _shared_conn = _init_conn(timeout)
    try:
        yield _shared_conn
    except sqlite3.OperationalError:
        # Lock contention — connection is fine, just re-raise
        raise
    except Exception:
        # Connection broken — reset and re-raise
        _shared_conn = None
        raise


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


# ── Contacts ──────────────────────────────────────────────

def get_contact(conn: sqlite3.Connection, contact_id: int) -> Contact | None:
    row = conn.execute("SELECT * FROM contacts WHERE id = ?", (contact_id,)).fetchone()
    if row:
        return Contact(**dict(row))
    return None


def get_contact_by_name(conn: sqlite3.Connection, name: str) -> Contact | None:
    row = conn.execute(
        "SELECT * FROM contacts WHERE display_name = ? COLLATE NOCASE", (name,)
    ).fetchone()
    if row:
        return Contact(**dict(row))
    return None


def upsert_contact(conn: sqlite3.Connection, name: str, phone: str | None = None) -> int:
    """Insert or update a contact. Returns the contact id."""
    existing = get_contact_by_name(conn, name)
    if existing:
        if phone and phone != existing.phone:
            conn.execute("UPDATE contacts SET phone = ? WHERE id = ?", (phone, existing.id))
            conn.commit()
        return existing.id
    cur = conn.execute(
        "INSERT INTO contacts (display_name, phone) VALUES (?, ?)", (name, phone)
    )
    conn.commit()
    return cur.lastrowid


def search_contacts(conn: sqlite3.Connection, query: str) -> list[Contact]:
    """Search contacts by display_name or phone using LIKE %query%."""
    pattern = f"%{query}%"
    rows = conn.execute(
        "SELECT * FROM contacts WHERE display_name LIKE ? OR phone LIKE ? ORDER BY display_name",
        (pattern, pattern),
    ).fetchall()
    return [Contact(**dict(r)) for r in rows]



def import_tsv(path: Path) -> int:
    """Import contacts from a name\\tphone TSV file. Returns count imported."""
    count = 0
    with get_connection() as conn, open(path, newline="") as f:
        reader = csv.reader(f, delimiter="\t")
        for row in reader:
            if len(row) < 2:
                continue
            name, phone = row[0].strip(), row[1].strip()
            if not name or not phone:
                continue
            # Skip non-phone entries (service codes like *225#, 411, 611)
            if not phone.startswith("+"):
                continue
            upsert_contact(conn, name, phone)
            count += 1
    return count


def mark_viewed(conn: sqlite3.Connection, contact_id: int) -> None:
    """Update last_viewed_at to now for a contact."""
    conn.execute(
        "UPDATE contacts SET last_viewed_at = CURRENT_TIMESTAMP WHERE id = ?",
        (contact_id,),
    )
    conn.commit()


def toggle_pin(conn: sqlite3.Connection, contact_id: int) -> bool:
    """Toggle pinned status for a contact. Returns new pinned state."""
    row = conn.execute("SELECT pinned FROM contacts WHERE id = ?", (contact_id,)).fetchone()
    if row:
        new_val = 0 if row["pinned"] else 1
        conn.execute("UPDATE contacts SET pinned = ? WHERE id = ?", (new_val, contact_id))
        conn.commit()
        return bool(new_val)
    return False


def toggle_mute(conn: sqlite3.Connection, contact_id: int) -> bool:
    """Toggle muted status for a contact. Returns new muted state."""
    row = conn.execute("SELECT muted FROM contacts WHERE id = ?", (contact_id,)).fetchone()
    if row:
        new_val = 0 if row["muted"] else 1
        conn.execute("UPDATE contacts SET muted = ? WHERE id = ?", (new_val, contact_id))
        conn.commit()
        return bool(new_val)
    return False


# ── Messages ──────────────────────────────────────────────

def add_message(
    conn: sqlite3.Connection,
    contact_id: int,
    platform: str,
    direction: str,
    body: str,
    delivered: bool = False,
    relay_output: str | None = None,
) -> int:
    """Insert a message. Returns the message id."""
    now = datetime.now(timezone.utc).isoformat()
    cur = conn.execute(
        "INSERT INTO messages (contact_id, platform, direction, body, delivered, relay_output, sent_at) "
        "VALUES (?, ?, ?, ?, ?, ?, ?)",
        (contact_id, platform, direction, body, int(delivered), relay_output, now),
    )
    conn.commit()
    return cur.lastrowid


def add_message_with_guid(
    conn: sqlite3.Connection,
    contact_id: int,
    platform: str,
    direction: str,
    body: str,
    sent_at: str = "",
    external_guid: str | None = None,
    delivered: bool = True,
) -> bool:
    """Insert a message with dedup. Returns True if inserted, False if duplicate."""
    # Exact guid dedup
    if external_guid:
        existing = conn.execute(
            "SELECT 1 FROM messages WHERE external_guid = ?", (external_guid,)
        ).fetchone()
        if existing:
            return False

    # Fuzzy dedup for outbound messages: relay records the sent message, then
    # Pixel/iPad ingest finds the same message later. Match on first 50 chars
    # of body + same contact + within 2 minutes.
    if direction == "out" and sent_at:
        prefix = body[:50]
        fuzzy = conn.execute(
            "SELECT id FROM messages "
            "WHERE contact_id = ? AND direction = 'out' "
            "AND SUBSTR(body, 1, 50) = ? "
            "AND ABS(CAST((julianday(sent_at) - julianday(?)) * 86400 AS INTEGER)) < 120",
            (contact_id, prefix, sent_at),
        ).fetchone()
        if fuzzy:
            # Stamp the existing message with the ingest guid so future polls skip it
            if external_guid:
                conn.execute(
                    "UPDATE messages SET external_guid = ? WHERE id = ? AND external_guid IS NULL",
                    (external_guid, fuzzy["id"]),
                )
                conn.commit()
            return False

    if sent_at:
        conn.execute(
            "INSERT INTO messages (contact_id, platform, direction, body, delivered, sent_at, external_guid) "
            "VALUES (?, ?, ?, ?, ?, ?, ?)",
            (contact_id, platform, direction, body, int(delivered), sent_at, external_guid),
        )
    else:
        conn.execute(
            "INSERT INTO messages (contact_id, platform, direction, body, delivered, external_guid) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (contact_id, platform, direction, body, int(delivered), external_guid),
        )
    conn.commit()
    return True


def get_messages(
    conn: sqlite3.Connection, contact_id: int, limit: int = 200
) -> list[Message]:
    """Get messages for a contact, oldest first."""
    rows = conn.execute(
        "SELECT * FROM messages WHERE contact_id = ? ORDER BY datetime(sent_at) ASC LIMIT ?",
        (contact_id, limit),
    ).fetchall()
    return [Message(**dict(r)) for r in rows]


def delete_messages_for_contact(conn: sqlite3.Connection, contact_id: int) -> int:
    """Delete all messages for a contact. Returns count deleted."""
    cur = conn.execute("DELETE FROM messages WHERE contact_id = ?", (contact_id,))
    conn.commit()
    return cur.rowcount


def conversation_list(conn: sqlite3.Connection) -> list[ConversationSummary]:
    """Get all conversations sorted by most recent message."""
    rows = conn.execute("""
        SELECT
            c.id AS contact_id,
            c.display_name,
            c.phone,
            m.platform,
            m.body AS last_message,
            m.sent_at AS last_time,
            m.direction,
            c.pinned,
            c.muted,
            (SELECT COUNT(*) FROM messages
             WHERE contact_id = c.id AND direction = 'in'
             AND sent_at > COALESCE(c.last_viewed_at, '1970-01-01')
            ) AS unread_count
        FROM contacts c
        JOIN messages m ON m.id = (
            SELECT id FROM messages
            WHERE contact_id = c.id
            ORDER BY datetime(sent_at) DESC
            LIMIT 1
        )
        ORDER BY c.pinned DESC, datetime(m.sent_at) DESC
    """).fetchall()
    return [ConversationSummary(**dict(r)) for r in rows]


# ── Attachments ──────────────────────────────────────────

def add_attachment(
    conn: sqlite3.Connection,
    message_id: int,
    filename: str,
    mime_type: str | None = None,
    total_bytes: int = 0,
    remote_path: str | None = None,
) -> int:
    """Insert an attachment record. Returns the attachment id."""
    cur = conn.execute(
        "INSERT INTO attachments (message_id, filename, mime_type, total_bytes, remote_path) "
        "VALUES (?, ?, ?, ?, ?)",
        (message_id, filename, mime_type, total_bytes, remote_path),
    )
    conn.execute("UPDATE messages SET has_attachments = 1 WHERE id = ?", (message_id,))
    conn.commit()
    return cur.lastrowid


def update_attachment_status(
    conn: sqlite3.Connection, att_id: int, status: str, local_path: str | None = None,
) -> None:
    """Update download status (and optionally local_path) for an attachment."""
    if local_path:
        conn.execute(
            "UPDATE attachments SET download_status = ?, local_path = ? WHERE id = ?",
            (status, local_path, att_id),
        )
    else:
        conn.execute(
            "UPDATE attachments SET download_status = ? WHERE id = ?",
            (status, att_id),
        )
    conn.commit()


def get_attachments_for_messages(
    conn: sqlite3.Connection, message_ids: list[int],
) -> dict[int, list[Attachment]]:
    """Batch fetch attachments for a set of message ids."""
    if not message_ids:
        return {}
    placeholders = ",".join("?" * len(message_ids))
    rows = conn.execute(
        f"SELECT * FROM attachments WHERE message_id IN ({placeholders}) ORDER BY id",
        message_ids,
    ).fetchall()
    result: dict[int, list[Attachment]] = {}
    for r in rows:
        att = Attachment(**dict(r))
        result.setdefault(att.message_id, []).append(att)
    return result


# ── Scheduled Messages ───────────────────────────────────

def add_scheduled_message(
    conn: sqlite3.Connection, contact_id: int, body: str, scheduled_at: str,
) -> int:
    """Insert a scheduled message. Returns the id."""
    cur = conn.execute(
        "INSERT INTO scheduled_messages (contact_id, body, scheduled_at) VALUES (?, ?, ?)",
        (contact_id, body, scheduled_at),
    )
    conn.commit()
    return cur.lastrowid


def get_pending_scheduled(conn: sqlite3.Connection) -> list[ScheduledMessage]:
    """Get all pending scheduled messages that are due."""
    rows = conn.execute(
        "SELECT * FROM scheduled_messages "
        "WHERE status = 'pending' AND scheduled_at <= datetime('now') "
        "ORDER BY scheduled_at ASC",
    ).fetchall()
    return [ScheduledMessage(**dict(r)) for r in rows]


def get_scheduled_for_contact(
    conn: sqlite3.Connection, contact_id: int,
) -> list[ScheduledMessage]:
    """Get pending scheduled messages for a contact."""
    rows = conn.execute(
        "SELECT * FROM scheduled_messages "
        "WHERE contact_id = ? AND status = 'pending' "
        "ORDER BY scheduled_at ASC",
        (contact_id,),
    ).fetchall()
    return [ScheduledMessage(**dict(r)) for r in rows]


def get_all_scheduled(conn: sqlite3.Connection, include_done: bool = False) -> list[dict]:
    """Get all scheduled messages with contact names."""
    where = "" if include_done else "WHERE s.status = 'pending'"
    order = "s.scheduled_at ASC" if not include_done else "CASE WHEN s.status = 'pending' THEN 0 ELSE 1 END, s.scheduled_at DESC"
    rows = conn.execute(
        f"SELECT s.*, c.display_name, c.phone FROM scheduled_messages s "
        f"JOIN contacts c ON c.id = s.contact_id "
        f"{where} ORDER BY {order} LIMIT 50",
    ).fetchall()
    return [dict(r) for r in rows]


def update_scheduled(
    conn: sqlite3.Connection, sched_id: int,
    body: str | None = None, scheduled_at: str | None = None,
) -> bool:
    """Update body and/or scheduled_at of a pending message. Returns True if updated."""
    row = conn.execute(
        "SELECT status FROM scheduled_messages WHERE id = ?", (sched_id,)
    ).fetchone()
    if not row or row["status"] != "pending":
        return False
    if body is not None:
        conn.execute("UPDATE scheduled_messages SET body = ? WHERE id = ?", (body, sched_id))
    if scheduled_at is not None:
        conn.execute("UPDATE scheduled_messages SET scheduled_at = ? WHERE id = ?", (scheduled_at, sched_id))
    conn.commit()
    return True


def cancel_scheduled_by_id(conn: sqlite3.Connection, sched_id: int) -> bool:
    """Cancel a single scheduled message by id. Returns True if cancelled."""
    cur = conn.execute(
        "UPDATE scheduled_messages SET status = 'cancelled' "
        "WHERE id = ? AND status = 'pending'",
        (sched_id,),
    )
    conn.commit()
    return cur.rowcount > 0


def cancel_scheduled(conn: sqlite3.Connection, contact_id: int, cancel_all: bool = False) -> int:
    """Cancel pending scheduled messages. Returns count cancelled."""
    if cancel_all:
        cur = conn.execute(
            "UPDATE scheduled_messages SET status = 'cancelled' "
            "WHERE contact_id = ? AND status = 'pending'",
            (contact_id,),
        )
    else:
        row = conn.execute(
            "SELECT id FROM scheduled_messages "
            "WHERE contact_id = ? AND status = 'pending' "
            "ORDER BY scheduled_at DESC LIMIT 1",
            (contact_id,),
        ).fetchone()
        if not row:
            conn.commit()
            return 0
        cur = conn.execute(
            "UPDATE scheduled_messages SET status = 'cancelled' WHERE id = ?",
            (row["id"],),
        )
    conn.commit()
    return cur.rowcount


def mark_scheduled_sent(conn: sqlite3.Connection, sched_id: int) -> None:
    conn.execute(
        "UPDATE scheduled_messages SET status = 'sent', sent_at = CURRENT_TIMESTAMP WHERE id = ?",
        (sched_id,),
    )
    conn.commit()


def mark_scheduled_failed(conn: sqlite3.Connection, sched_id: int, error: str) -> None:
    conn.execute(
        "UPDATE scheduled_messages SET status = 'failed', last_error = ?, "
        "attempts = attempts + 1 WHERE id = ?",
        (error, sched_id),
    )
    conn.commit()
