"""Constants, formatting utilities, and schedule-time parsing."""

from __future__ import annotations

import datetime as _dt
import re
from collections import defaultdict
from datetime import date, datetime, timezone
from pathlib import Path

from rich.style import Style
from textual.widgets import TextArea
from textual.widgets.text_area import TextAreaTheme

from ..db import Attachment, Message


# ── Schedule time parsing ─────────────────────────────────

_TIME_RE = re.compile(r"^(\d{1,2}):?(\d{2})?\s*(am|pm)\b", re.IGNORECASE)
_DATE_RE = re.compile(r"^(\d{1,2})/(\d{1,2})\b")


def _parse_schedule_time(text: str) -> tuple[str, str] | None:
    """Parse '/at 9:30am [tomorrow|M/D] message' into (ISO datetime, message).

    Returns (scheduled_at_utc_iso, message_body) or None on parse failure.
    """
    from zoneinfo import ZoneInfo
    from ..constants import USER_TZ

    text = text.strip()
    tz = ZoneInfo(USER_TZ)
    now = datetime.now(tz)

    # Parse time
    m = _TIME_RE.match(text)
    if not m:
        return None
    hour = int(m.group(1))
    minute = int(m.group(2) or 0)
    ampm = m.group(3).lower()
    if ampm == "pm" and hour != 12:
        hour += 12
    elif ampm == "am" and hour == 12:
        hour = 0

    rest = text[m.end():].strip()

    # Parse optional date modifier
    target_date = now.date()
    if rest.lower().startswith("tomorrow"):
        target_date += _dt.timedelta(days=1)
        rest = rest[8:].strip()
    else:
        dm = _DATE_RE.match(rest)
        if dm:
            month, day = int(dm.group(1)), int(dm.group(2))
            year = now.year
            candidate = _dt.date(year, month, day)
            if candidate < now.date():
                candidate = _dt.date(year + 1, month, day)
            target_date = candidate
            rest = rest[dm.end():].strip()

    if not rest:
        return None

    # Build datetime in user's timezone, convert to UTC
    local_dt = datetime(target_date.year, target_date.month, target_date.day, hour, minute, tzinfo=tz)
    # If time is in the past today and no date modifier, bump to tomorrow
    if local_dt <= now and target_date == now.date():
        local_dt += _dt.timedelta(days=1)
    utc_dt = local_dt.astimezone(timezone.utc)

    return utc_dt.strftime("%Y-%m-%d %H:%M:%S"), rest


REPLY_PATH = Path.home() / ".m0usunet" / "reply.txt"

_PHONE_RE = re.compile(r"^\+?\d[\d\s\-]{6,}$")


# ── Shared constants ─────────────────────────────────────

MESH_DEVICES = {
    "PIXEL": "192.168.0.22",
    "MINI": "192.168.0.15",
    "MAC": "192.168.0.13",
}

PLATFORM_STYLE = {
    "imessage": ("imsg", "#68a8e4"),
    "sms":      ("sms",  "#50fa7b"),
    "bumble":   ("bmbl", "#ff2d6f"),
    "hinge":    ("hnge", "#ff2d6f"),
    "tinder":   ("tndr", "#ff8c00"),
}

DEVICE_MAP = {
    "imessage": "MINI",
    "sms": "PIXEL",
}


# ── Chat theme and formatting ────────────────────────────

_CHAT_THEME = TextAreaTheme(
    "m0usunet",
    base_style=Style(color="#e0e0e0", bgcolor="#0a0a0a"),
    cursor_style=Style(color="#e0e0e0", bgcolor="#333333"),
    cursor_line_style=Style(bgcolor="#111111"),
    selection_style=Style(bgcolor="#1a3a5a"),
    syntax_styles={
        "sent": Style(color="#00d4ff"),
        "sent.name": Style(color="#00d4ff", bold=True),
        "received": Style(color="#50fa7b"),
        "received.name": Style(color="#50fa7b", bold=True),
        "separator": Style(color="#444444"),
        "timestamp": Style(color="#666666"),
        "delivery.ok": Style(color="#50fa7b"),
        "delivery.fail": Style(color="#f75341"),
        "attachment": Style(color="#ff8c00"),
        "scheduled": Style(color="#ff8c00", italic=True),
    },
)

_MSG_RE = re.compile(r"^(\s*\d+:\d+[ap]m)\s{2}(.+?):\s(.*)$")


def _to_local(dt: datetime) -> datetime:
    """Convert a datetime to local time."""
    if dt.tzinfo is not None:
        return dt.astimezone()
    return dt.replace(tzinfo=timezone.utc).astimezone()


def _date_label(d: date) -> str:
    today = date.today()
    delta = (today - d).days
    weekday = d.strftime("%A").upper()
    datestamp = d.strftime("%b %d").upper()
    if delta == 0:
        return f"TODAY \u2014 {weekday}, {datestamp}"
    elif delta == 1:
        return f"YESTERDAY \u2014 {weekday}, {datestamp}"
    elif delta < 7:
        return f"{weekday}, {datestamp}"
    else:
        return d.strftime("%A, %b %d, %Y").upper()


def _format_time(dt: datetime | None) -> str:
    if not dt:
        return "     "
    local_dt = _to_local(dt)
    return local_dt.strftime("%-I:%M%p").lower().rjust(7)


def _local_date(dt: datetime | None) -> date | None:
    if not dt:
        return None
    return _to_local(dt).date()


def _fmt_bytes(n: int) -> str:
    if n < 1024:
        return f"{n}B"
    elif n < 1024 * 1024:
        return f"{n // 1024}KB"
    else:
        return f"{n / (1024 * 1024):.1f}MB"


def _render_message(msg: Message, contact_name: str, attachments: list[Attachment] | None = None) -> list[str]:
    """Render a message as one or more plain-text lines."""
    ts = _format_time(msg.sent_at)
    if msg.direction == "out":
        sender = "you"
    else:
        sender = contact_name
    lines = [f"{ts}  {sender}: {msg.body}"]

    if attachments:
        for att in attachments:
            kind = (att.mime_type or "file").split("/")[0]
            size = _fmt_bytes(att.total_bytes) if att.total_bytes else ""
            status = ""
            if att.download_status == "downloading":
                status = " downloading..."
            elif att.download_status == "failed":
                status = " download failed"
            size_part = f", {size}" if size else ""
            lines.append(f"            [{att.filename} ({kind}{size_part}{status})]")

    return lines


def _apply_highlights(area: TextArea, line_directions: dict[int, str]) -> None:
    """Inject per-line highlights into the TextArea based on message direction."""
    highlights: dict[int, list[tuple[int, int | None, str]]] = defaultdict(list)

    text = area.text
    lines = text.split("\n")

    for i, line in enumerate(lines):
        if line.startswith("\u2500\u2500\u2500"):
            token = "scheduled" if "SCHEDULED" in line else "separator"
            highlights[i].append((0, None, token))
            continue

        if line.lstrip().startswith("[") and line.lstrip().endswith("]"):
            if "  [" in line:
                highlights[i].append((0, None, "attachment"))
            else:
                highlights[i].append((0, None, "scheduled"))
            continue

        if not line.strip():
            continue

        direction = line_directions.get(i)
        if direction is None:
            continue

        # Color the whole line based on direction
        if direction == "out":
            highlights[i].append((0, None, "sent"))
        else:
            highlights[i].append((0, None, "received"))

    # These are private Textual internals — wrap so chat still works
    # (just unstyled) if Textual changes them.
    try:
        area._highlights = highlights
        area._line_cache.clear()
    except AttributeError:
        pass
