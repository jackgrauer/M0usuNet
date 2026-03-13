"""Background scheduler for delayed + outbox message sending.

Handles two queues:
1. scheduled_messages — time-delayed sends (user schedules via /at)
2. outbox — store-and-forward with exponential backoff retry

Scheduled messages move into the outbox when they come due.
The outbox handles reliable delivery with retry logic.
"""

import logging
import threading
import time

from .db import (
    add_message, enqueue_message, get_connection, get_contact,
    get_due_outbox, get_pending_scheduled,
    mark_outbox_sending, mark_outbox_sent, mark_outbox_retry,
    mark_scheduled_failed, mark_scheduled_sent,
)
from .hooks import run_hooks
from .transport import get_registry
from .exceptions import RelayError

log = logging.getLogger(__name__)

CHECK_INTERVAL = 30  # seconds
MAX_ATTEMPTS = 3


def _process_scheduled() -> int:
    """Move due scheduled messages into the outbox. Returns count enqueued."""
    with get_connection() as conn:
        due = get_pending_scheduled(conn)

    enqueued = 0
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

        # Enqueue into outbox for reliable delivery
        with get_connection() as conn:
            enqueue_message(conn, sched.contact_id, sched.body, transport="relay")
            mark_scheduled_sent(conn, sched.id)
        log.info("Scheduled -> outbox: %s -> %s", contact.display_name, sched.body[:40])
        enqueued += 1

    return enqueued


def _process_outbox() -> int:
    """Send due outbox messages via transports. Returns count sent."""
    with get_connection() as conn:
        due = get_due_outbox(conn)

    if not due:
        return 0

    registry = get_registry()
    sent = 0

    for item in due:
        with get_connection() as conn:
            contact = get_contact(conn, item.contact_id)
        if not contact:
            with get_connection() as conn:
                mark_outbox_retry(conn, item.id, "contact not found")
            continue

        # Run on_send hooks
        hook_msg = {
            "contact_id": item.contact_id,
            "contact_name": contact.display_name,
            "body": item.body,
            "transport": item.transport,
        }
        if not run_hooks("on_send", hook_msg):
            log.info("Outbox message blocked by on_send hook: %s", item.body[:40])
            with get_connection() as conn:
                mark_outbox_retry(conn, item.id, "blocked by hook")
            continue

        with get_connection() as conn:
            mark_outbox_sending(conn, item.id)

        try:
            output = registry.send(item.transport, contact.display_name, item.body)
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
                    conn, item.contact_id, platform_str, "out", item.body,
                    delivered=True, relay_output=output,
                )
                mark_outbox_sent(conn, item.id, relay_output=output)
            log.info("Outbox sent: %s -> %s (%s)", contact.display_name, item.body[:40], output[:40])
            sent += 1
        else:
            with get_connection() as conn:
                mark_outbox_retry(conn, item.id, output)
            log.warning(
                "Outbox send failed (attempt %d/%d): %s -> %s",
                item.attempts + 1, item.max_attempts, contact.display_name, output,
            )

    return sent


def run_forever(interval: float = CHECK_INTERVAL) -> None:
    """Blocking loop that processes scheduled messages and outbox."""
    log.info("Scheduler started (interval=%ds)", interval)
    while True:
        try:
            _process_scheduled()
            _process_outbox()
        except Exception:
            log.exception("Scheduler error")
        time.sleep(interval)


def start_background(interval: float = CHECK_INTERVAL) -> threading.Thread:
    """Start scheduler in a daemon thread."""
    t = threading.Thread(target=run_forever, args=(interval,), daemon=True)
    t.start()
    log.info("Scheduler background thread started")
    return t
