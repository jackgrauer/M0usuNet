"""Right panel — chat in a read-only TextArea for native CUA select/copy."""

from __future__ import annotations

import re
from collections import defaultdict
from datetime import datetime, date, timezone

from rich.style import Style
from textual.app import ComposeResult
from textual.containers import Vertical
from textual.widgets import TextArea
from textual.widgets.text_area import TextAreaTheme

from ...db.models import Message


DEVICE_MAP = {
    "imessage": "MAC",
    "sms": "PIXEL",
}

# Custom theme for chat coloring
_CHAT_THEME = TextAreaTheme(
    "m0usunet",
    base_style=Style(color="#e0e0e0", bgcolor="#0a0a0a"),
    cursor_style=Style(color="#e0e0e0", bgcolor="#333333"),
    cursor_line_style=Style(bgcolor="#111111"),
    selection_style=Style(bgcolor="#1a3a5a"),
    syntax_styles={
        "sent": Style(color="#00d4ff"),          # cyan — your messages
        "sent.name": Style(color="#00d4ff", bold=True),
        "received": Style(color="#50fa7b"),       # green — their messages
        "received.name": Style(color="#50fa7b", bold=True),
        "separator": Style(color="#444444"),      # dim — date separators
        "timestamp": Style(color="#666666"),      # dim — time prefix
        "delivery.ok": Style(color="#50fa7b"),    # green ✓
        "delivery.fail": Style(color="#f75341"),  # red ✗
    },
)

# Regex for a message line: "  1:23pm  name: body [✓✗]"
_MSG_RE = re.compile(r"^(\s*\d+:\d+[ap]m)\s{2}(\S+):\s(.*)$")


def _to_local(dt: datetime) -> datetime:
    """Convert a datetime to local time. Handles both aware and naive datetimes."""
    if dt.tzinfo is not None:
        return dt.astimezone()
    # Naive datetime — assume it's already local
    return dt


def _date_label(d: date) -> str:
    today = date.today()
    delta = (today - d).days
    weekday = d.strftime("%A").upper()
    datestamp = d.strftime("%b %d").upper()
    if delta == 0:
        return f"TODAY — {weekday}, {datestamp}"
    elif delta == 1:
        return f"YESTERDAY — {weekday}, {datestamp}"
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
    """Get the local date for a datetime (converting from UTC if needed)."""
    if not dt:
        return None
    return _to_local(dt).date()


def _render_message(msg: Message, contact_name: str) -> str:
    """Render a single message as a plain-text line."""
    ts = _format_time(msg.sent_at)
    if msg.direction == "out":
        sender = "you"
        delivery = " ✓" if msg.delivered else " ✗"
    else:
        sender = contact_name
        delivery = ""
    return f"{ts}  {sender}: {msg.body}{delivery}"


def _apply_highlights(area: TextArea, line_directions: dict[int, str]) -> None:
    """Inject per-line highlights into the TextArea based on message direction."""
    highlights: dict[int, list[tuple[int, int | None, str]]] = defaultdict(list)

    text = area.text
    lines = text.split("\n")

    for i, line in enumerate(lines):
        # Date separator lines
        if line.startswith("───"):
            highlights[i].append((0, len(line), "separator"))
            continue

        # Empty lines — skip
        if not line.strip():
            continue

        direction = line_directions.get(i)
        if direction is None:
            continue

        m = _MSG_RE.match(line)
        if not m:
            # Fallback: color the entire line
            token = "sent" if direction == "out" else "received"
            highlights[i].append((0, len(line), token))
            continue

        ts_text, sender_text, body_text = m.group(1), m.group(2), m.group(3)
        ts_end = m.start(1) + len(ts_text)
        sender_start = m.start(2)
        sender_end = sender_start + len(sender_text) + 1  # include ":"
        body_start = m.start(3)
        body_end = body_start + len(body_text)

        if direction == "out":
            # Timestamp dim
            highlights[i].append((0, ts_end, "timestamp"))
            # Sender bold cyan
            highlights[i].append((sender_start, sender_end, "sent.name"))
            # Body cyan
            highlights[i].append((sender_end + 1, None, "sent"))
            # Delivery indicator
            if body_text.endswith(" ✓"):
                highlights[i].append((body_end - 1, body_end, "delivery.ok"))
            elif body_text.endswith(" ✗"):
                highlights[i].append((body_end - 1, body_end, "delivery.fail"))
        else:
            # Timestamp dim
            highlights[i].append((0, ts_end, "timestamp"))
            # Sender bold green
            highlights[i].append((sender_start, sender_end, "received.name"))
            # Body green
            highlights[i].append((sender_end + 1, None, "received"))

    area._highlights = highlights
    area._line_cache.clear()


class ChatView(Vertical):
    """Chat history in a selectable read-only TextArea."""

    def __init__(self) -> None:
        super().__init__()
        self._contact_name: str = ""
        self._contact_phone: str = ""
        self._platform: str = ""
        self._messages_data: list[tuple[str, str, str]] = []
        self._message_count: int = 0
        self._raw_messages: list[Message] = []
        self._line_directions: dict[int, str] = {}
        self.border_title = "╸ SELECT A NODE ╺"

    @property
    def message_count(self) -> int:
        return self._message_count

    def compose(self) -> ComposeResult:
        area = TextArea("", read_only=True, id="chat-area", show_line_numbers=False)
        area.register_theme(_CHAT_THEME)
        area.theme = "m0usunet"
        yield area

    def set_messages(
        self, messages: list[Message], contact_name: str, platform: str = "",
        phone: str = "",
    ) -> None:
        self._contact_name = contact_name
        self._contact_phone = phone
        self._platform = platform
        self._raw_messages = list(messages)
        self._message_count = len(messages)
        self._update_title()
        self._messages_data = []
        self._line_directions = {}

        if not messages:
            self._set_text("  no transmissions yet", {})
            return

        lines: list[str] = []
        line_dirs: dict[int, str] = {}
        last_date = None

        for msg in messages:
            sender = "you" if msg.direction == "out" else contact_name
            self._messages_data.append((msg.direction, sender, msg.body))

            msg_date = _local_date(msg.sent_at)
            if msg_date and msg_date != last_date:
                if lines:
                    lines.append("")
                sep = _date_label(msg_date)
                lines.append(f"─── {sep} ───")
                lines.append("")
                last_date = msg_date

            line_dirs[len(lines)] = msg.direction
            lines.append(_render_message(msg, contact_name))

        self._line_directions = line_dirs
        self._set_text("\n".join(lines), line_dirs)

    def _set_text(self, text: str, line_dirs: dict[int, str]) -> None:
        try:
            area = self.query_one("#chat-area", TextArea)
            area.load_text(text)
            _apply_highlights(area, line_dirs)
            area.refresh()
            self.call_after_refresh(self._scroll_to_end)
        except Exception:
            pass

    def _scroll_to_end(self) -> None:
        try:
            area = self.query_one("#chat-area", TextArea)
            area.scroll_end(animate=False)
            area.move_cursor((area.document.line_count - 1, 0))
        except Exception:
            pass

    def _update_title(self) -> None:
        if not self._contact_name:
            self.border_title = "╸ SELECT A NODE ╺"
            return
        platform_upper = self._platform.upper() if self._platform else "???"
        device = DEVICE_MAP.get(self._platform, "RELAY")
        phone = self._contact_phone or ""
        count = f"{self._message_count} msgs" if self._message_count else "0 msgs"
        self.border_title = (
            f"╸ {self._contact_name.upper()} {phone} // {platform_upper} via {device} // {count} ╺"
        )

    def get_messages_text(self) -> list[tuple[str, str, str]]:
        return list(self._messages_data)

    def get_last_message_body(self) -> str:
        if self._messages_data:
            return self._messages_data[-1][2]
        return ""

    def append_message(self, msg: Message) -> None:
        self._raw_messages.append(msg)
        sender = "you" if msg.direction == "out" else self._contact_name
        self._messages_data.append((msg.direction, sender, msg.body))
        self._message_count += 1
        self._update_title()

        try:
            area = self.query_one("#chat-area", TextArea)
            current = area.text

            # Check if we need a date separator
            new_line = ""
            msg_date = _local_date(msg.sent_at)
            if msg_date:
                last_date = None
                for m in reversed(self._raw_messages[:-1]):
                    ld = _local_date(m.sent_at)
                    if ld:
                        last_date = ld
                        break
                if last_date and msg_date != last_date:
                    sep = _date_label(msg_date)
                    new_line = f"\n\n─── {sep} ───\n"

            line = _render_message(msg, self._contact_name)
            if current and not current.endswith("\n"):
                new_line = "\n" + new_line

            full_text = current + new_line + line
            # Rebuild line direction map
            new_line_idx = full_text.count("\n")
            self._line_directions[new_line_idx] = msg.direction

            area.load_text(full_text)
            _apply_highlights(area, self._line_directions)
            area.refresh()
            self.call_after_refresh(self._scroll_to_end)
        except Exception:
            pass
