"""Inbound message polling loop.

Can run as a standalone daemon (mousunet ingest) or as a background
thread started by the TUI on mount.
"""

import logging
import re
import threading
import time
from typing import Callable

from ..db.connection import get_connection
from ..db.messages import add_message_with_guid
from .ipad import fetch_ipad_messages
from .sync_bridge import fetch_sync_messages
from .pixel import fetch_pixel_messages

log = logging.getLogger(__name__)

POLL_INTERVAL = 30  # seconds


def _get_sync_state(source: str) -> str:
    """Read last_synced_value for a source."""
    with get_connection() as conn:
        row = conn.execute(
            "SELECT last_synced_value FROM sync_state WHERE source = ?",
            (source,),
        ).fetchone()
        return row["last_synced_value"] if row else ""


def _set_sync_state(source: str, value: str) -> None:
    """Upsert sync state."""
    with get_connection() as conn:
        conn.execute(
            "INSERT INTO sync_state (source, last_synced_value, updated_at) "
            "VALUES (?, ?, CURRENT_TIMESTAMP) "
            "ON CONFLICT(source) DO UPDATE SET "
            "last_synced_value = excluded.last_synced_value, "
            "updated_at = excluded.updated_at",
            (source, value),
        )
        conn.commit()


def _resolve_contact(phone: str) -> int | None:
    """Find contact_id by phone number (exact or normalized)."""
    # Normalize: strip everything except digits and leading +
    normalized = re.sub(r"[^\d+]", "", phone)
    with get_connection() as conn:
        # Try exact match first
        row = conn.execute(
            "SELECT id FROM contacts WHERE phone = ?", (normalized,)
        ).fetchone()
        if row:
            return row["id"]
        # Try without +1 prefix
        if normalized.startswith("+1") and len(normalized) == 12:
            short = normalized[2:]
            row = conn.execute(
                "SELECT id FROM contacts WHERE phone LIKE ?", (f"%{short}",)
            ).fetchone()
            if row:
                return row["id"]
    return None


def poll_ipad() -> int:
    """Poll iPad SMS.db for new messages. Returns count ingested."""
    state = _get_sync_state("ipad")
    since_ts = int(state) if state else 0

    messages = fetch_ipad_messages(since_apple_ts=since_ts)
    if not messages:
        return 0

    count = 0
    max_ts = since_ts
    for msg in messages:
        if msg["is_from_me"]:
            continue  # Only ingest inbound

        contact_id = _resolve_contact(msg["handle_id"])
        if contact_id is None:
            log.debug("No contact for handle %s, skipping", msg["handle_id"])
            continue

        platform = "imessage" if "imessage" in msg["service"] else "sms"
        guid = f"ipad:{msg['guid']}"

        with get_connection() as conn:
            added = add_message_with_guid(
                conn,
                contact_id=contact_id,
                platform=platform,
                direction="in",
                body=msg["text"],
                sent_at=msg["sent_at_iso"],
                external_guid=guid,
            )
        if added:
            count += 1

        if msg["apple_date"] > max_ts:
            max_ts = msg["apple_date"]

    if max_ts > since_ts:
        _set_sync_state("ipad", str(max_ts))

    if count:
        log.info("iPad: ingested %d messages", count)
    return count


def poll_sync_bridge() -> int:
    """Poll imessage-sync messages.db for new messages. Returns count ingested."""
    state = _get_sync_state("imessage-sync")
    since_ms = int(state) if state else 0

    messages = fetch_sync_messages(since_ms=since_ms)
    if not messages:
        return 0

    count = 0
    max_ms = since_ms
    for msg in messages:
        contact_id = _resolve_contact(msg["sender"])
        if contact_id is None:
            log.debug("No contact for sender %s, skipping", msg["sender"])
            continue

        guid = f"sync:{msg['guid']}"

        with get_connection() as conn:
            added = add_message_with_guid(
                conn,
                contact_id=contact_id,
                platform=msg["platform"],
                direction="in",
                body=msg["text"],
                sent_at=msg["sent_at_iso"],
                external_guid=guid,
            )
        if added:
            count += 1

        if msg["date_created_ms"] > max_ms:
            max_ms = msg["date_created_ms"]

    if max_ms > since_ms:
        _set_sync_state("imessage-sync", str(max_ms))

    if count:
        log.info("imessage-sync: ingested %d messages", count)
    return count


def poll_pixel() -> int:
    """Poll Pixel Google Messages via ADB. Returns count ingested."""
    state = _get_sync_state('pixel')
    since_ms = int(state) if state else 0

    messages = fetch_pixel_messages(since_ms=since_ms)
    if not messages:
        return 0

    count = 0
    max_ts = since_ms
    for msg in messages:
        if msg['is_from_me']:
            continue  # Only ingest inbound

        contact_id = _resolve_contact(msg['phone'])
        if contact_id is None:
            log.debug('No contact for phone %s, skipping', msg['phone'])
            continue

        with get_connection() as conn:
            added = add_message_with_guid(
                conn,
                contact_id=contact_id,
                platform='sms',
                direction='in',
                body=msg['text'],
                sent_at=msg['sent_at_iso'],
                external_guid=msg['guid'],
            )
        if added:
            count += 1

        if msg['timestamp_ms'] > max_ts:
            max_ts = msg['timestamp_ms']

    if max_ts > since_ms:
        _set_sync_state('pixel', str(max_ts))

    if count:
        log.info('Pixel: ingested %d messages', count)
    return count


def poll_once() -> int:
    """Run one poll cycle across all sources. Returns total ingested."""
    total = 0
    total += poll_ipad()
    total += poll_sync_bridge()
    total += poll_pixel()
    return total


def run_forever(interval: float = POLL_INTERVAL) -> None:
    """Blocking loop for standalone daemon mode."""
    log.info("Ingest daemon started (interval=%ds)", interval)
    while True:
        try:
            poll_once()
        except Exception:
            log.exception("Poll cycle error")
        time.sleep(interval)


def start_background(interval: float = POLL_INTERVAL) -> threading.Thread:
    """Start polling in a daemon thread. Returns the thread."""
    t = threading.Thread(target=run_forever, args=(interval,), daemon=True)
    t.start()
    log.info("Ingest background thread started")
    return t
