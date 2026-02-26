"""Pixel Google Messages (bugle_db) ingestion via ADB."""

import logging
import sqlite3
import subprocess
from datetime import datetime, timezone
from pathlib import Path

log = logging.getLogger(__name__)

# Paths on Pixel
BUGLE_DB = "/data/data/com.google.android.apps.messaging/databases/bugle_db"
STAGING = "/sdcard/mousunet_bugle.tmp"
LOCAL_BUGLE = Path("/tmp/mousunet_bugle_db")


def _pull_bugle_db(timeout: float = 15.0) -> bool:
    """Copy bugle_db off Pixel via ADB. Returns True on success."""
    try:
        r = subprocess.run(
            ["adb", "shell", f"su -c 'cp {BUGLE_DB} {STAGING}'"],
            capture_output=True, text=True, timeout=timeout,
        )
        if r.returncode != 0:
            log.warning("ADB su cp failed: %s", r.stderr.strip())
            return False

        r = subprocess.run(
            ["adb", "pull", STAGING, str(LOCAL_BUGLE)],
            capture_output=True, text=True, timeout=timeout,
        )
        if r.returncode != 0:
            log.warning("ADB pull failed: %s", r.stderr.strip())
            return False

        return True
    except subprocess.TimeoutExpired:
        log.warning("ADB timed out pulling bugle_db")
        return False
    except Exception as e:
        log.warning("ADB pull error: %s", e)
        return False


def _epoch_ms_to_iso(ms: int) -> str:
    """Convert epoch milliseconds to ISO 8601."""
    dt = datetime.fromtimestamp(ms / 1000, tz=timezone.utc)
    return dt.isoformat()


def _get_self_participant_id(conn: sqlite3.Connection) -> int | None:
    """Find the participant_id that represents the Pixel owner."""
    row = conn.execute(
        "SELECT participant_id FROM self_participants LIMIT 1"
    ).fetchone()
    return row[0] if row else None


def fetch_pixel_messages(since_ms: int = 0) -> list[dict]:
    """Pull bugle_db and query for messages newer than since_ms.

    Returns:
        List of dicts with: phone, text, timestamp_ms, sent_at_iso, is_from_me, guid.
    """
    if not _pull_bugle_db():
        return []

    try:
        conn = sqlite3.connect(str(LOCAL_BUGLE), timeout=5.0)
        conn.row_factory = sqlite3.Row
    except Exception as e:
        log.warning("Failed to open pulled bugle_db: %s", e)
        return []

    try:
        self_pid = _get_self_participant_id(conn)

        rows = conn.execute("""
            SELECT
                m._id AS msg_id,
                m.received_timestamp,
                m.sent_timestamp,
                m.sender_id,
                m.conversation_id,
                p.text
            FROM messages m
            JOIN parts p ON p.message_id = m._id
            WHERE p.text IS NOT NULL
              AND p.text != ''
              AND m.received_timestamp > ?
            ORDER BY m.received_timestamp ASC
            LIMIT 500
        """, (since_ms,)).fetchall()

        # Build conversation -> phone lookup
        conv_phones: dict[int, str] = {}
        for r in conn.execute("""
            SELECT cp.conversation_id, pa.normalized_destination
            FROM conversation_participants cp
            JOIN participants pa ON pa._id = cp.participant_id
            WHERE pa.normalized_destination IS NOT NULL
        """).fetchall():
            conv_phones[r[0]] = r[1]

        messages = []
        seen_ids: set[int] = set()
        for r in rows:
            msg_id = r["msg_id"]
            if msg_id in seen_ids:
                continue
            seen_ids.add(msg_id)

            conv_id = r["conversation_id"]
            phone = conv_phones.get(conv_id)
            if not phone:
                continue

            is_from_me = (r["sender_id"] == self_pid) if self_pid else False
            ts = r["received_timestamp"] or r["sent_timestamp"] or 0

            messages.append({
                "phone": phone,
                "text": r["text"],
                "timestamp_ms": ts,
                "sent_at_iso": _epoch_ms_to_iso(ts) if ts else "",
                "is_from_me": is_from_me,
                "guid": f"pixel:{msg_id}",
            })

        return messages
    except Exception as e:
        log.warning("Pixel bugle_db query failed: %s", e)
        return []
    finally:
        conn.close()
