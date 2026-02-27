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
    """Human-friendly date label with day of week and date."""
    today = date.today()
    delta = (today - d).days
    weekday = d.strftime("%A")  # e.g. "Tuesday"
    if delta == 0:
        return f"Today — {weekday}, {d.strftime('%b %d')}"
    elif delta == 1:
        return f"Yesterday — {weekday}, {d.strftime('%b %d')}"
    else:
        return f"{weekday}, {d.strftime('%b %d')}"


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
        self._contact_phone: str = ""
        self._platform: str = ""
        self._messages_data: list[tuple[str, str, str]] = []
        self.border_title = "╸ SELECT A NODE ╺"

    def set_messages(
        self, messages: list[Message], contact_name: str, platform: str = "",
        phone: str = "",
    ) -> None:
        self._contact_name = contact_name
        self._contact_phone = phone
        self._platform = platform
        self._update_title()
        self._messages_data: list[tuple[str, str, str]] = []
        self.remove_children()
        if not messages:
            self.mount(Static("no messages yet", classes="empty-state"))
            return

        for msg in messages:
            sender = "you" if msg.direction == "out" else contact_name
            self._messages_data.append((msg.direction, sender, msg.body))
            self.mount(MessageRow(msg, contact_name))

        self.call_after_refresh(self.scroll_end, animate=False)

    def _update_title(self) -> None:
        if not self._contact_name:
            self.border_title = "╸ SELECT A NODE ╺"
            return
        platform_upper = self._platform.upper() if self._platform else "???"
        device = DEVICE_MAP.get(self._platform, "RELAY")
        phone = self._contact_phone or ""
        self.border_title = (
            f"╸ {self._contact_name.upper()} {phone} // {platform_upper} // {device} ╺"
        )

    def get_messages_text(self) -> list[tuple[str, str, str]]:
        """Return list of (direction, sender, body) for all messages."""
        return list(self._messages_data)

    def get_last_message_body(self) -> str:
        """Return body of the last message, or empty string."""
        if self._messages_data:
            return self._messages_data[-1][2]
        return ""

    def append_message(self, msg: Message) -> None:
        for child in self.children:
            if isinstance(child, Static) and "empty-state" in child.classes:
                child.remove()
                break
        self.mount(MessageRow(msg, self._contact_name))
        self.call_after_refresh(self.scroll_end, animate=False)


class CopyButton(Static):
    """Inline copy button that appears below a clicked message."""

    def __init__(self, text: str) -> None:
        super().__init__("  \u25b8 copy to clipboard")
        self._text = text

    def on_click(self, event) -> None:
        event.stop()
        from ..clipboard import copy_osc52
        copy_osc52(self._text)
        self.app.notify("copied to clipboard", timeout=2)
        self.remove()


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
        self._body = msg.body

        ts = ""
        if msg.sent_at:
            ts = msg.sent_at.strftime("%-I:%M %p %a, %b %-d %Y").upper()  # e.g. "6:30 AM TUE, FEB 27 2026"

        if msg.direction == "out":
            sender_color = "#50fa7b"
            sender = "you"
        else:
            sender_color = "#00d4ff"
            sender = contact_name

        markup = (
            f"[#1a5c7a]{ts}[/] "
            f"[{sender_color} bold]{sender}:[/]"
            f" {msg.body}"
        )
        super().__init__(markup)

    def on_click(self, event) -> None:
        event.stop()
        # Remove any existing copy buttons in the chat
        for btn in self.screen.query(CopyButton):
            btn.remove()
        # Mount copy button right after this message
        self.parent.mount(CopyButton(self._body), after=self)
