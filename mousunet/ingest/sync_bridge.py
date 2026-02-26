"""imessage-sync messages.db local query (BlueBubbles relay)."""

import sqlite3
import logging
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger(__name__)

SYNC_DB = Path("/home/jackpi5/imessage-sync/messages.db")


def _epoch_ms_to_iso(ms: int) -> str:
    """Convert epoch milliseconds to ISO 8601."""
    dt = datetime.fromtimestamp(ms / 1000, tz=timezone.utc)
    return dt.isoformat()


def fetch_sync_messages(since_ms: int = 0) -> list[dict]:
    """Query the local imessage-sync messages.db for inbound messages.

    Args:
        since_ms: Epoch milliseconds. Messages with date_created > this are returned.

    Returns:
        List of dicts with: sender, text, date_created_ms, sent_at_iso, chat_guid, guid, is_from_me, platform.
    """
    if not SYNC_DB.exists():
        log.warning("imessage-sync DB not found at %s", SYNC_DB)
        return []

    try:
        conn = sqlite3.connect(str(SYNC_DB), timeout=5.0)
        conn.row_factory = sqlite3.Row
    except Exception as e:
        log.warning("Failed to open imessage-sync DB: %s", e)
        return []

    try:
        rows = conn.execute(
            "SELECT guid, chat_guid, sender, text, date_created, is_from_me "
            "FROM messages "
            "WHERE date_created > ? "
            "ORDER BY date_created ASC LIMIT 500",
            (since_ms,),
        ).fetchall()

        messages = []
        for r in rows:
            dc = r["date_created"] or 0
            messages.append({
                "guid": r["guid"],
                "chat_guid": r["chat_guid"],
                "sender": r["sender"],
                "text": r["text"],
                "date_created_ms": dc,
                "sent_at_iso": _epoch_ms_to_iso(dc) if dc else "",
                "is_from_me": bool(r["is_from_me"]),
                "platform": "imessage",
            })
        return messages
    except Exception as e:
        log.warning("imessage-sync query failed: %s", e)
        return []
    finally:
        conn.close()
