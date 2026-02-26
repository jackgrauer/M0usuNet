"""Message CRUD and conversation list query."""

import sqlite3

from .models import Message, ConversationSummary


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
    cur = conn.execute(
        "INSERT INTO messages (contact_id, platform, direction, body, delivered, relay_output) "
        "VALUES (?, ?, ?, ?, ?, ?)",
        (contact_id, platform, direction, body, int(delivered), relay_output),
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
    """Insert a message with external_guid dedup. Returns True if inserted, False if duplicate."""
    if external_guid:
        existing = conn.execute(
            "SELECT 1 FROM messages WHERE external_guid = ?", (external_guid,)
        ).fetchone()
        if existing:
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
        "SELECT * FROM messages WHERE contact_id = ? ORDER BY sent_at ASC LIMIT ?",
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
            m.direction
        FROM contacts c
        JOIN messages m ON m.id = (
            SELECT id FROM messages
            WHERE contact_id = c.id
            ORDER BY sent_at DESC
            LIMIT 1
        )
        ORDER BY m.sent_at DESC
    """).fetchall()
    return [ConversationSummary(**dict(r)) for r in rows]
