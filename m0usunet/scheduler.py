"""Background scheduler for delayed message sending."""

import logging
import threading
import time
from typing import Callable, Optional

from .db import (
    add_message, get_connection, get_contact, get_pending_scheduled,
    mark_scheduled_failed, mark_scheduled_sent,
)
from .relay import send_message
from .exceptions import RelayError

log = logging.getLogger(__name__)

CHECK_INTERVAL = 30  # seconds
MAX_ATTEMPTS = 3

_on_sent_callback: Optional[Callable[[str, str], None]] = None


def set_on_sent(callback: Optional[Callable[[str, str], None]]) -> None:
    """Register a callback(contact_name, body) for when a scheduled message fires."""
    global _on_sent_callback
    _on_sent_callback = callback


def _process_due_messages() -> int:
    """Send all due scheduled messages. Returns count sent."""
    with get_connection() as conn:
        due = get_pending_scheduled(conn)

    sent = 0
    for sched in due:
        with get_connection() as conn:
            contact = get_contact(conn, sched.contact_id)
        if not contact:
            with get_connection() as conn:
                mark_scheduled_failed(conn, sched.id, "contact not found")
            continue

        if sched.attempts >= MAX_ATTEMPTS:
            with get_connection() as conn:
                mark_scheduled_failed(conn, sched.id, "max attempts exceeded")
            continue

        try:
            output = send_message(contact.display_name, sched.body)
            success = True
        except RelayError as e:
            output = str(e)
            success = False

        if success:
            platform_str = "sms"
            if "imessage" in output.lower() or "imsg" in output.lower():
                platform_str = "imessage"

            with get_connection() as conn:
                add_message(
                    conn, sched.contact_id, platform_str, "out", sched.body,
                    delivered=True, relay_output=output,
                )
                mark_scheduled_sent(conn, sched.id)
            log.info("Scheduled message sent: %s -> %s", contact.display_name, sched.body[:40])
            sent += 1
            if _on_sent_callback:
                try:
                    _on_sent_callback(contact.display_name, sched.body)
                except Exception:
                    pass
        else:
            with get_connection() as conn:
                # Increment attempts; mark failed if at max
                conn.execute(
                    "UPDATE scheduled_messages SET attempts = attempts + 1, last_error = ? "
                    "WHERE id = ?",
                    (output, sched.id),
                )
                conn.commit()
            log.warning("Scheduled send failed for %s: %s", contact.display_name, output)

    return sent


def run_forever(interval: float = CHECK_INTERVAL) -> None:
    """Blocking loop that checks for due messages."""
    log.info("Scheduler started (interval=%ds)", interval)
    while True:
        try:
            _process_due_messages()
        except Exception:
            log.exception("Scheduler error")
        time.sleep(interval)


def start_background(interval: float = CHECK_INTERVAL) -> threading.Thread:
    """Start scheduler in a daemon thread."""
    t = threading.Thread(target=run_forever, args=(interval,), daemon=True)
    t.start()
    log.info("Scheduler background thread started")
    return t
