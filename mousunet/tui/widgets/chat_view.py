"""Right panel — chat history with DedSec styling, date separators, and bubble layout."""

from __future__ import annotations

import re
from datetime import datetime, date

from textual.app import ComposeResult
from textual.containers import VerticalScroll
from textual.widget import Widget
from textual.widgets import Static

from ...db.models import Message


DEVICE_MAP = {
    "imessage": "MAC",
    "sms": "PIXEL",
}

# Match URLs in message text
_URL_RE = re.compile(r'(https?://\S+)', re.IGNORECASE)
# Match phone numbers
_PHONE_RE = re.compile(r'(\+?1?\s*\(?\d{3}\)?[\s\-]?\d{3}[\s\-]?\d{4})')


def _date_label(d: date) -> str:
    """Human-friendly date label."""
    today = date.today()
    delta = (today - d).days
    weekday = d.strftime("%A")
    if delta == 0:
        return f"TODAY — {weekday.upper()}, {d.strftime('%b %d').upper()}"
    elif delta == 1:
        return f"YESTERDAY — {weekday.upper()}, {d.strftime('%b %d').upper()}"
    elif delta < 7:
        return f"{weekday.upper()}, {d.strftime('%b %d').upper()}"
    else:
        return f"{d.strftime('%A, %b %d, %Y').upper()}"


def _highlight_body(text: str) -> str:
    """Add Rich markup for URLs and phone numbers in message body."""
    # Escape Rich markup chars in text first
    safe = text.replace("[", "\\[")
    # Highlight URLs
    safe = _URL_RE.sub(r'[underline #68a8e4]\1[/]', safe)
    return safe


def _format_time(dt: datetime | None) -> str:
    """Format timestamp as compact time string."""
    if not dt:
        return ""
    return dt.strftime("%-I:%M%p").lower()


class DateSeparator(Static):
    """Horizontal rule with date label."""

    DEFAULT_CSS = """
    DateSeparator {
        height: 1;
        color: #555555;
        text-align: center;
        margin: 1 0 0 0;
    }
    """

    def __init__(self, label: str) -> None:
        super().__init__(f"─── {label} ───")


class UnreadMarker(Static):
    """Visual marker for new/unread messages."""

    DEFAULT_CSS = """
    UnreadMarker {
        height: 1;
        color: #ff2d6f;
        text-align: center;
        margin: 1 0;
    }
    """

    def __init__(self) -> None:
        super().__init__("── NEW MESSAGES ──")


class ChatView(VerticalScroll):
    """Scrollable chat history with dynamic border title."""

    BINDINGS = [
        ("home", "scroll_top", "Top"),
        ("end", "scroll_bottom", "Bottom"),
    ]

    def __init__(self) -> None:
        super().__init__()
        self._contact_name: str = ""
        self._contact_phone: str = ""
        self._platform: str = ""
        self._messages_data: list[tuple[str, str, str]] = []
        self._message_count: int = 0
        self.border_title = "╸ SELECT A NODE ╺"

    @property
    def message_count(self) -> int:
        return self._message_count

    def set_messages(
        self, messages: list[Message], contact_name: str, platform: str = "",
        phone: str = "",
    ) -> None:
        self._contact_name = contact_name
        self._contact_phone = phone
        self._platform = platform
        self._message_count = len(messages)
        self._update_title()
        self._messages_data = []
        self.remove_children()

        if not messages:
            self.mount(Static("[#555555]no transmissions yet[/]", classes="empty-state"))
            return

        last_date = None
        for msg in messages:
            sender = "you" if msg.direction == "out" else contact_name
            self._messages_data.append((msg.direction, sender, msg.body))

            # Insert date separator when day changes
            if msg.sent_at:
                msg_date = msg.sent_at.date()
                if msg_date != last_date:
                    self.mount(DateSeparator(_date_label(msg_date)))
                    last_date = msg_date

            self.mount(MessageBubble(msg, contact_name))

        self.call_after_refresh(self.scroll_end, animate=False)

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
        sender = "you" if msg.direction == "out" else self._contact_name
        self._messages_data.append((msg.direction, sender, msg.body))
        self._message_count += 1
        self._update_title()

        # Add date separator if needed
        if msg.sent_at:
            msg_date = msg.sent_at.date()
            last_date = None
            for child in reversed(list(self.children)):
                if isinstance(child, DateSeparator):
                    # Parse date from existing separator — just insert new one if different day
                    break
                if isinstance(child, MessageBubble) and child._sent_at:
                    last_date = child._sent_at.date()
                    break
            if last_date and msg_date != last_date:
                self.mount(DateSeparator(_date_label(msg_date)))

        self.mount(MessageBubble(msg, self._contact_name))
        self.call_after_refresh(self.scroll_end, animate=False)

    def action_scroll_top(self) -> None:
        self.scroll_home(animate=False)

    def action_scroll_bottom(self) -> None:
        self.scroll_end(animate=False)


class CopyButton(Static):
    """Inline copy button that appears below a clicked message."""

    def __init__(self, text: str) -> None:
        super().__init__("  \u25b8 copy to clipboard")
        self._text = text

    def on_click(self, event) -> None:
        event.stop()
        from ..clipboard import copy_osc52
        copy_osc52(self._text)
        self.app.notify("copied", timeout=2)
        self.remove()


class MessageBubble(Static):
    """A message bubble with direction-based alignment and styling."""

    def __init__(self, msg: Message, contact_name: str) -> None:
        self._body = msg.body
        self._sent_at = msg.sent_at
        self._direction = msg.direction

        ts = _format_time(msg.sent_at)
        body_rich = _highlight_body(msg.body)

        if msg.direction == "out":
            # Outbound: green sender, delivery indicator
            delivery = "\u2713" if msg.delivered else "\u2717"
            delivery_color = "#50fa7b" if msg.delivered else "#f75341"
            markup = (
                f"[#1a5c7a]{ts}[/] "
                f"[#50fa7b bold]you:[/] "
                f"{body_rich}"
                f" [{delivery_color}]{delivery}[/]"
            )
        else:
            # Inbound: cyan sender
            markup = (
                f"[#1a5c7a]{ts}[/] "
                f"[#00d4ff bold]{contact_name}:[/] "
                f"{body_rich}"
            )

        super().__init__(markup)

        if msg.direction == "out":
            self.add_class("msg-out")
        else:
            self.add_class("msg-in")

    def on_click(self, event) -> None:
        event.stop()
        for btn in self.screen.query(CopyButton):
            btn.remove()
        self.parent.mount(CopyButton(self._body), after=self)
