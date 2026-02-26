"""Right panel — chat history with DedSec styling and date separators."""

from __future__ import annotations

from datetime import datetime, date

from textual.app import ComposeResult
from textual.containers import VerticalScroll
from textual.widgets import Static

from ...db.models import Message


DEVICE_MAP = {
    "imessage": "MAC",
    "sms": "PIXEL",
}


def _date_label(d: date) -> str:
    """Human-friendly date label."""
    today = date.today()
    delta = (today - d).days
    if delta == 0:
        return "Today"
    elif delta == 1:
        return "Yesterday"
    elif delta < 7:
        return d.strftime("%A")  # e.g. "Tuesday"
    else:
        return d.strftime("%b %d")  # e.g. "Feb 24"


class DateSeparator(Static):
    """Horizontal rule with date label."""

    DEFAULT_CSS = """
    DateSeparator {
        height: 1;
        color: #555555;
        text-align: center;
        margin: 1 0;
    }
    """

    def __init__(self, label: str) -> None:
        super().__init__(f"── {label} ──")


class ChatView(VerticalScroll):
    """Scrollable chat history with dynamic border title."""

    def __init__(self) -> None:
        super().__init__()
        self._contact_name: str = ""
        self._platform: str = ""
        self.border_title = "╸ SELECT A NODE ╺"

    def set_messages(
        self, messages: list[Message], contact_name: str, platform: str = ""
    ) -> None:
        self._contact_name = contact_name
        self._platform = platform
        self._update_title()
        self.remove_children()
        if not messages:
            self.mount(Static("no messages yet", classes="empty-state"))
            return

        last_date: date | None = None
        for msg in messages:
            msg_date = msg.sent_at.date() if msg.sent_at else None
            if msg_date and msg_date != last_date:
                self.mount(DateSeparator(_date_label(msg_date)))
                last_date = msg_date
            self.mount(MessageRow(msg, contact_name))

        self.call_after_refresh(self.scroll_end, animate=False)

    def _update_title(self) -> None:
        if not self._contact_name:
            self.border_title = "╸ SELECT A NODE ╺"
            return
        platform_upper = self._platform.upper() if self._platform else "???"
        device = DEVICE_MAP.get(self._platform, "RELAY")
        self.border_title = (
            f"╸ {self._contact_name.upper()} // {platform_upper} // {device} ╺"
        )

    def append_message(self, msg: Message) -> None:
        for child in self.children:
            if isinstance(child, Static) and "empty-state" in child.classes:
                child.remove()
                break
        self.mount(MessageRow(msg, self._contact_name))
        self.call_after_refresh(self.scroll_end, animate=False)


class MessageRow(Static):
    """A single message with direction-tinted sender labels."""

    DEFAULT_CSS = """
    MessageRow {
        height: auto;
        padding: 0 0;
        margin: 0 0;
    }
    """

    def __init__(self, msg: Message, contact_name: str) -> None:
        ts = ""
        if msg.sent_at:
            ts = msg.sent_at.strftime("%H:%M")

        if msg.direction == "out":
            sender_color = "#50fa7b"
            sender = "you"
        else:
            sender_color = "#00d4ff"
            sender = contact_name

        markup = (
            f"[#1a5c7a]{ts}[/] "
            f"[{sender_color} bold]{sender}[/]"
            f"  {msg.body}"
        )
        super().__init__(markup)
