"""Right panel — chat in a read-only TextArea for native CUA select/copy."""

from __future__ import annotations

import re
from datetime import datetime, date

from textual.app import ComposeResult
from textual.containers import Vertical
from textual.widgets import TextArea

from ...db.models import Message


DEVICE_MAP = {
    "imessage": "MAC",
    "sms": "PIXEL",
}


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
    return dt.strftime("%-I:%M%p").lower().rjust(7)


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
        self.border_title = "╸ SELECT A NODE ╺"

    @property
    def message_count(self) -> int:
        return self._message_count

    def compose(self) -> ComposeResult:
        yield TextArea("", read_only=True, id="chat-area", show_line_numbers=False)

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

        if not messages:
            self._set_text("  no transmissions yet")
            return

        lines: list[str] = []
        last_date = None

        for msg in messages:
            sender = "you" if msg.direction == "out" else contact_name
            self._messages_data.append((msg.direction, sender, msg.body))

            if msg.sent_at:
                msg_date = msg.sent_at.date()
                if msg_date != last_date:
                    if lines:
                        lines.append("")
                    sep = _date_label(msg_date)
                    lines.append(f"─── {sep} ───")
                    lines.append("")
                    last_date = msg_date

            lines.append(_render_message(msg, contact_name))

        self._set_text("\n".join(lines))

    def _set_text(self, text: str) -> None:
        try:
            area = self.query_one("#chat-area", TextArea)
            area.load_text(text)
            # Scroll to bottom
            self.call_after_refresh(self._scroll_to_end)
        except Exception:
            pass

    def _scroll_to_end(self) -> None:
        try:
            area = self.query_one("#chat-area", TextArea)
            area.scroll_end(animate=False)
            # Move cursor to end too
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
            if msg.sent_at:
                msg_date = msg.sent_at.date()
                last_date = None
                for m in reversed(self._raw_messages[:-1]):
                    if m.sent_at:
                        last_date = m.sent_at.date()
                        break
                if last_date and msg_date != last_date:
                    sep = _date_label(msg_date)
                    new_line = f"\n\n─── {sep} ───\n"

            line = _render_message(msg, self._contact_name)
            if current and not current.endswith("\n"):
                new_line = "\n" + new_line
            area.load_text(current + new_line + line)
            self.call_after_refresh(self._scroll_to_end)
        except Exception:
            pass
