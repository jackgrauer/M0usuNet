"""iPad SMS.db ingestion via SSH."""

import subprocess
import logging
from datetime import datetime, timezone, timedelta

log = logging.getLogger(__name__)

# Apple epoch: 2001-01-01 00:00:00 UTC
APPLE_EPOCH = datetime(2001, 1, 1, tzinfo=timezone.utc)
# Apple dates in SMS.db are nanoseconds since epoch
NANO = 1_000_000_000

QUERY = """
SELECT
    h.id AS handle_id,
    m.text,
    m.date AS apple_date,
    m.service,
    m.guid,
    m.is_from_me
FROM message m
JOIN handle h ON m.handle_id = h.ROWID
WHERE m.text IS NOT NULL
  AND m.date > ?
ORDER BY m.date ASC
LIMIT 500;
"""


def apple_ts_to_iso(apple_ns: int) -> str:
    """Convert Apple nanosecond timestamp to ISO 8601 string."""
    seconds = apple_ns / NANO
    dt = APPLE_EPOCH + timedelta(seconds=seconds)
    return dt.isoformat()


def fetch_ipad_messages(since_apple_ts: int = 0, timeout: float = 30.0) -> list[dict]:
    """SSH to iPad and query SMS.db for new messages.

    Args:
        since_apple_ts: Apple nanosecond timestamp to fetch messages after.
        timeout: SSH command timeout in seconds.

    Returns:
        List of dicts with keys: handle_id, text, apple_date, service, guid, is_from_me, sent_at_iso.
    """
    sql = QUERY.strip().replace("\n", " ")
    cmd = [
        "ssh",
        "-o", "ConnectTimeout=10",
        "-o", "ServerAliveInterval=5",
        "-o", "ServerAliveCountMax=3",
        "ipad",
        f"sqlite3 -separator '|' /var/mobile/Library/SMS/sms.db \"{sql.replace('?', str(since_apple_ts))}\""
    ]

    try:
        result = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
    except subprocess.TimeoutExpired:
        log.warning("iPad SSH timed out after %.0fs", timeout)
        return []
    except Exception as e:
        log.warning("iPad SSH failed: %s", e)
        return []

    if result.returncode != 0:
        stderr = result.stderr.strip()
        # Connection refused / host down is normal when iPad is asleep
        if "Connection refused" in stderr or "No route" in stderr or "timed out" in stderr:
            log.debug("iPad offline: %s", stderr)
        elif stderr:
            log.warning("iPad query error: %s", stderr)
        return []

    messages = []
    for line in result.stdout.strip().splitlines():
        parts = line.split("|", 5)
        if len(parts) < 6:
            continue
        handle_id, text, apple_date_str, service, guid, is_from_me = parts
        try:
            apple_date = int(apple_date_str)
        except ValueError:
            continue
        messages.append({
            "handle_id": handle_id,
            "text": text,
            "apple_date": apple_date,
            "service": service.lower(),
            "guid": guid,
            "is_from_me": is_from_me == "1",
            "sent_at_iso": apple_ts_to_iso(apple_date),
        })

    return messages
