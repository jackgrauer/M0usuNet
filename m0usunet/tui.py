"""m0usunet TUI — unified messaging interface."""

from __future__ import annotations

import base64
import datetime as _dt
import logging
import platform
import re
import subprocess
import threading
from collections import defaultdict
from datetime import date, datetime, timezone
from pathlib import Path

from rich.style import Style
from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.message import Message as TMessage
from textual.reactive import reactive
from textual.screen import ModalScreen, Screen
from textual.widget import Widget
from textual.widgets import DirectoryTree, Input, OptionList, Static, TextArea
from textual.widgets.option_list import Option
from textual.widgets.text_area import TextAreaTheme

from .db import (
    Attachment, Contact, ConversationSummary, Message, ScheduledMessage,
    add_message, add_scheduled_message, cancel_scheduled,
    conversation_list, delete_messages_for_contact,
    ensure_schema, get_attachments_for_messages, get_connection,
    get_contact, get_messages, get_scheduled_for_contact,
    mark_viewed, search_contacts, toggle_pin, upsert_contact,
)
from .exceptions import RelayError
from .relay import send_message

log = logging.getLogger(__name__)

CSS_PATH = Path(__file__).parent / "sorbet.tcss"


# ── Schedule time parsing ─────────────────────────────────

_TIME_RE = re.compile(r"^(\d{1,2}):?(\d{2})?\s*(am|pm)\b", re.IGNORECASE)
_DATE_RE = re.compile(r"^(\d{1,2})/(\d{1,2})\b")


def _parse_schedule_time(text: str) -> tuple[str, str] | None:
    """Parse '/at 9:30am [tomorrow|M/D] message' into (ISO datetime, message).

    Returns (scheduled_at_utc_iso, message_body) or None on parse failure.
    """
    from zoneinfo import ZoneInfo
    from .constants import USER_TZ

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


# ── Clipboard ─────────────────────────────────────────────

def copy_osc52(text: str) -> None:
    """Copy text to system clipboard.

    Uses pbcopy on macOS (native, always works).
    Falls back to OSC 52 escape sequence for Linux/SSH.
    """
    if platform.system() == "Darwin":
        subprocess.run(["pbcopy"], input=text.encode(), check=True)
    else:
        encoded = base64.b64encode(text.encode()).decode()
        osc = f"\033]52;c;{encoded}\a"
        with open("/dev/tty", "w") as tty:
            tty.write(osc)
            tty.flush()


# ── Header bar ────────────────────────────────────────────

MESH_DEVICES = {
    "PIXEL": "192.168.0.22",
    "MINI": "192.168.0.15",
    "MAC": "192.168.0.13",
}


class TabButton(Static):
    """Clickable tab button in the header."""

    def on_click(self, event) -> None:
        event.stop()
        if self.id == "tab-messages":
            self.app.action_show_messages()
        elif self.id == "tab-compose":
            self.app.action_new_message()


class HeaderBar(Widget):
    """Top bar with app title, tabs, mesh status, and ingest health."""

    def __init__(self) -> None:
        super().__init__()
        self._device_status: dict[str, bool] = {name: False for name in MESH_DEVICES}
        self._pinging = False

    def compose(self) -> ComposeResult:
        with Horizontal():
            yield Static(" m0usunet ", id="app-title")
            yield Static(" ", id="title-spacer")
            yield TabButton(" MESSAGES ", id="tab-messages", classes="tab-btn --active")
            yield TabButton(" COMPOSE ", id="tab-compose", classes="tab-btn")
            yield Static("", id="spacer", classes="header-spacer")
            yield Static("", id="unread-count")
            yield Static("", id="ingest-status")
            yield Static("", id="mesh-status")
            yield Static("", id="clock")

    def on_mount(self) -> None:
        self.set_interval(1.0, self._update_clock)
        self.set_interval(60.0, self._check_devices_bg)
        self.set_interval(30.0, self._check_ingest)
        self._update_clock()
        self.set_timer(2.0, self._check_devices_bg)
        self.set_timer(3.0, self._check_ingest)

    def _update_clock(self) -> None:
        now = datetime.now().strftime("%H:%M:%S")
        try:
            self.query_one("#clock", Static).update(f"[#555555]{now}[/]")
        except Exception:
            pass

    def _check_ingest(self) -> None:
        threading.Thread(target=self._do_check_ingest, daemon=True).start()

    def _do_check_ingest(self) -> None:
        try:
            thread = getattr(self.app, "_ingest_thread", None)
            active = thread is not None and thread.is_alive()
            self.app.call_from_thread(self._render_ingest, active)
        except Exception:
            self.app.call_from_thread(self._render_ingest, False)

    def _render_ingest(self, active: bool) -> None:
        try:
            widget = self.query_one("#ingest-status", Static)
            if active:
                widget.update("[#50fa7b]\u25c9 INGEST[/]")
            else:
                widget.update("[#f75341]\u25c9 INGEST:OFF[/]")
        except Exception:
            pass

    def _check_devices_bg(self) -> None:
        if self._pinging:
            return
        self._pinging = True
        threading.Thread(target=self._ping_devices, daemon=True).start()

    def _ping_devices(self) -> None:
        try:
            for label, host in MESH_DEVICES.items():
                try:
                    result = subprocess.run(
                        ["ping", "-c", "1", "-W", "1", host],
                        capture_output=True,
                        timeout=3,
                    )
                    self._device_status[label] = result.returncode == 0
                except Exception:
                    self._device_status[label] = False
            self.app.call_from_thread(self._render_status)
        finally:
            self._pinging = False

    def update_unread(self, count: int) -> None:
        try:
            widget = self.query_one("#unread-count", Static)
            if count > 0:
                widget.update(f"[bold #ff2d6f]{count} UNREAD[/]")
            else:
                widget.update("")
        except Exception:
            pass

    def _render_status(self) -> None:
        parts = []
        for label, alive in self._device_status.items():
            if alive:
                parts.append(f"[#50fa7b]\u25c9 {label}[/]")
            else:
                parts.append(f"[#f75341]\u25cb {label}[/]")
        text = " ".join(parts)
        try:
            self.query_one("#mesh-status", Static).update(text)
        except Exception:
            pass


# ── Compose box ───────────────────────────────────────────

class ReplyButton(Static):
    """Clickable button that toggles the compose input."""

    def on_click(self, event) -> None:
        event.stop()
        for ancestor in self.ancestors:
            if isinstance(ancestor, ComposeBox):
                ancestor.show_input()
                break


class AttachButton(Static):
    """Clickable button that opens the file picker."""

    def on_click(self, event) -> None:
        event.stop()
        self.app.action_attach_file()


class ComposeBox(Widget):
    """Reply input that fires a Submitted message."""

    WORD_LIMIT = 18

    class Submitted(TMessage):
        """User pressed Enter with a message."""

        def __init__(self, body: str, attachment_path: str = "") -> None:
            super().__init__()
            self.body = body
            self.attachment_path = attachment_path

    def __init__(self) -> None:
        super().__init__()
        self._placeholder = "select a node..."
        self._status = Static("", id="relay-status")
        self._input_visible = False
        self._attachment_path: str = ""

    def compose(self) -> ComposeResult:
        with Horizontal():
            yield ReplyButton(" REPLY ", id="reply-btn")
            yield AttachButton(" ATTACH ", id="attach-btn")
            yield Static(" \u25b8 ", id="compose-prompt")
            yield Input(placeholder=self._placeholder, id="compose-input")
        yield Static("", id="attach-indicator")
        yield self._status

    def on_mount(self) -> None:
        self._hide_input()
        try:
            self.query_one("#attach-indicator", Static).display = False
        except Exception:
            pass

    def _hide_input(self) -> None:
        self._input_visible = False
        try:
            self.query_one("#compose-prompt", Static).display = False
            self.query_one("#compose-input", Input).display = False
        except Exception:
            pass

    def show_input(self) -> None:
        self._input_visible = True
        try:
            self.query_one("#compose-prompt", Static).display = True
            inp = self.query_one("#compose-input", Input)
            inp.display = True
            inp.focus()
        except Exception:
            pass

    def set_attachment(self, path: str) -> None:
        self._attachment_path = path
        filename = path.rsplit("/", 1)[-1]
        try:
            indicator = self.query_one("#attach-indicator", Static)
            indicator.update(f"  [#ff8c00]+ {filename}[/]")
            indicator.display = True
        except Exception:
            pass

    def clear_attachment(self) -> None:
        self._attachment_path = ""
        try:
            self.query_one("#attach-indicator", Static).display = False
            self.query_one("#attach-indicator", Static).update("")
            self.query_one("#attach-input", Input).value = ""
        except Exception:
            pass

    def set_contact_name(self, name: str) -> None:
        self._placeholder = f"message {name}..."
        try:
            inp = self.query_one("#compose-input", Input)
            inp.placeholder = self._placeholder
        except Exception:
            pass

    def show_status(self, text: str, error: bool = False) -> None:
        self._status.update(text)
        self._status.set_class(error, "error")
        self._status.set_class(not error, "success")

    def clear_status(self) -> None:
        self._status.update("")

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id == "compose-input":
            words = len(event.value.split()) if event.value.strip() else 0
            event.input.set_class(words > self.WORD_LIMIT, "--over-limit")

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id != "compose-input":
            return
        body = event.value.strip()
        if not body and not self._attachment_path:
            return
        event.input.value = ""
        event.input.remove_class("--over-limit")
        att_path = self._attachment_path
        self.clear_attachment()
        self._hide_input()
        self.post_message(self.Submitted(body or "[attachment]", att_path))


# ── Conversation list ─────────────────────────────────────

PLATFORM_STYLE = {
    "imessage": ("imsg", "#68a8e4"),
    "sms":      ("sms",  "#50fa7b"),
    "bumble":   ("bmbl", "#ff2d6f"),
    "hinge":    ("hnge", "#ff2d6f"),
    "tinder":   ("tndr", "#ff8c00"),
}


class SearchBar(Widget):
    """Inline search/filter bar for conversations."""

    DEFAULT_CSS = """
    SearchBar {
        height: 1;
        display: none;
    }
    SearchBar.--visible {
        display: block;
    }
    SearchBar Input {
        background: #111111;
        color: #e0e0e0;
        border: none;
        width: 100%;
        padding: 0;
        height: 1;
    }
    SearchBar Input:focus {
        border: none;
    }
    """

    def compose(self) -> ComposeResult:
        yield Input(placeholder="/ filter...", id="conv-search")


class NewMessageButton(Static):
    """Clickable '+ NEW MESSAGE' button at top of conversation list."""

    DEFAULT_CSS = """
    NewMessageButton {
        height: 1;
        padding: 0 1;
        color: #00d4ff;
        text-style: bold;
        background: #0d0d0d;
        text-align: center;
    }
    NewMessageButton:hover {
        background: #111a22;
    }
    """

    def __init__(self) -> None:
        super().__init__("[ + NEW ]")

    def on_click(self) -> None:
        for ancestor in self.ancestors:
            if isinstance(ancestor, ConversationList):
                ancestor.post_message(ConversationList.NewMessageRequested())
                break


class ConversationItem(Widget):
    """A single conversation row with platform badge and preview."""

    DEFAULT_CSS = """
    ConversationItem {
        height: 2;
        padding: 0 1;
        border-left: thick #0a0a0a;
    }
    ConversationItem.--highlight {
        background: #152030;
        border-left: thick #00d4ff;
    }
    """

    def __init__(self, convo: ConversationSummary, index: int) -> None:
        super().__init__()
        self._convo = convo
        self.index = index

    @staticmethod
    def _render_lines(c: ConversationSummary) -> tuple[str, str]:
        tag_label, tag_color = PLATFORM_STYLE.get(c.platform, (c.platform[:4], "#555555"))

        time_str = ""
        if c.last_time:
            today = _dt.date.today()
            local_time = c.last_time.astimezone() if c.last_time.tzinfo else c.last_time.replace(tzinfo=timezone.utc).astimezone()
            if local_time.date() == today:
                time_str = local_time.strftime("%-I:%M%p").lower()
            else:
                time_str = local_time.strftime("%b %-d")

        unread = ""
        if c.unread_count > 0:
            unread = f" [bold #00d4ff]({c.unread_count})[/]"

        pin = "[#ff8c00]\u2605[/] " if c.pinned else ""

        name_line = (
            f"{pin}[{tag_color}]\\[{tag_label}][/] "
            f"{c.display_name}{unread}"
            f"  [#555555]{time_str}[/]"
        )

        prefix = "[#555555]you: [/]" if c.direction == "out" else ""
        preview = c.last_message[:30] if c.last_message else ""
        preview_line = f"     [#555555]{prefix}{preview}[/]"
        return name_line, preview_line

    def compose(self) -> ComposeResult:
        name_line, preview_line = self._render_lines(self._convo)
        yield Static(name_line, classes="conv-name")
        yield Static(preview_line, classes="conv-preview")

    def update_convo(self, convo: ConversationSummary, index: int) -> None:
        self._convo = convo
        self.index = index
        name_line, preview_line = self._render_lines(convo)
        children = list(self.children)
        if len(children) >= 2:
            children[0].update(name_line)
            children[1].update(preview_line)

    def on_click(self, event) -> None:
        for ancestor in self.ancestors:
            if isinstance(ancestor, ConversationList):
                ancestor.selected_index = self.index
                if event.button == 3:
                    ancestor._request_context_menu(self.index)
                break


class ConversationList(VerticalScroll):
    """Scrollable list of conversations with search and vim nav."""

    BORDER_TITLE = "\u25c8 NODES \u25c8"

    class Selected(TMessage):
        """Fired when a conversation is selected."""

        def __init__(self, contact_id: int, display_name: str, platform: str = "sms", phone: str = "", pinned: bool = False) -> None:
            super().__init__()
            self.contact_id = contact_id
            self.display_name = display_name
            self.platform = platform
            self.phone = phone
            self.pinned = pinned

    class NewMessageRequested(TMessage):
        """Fired when the user wants to start a new conversation."""

    class ContextMenuRequested(TMessage):
        """Fired on right-click or 'm' key for context menu."""

        def __init__(self, contact_id: int, display_name: str, phone: str, message_count: int, pinned: bool = False) -> None:
            super().__init__()
            self.contact_id = contact_id
            self.display_name = display_name
            self.phone = phone
            self.message_count = message_count
            self.pinned = pinned

    selected_index: reactive[int] = reactive(0)

    def __init__(self) -> None:
        super().__init__()
        self._conversations: list[ConversationSummary] = []
        self._filtered: list[ConversationSummary] = []
        self._search_active = False
        self._search_query = ""
        self.border_title = self.BORDER_TITLE

    def set_conversations(self, convos: list[ConversationSummary]) -> None:
        self._conversations = convos
        self._apply_filter()
        self._render_list()

    def _apply_filter(self) -> None:
        if self._search_query:
            q = self._search_query.lower()
            self._filtered = [c for c in self._conversations if q in c.display_name.lower()]
        else:
            self._filtered = list(self._conversations)

    @property
    def _visible_conversations(self) -> list[ConversationSummary]:
        return self._filtered

    def _render_list(self) -> None:
        convos = self._visible_conversations

        # Update title
        total = len(self._conversations)
        showing = len(convos)
        if self._search_query:
            self.border_title = f"\u25c8 NODES ({showing}/{total}) \u25c8"
        elif total:
            self.border_title = f"\u25c8 NODES ({total}) \u25c8"
        else:
            self.border_title = self.BORDER_TITLE

        # Fast path: same contacts in same order — update text in place
        existing = [c for c in self.children if isinstance(c, ConversationItem)]
        existing_ids = [c._convo.contact_id for c in existing]
        new_ids = [c.contact_id for c in convos]
        if existing_ids == new_ids and existing:
            for item, convo, i in zip(existing, convos, range(len(convos))):
                item.update_convo(convo, i)
            self._highlight()
            return

        # Slow path: structure changed — full rebuild
        self.remove_children()

        search = SearchBar()
        if self._search_active:
            search.add_class("--visible")
        self.mount(search)

        if not convos:
            if self._search_query:
                self.mount(Static(f"[#555555]no matches for '{self._search_query}'[/]", classes="empty-state"))
            else:
                self.mount(Static("[#555555]no nodes online[/]", classes="empty-state"))
            return

        for i, c in enumerate(convos):
            item = ConversationItem(c, i)
            self.mount(item)
        self._highlight()

    def _highlight(self) -> None:
        for child in self.children:
            if isinstance(child, ConversationItem):
                child.set_class(child.index == self.selected_index, "--highlight")

    def watch_selected_index(self) -> None:
        self._highlight()
        convos = self._visible_conversations
        if convos and 0 <= self.selected_index < len(convos):
            c = convos[self.selected_index]
            self.post_message(self.Selected(c.contact_id, c.display_name, c.platform, c.phone or "", c.pinned))

    def action_up(self) -> None:
        if self.selected_index > 0:
            self.selected_index -= 1

    def action_down(self) -> None:
        if self.selected_index < len(self._visible_conversations) - 1:
            self.selected_index += 1

    def toggle_search(self) -> None:
        """Toggle search bar visibility."""
        self._search_active = not self._search_active
        if not self._search_active:
            self._search_query = ""
            self._apply_filter()
            self._render_list()
        else:
            self._render_list()
            try:
                inp = self.query_one("#conv-search", Input)
                inp.focus()
            except Exception:
                pass

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id == "conv-search":
            self._search_query = event.value.strip()
            self._apply_filter()
            self._render_list()
            if self.selected_index >= len(self._visible_conversations):
                self.selected_index = 0

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id == "conv-search":
            self._search_active = False
            self._render_list()
            self.focus()

    def _request_context_menu(self, index: int) -> None:
        convos = self._visible_conversations
        if 0 <= index < len(convos):
            c = convos[index]
            self.post_message(self.ContextMenuRequested(
                c.contact_id, c.display_name, c.phone or "", 0, c.pinned,
            ))

    @property
    def current(self) -> ConversationSummary | None:
        convos = self._visible_conversations
        if convos and 0 <= self.selected_index < len(convos):
            return convos[self.selected_index]
        return None


# ── Chat view ─────────────────────────────────────────────

DEVICE_MAP = {
    "imessage": "MINI",
    "sms": "PIXEL",
}

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
        delivery = " \u2713" if msg.delivered else " \u2717"
    else:
        sender = contact_name
        delivery = ""
    lines = [f"{ts}  {sender}: {msg.body}{delivery}"]

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
        self.border_title = "\u256c\u2500 SELECT A NODE \u256c\u2500"

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
        phone: str = "", contact_id: int | None = None,
    ) -> None:
        self._contact_name = contact_name
        self._contact_phone = phone
        self._platform = platform
        self._raw_messages = list(messages)
        self._message_count = len(messages)
        self._update_title()
        self._messages_data = []
        self._line_directions = {}
        self._line_attachments: dict[int, Attachment] = {}

        if not messages and not contact_id:
            self._set_text("  no transmissions yet", {})
            return

        # Batch-load attachments for messages that have them
        att_map: dict[int, list[Attachment]] = {}
        msg_ids_with_att = [m.id for m in messages if m.has_attachments]
        if msg_ids_with_att:
            try:
                with get_connection() as conn:
                    att_map = get_attachments_for_messages(conn, msg_ids_with_att)
            except Exception:
                pass

        lines: list[str] = []
        line_dirs: dict[int, str] = {}
        line_atts: dict[int, Attachment] = {}
        last_date = None

        for msg in messages:
            sender = "you" if msg.direction == "out" else contact_name
            self._messages_data.append((msg.direction, sender, msg.body))

            msg_date = _local_date(msg.sent_at)
            if msg_date and msg_date != last_date:
                if lines:
                    lines.append("")
                sep = _date_label(msg_date)
                lines.append(f"\u2500\u2500\u2500 {sep} \u2500\u2500\u2500")
                lines.append("")
                last_date = msg_date

            msg_atts = att_map.get(msg.id, [])
            msg_lines = _render_message(msg, contact_name, msg_atts or None)
            line_dirs[len(lines)] = msg.direction
            lines.append(msg_lines[0])
            att_idx = 0
            for extra in msg_lines[1:]:
                if att_idx < len(msg_atts):
                    line_atts[len(lines)] = msg_atts[att_idx]
                    att_idx += 1
                lines.append(extra)

        # Show pending scheduled messages
        if contact_id is not None:
            try:
                with get_connection() as conn:
                    scheduled = get_scheduled_for_contact(conn, contact_id)
                if scheduled:
                    lines.append("")
                    lines.append("\u2500\u2500\u2500 SCHEDULED \u2500\u2500\u2500")
                    for s in scheduled:
                        lines.append(f"  [{s.scheduled_at}]  {s.body}")
            except Exception:
                pass

        if not lines:
            self._set_text("  no transmissions yet", {})
            return

        self._line_directions = line_dirs
        self._line_attachments = line_atts
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

    def get_cursor_attachment(self) -> Attachment | None:
        """Return the Attachment on the current cursor line, if any."""
        try:
            area = self.query_one("#chat-area", TextArea)
            row = area.cursor_location[0]
            return self._line_attachments.get(row)
        except Exception:
            return None

    def on_click(self, event) -> None:
        """Open attachment when an attachment line is clicked."""
        att = self.get_cursor_attachment()
        if att:
            self.app.action_view_attachment()

    @staticmethod
    def _fmt_duration(seconds: float) -> str:
        if seconds < 60:
            return f"{int(seconds)}s"
        elif seconds < 3600:
            return f"{int(seconds / 60)}m"
        else:
            h = seconds / 3600
            return f"{h:.1f}h" if h < 24 else f"{h / 24:.0f}d"

    def _reply_stats(self) -> str:
        msgs = self._raw_messages
        if len(msgs) < 2:
            return ""
        their_delays: list[float] = []
        your_delays: list[float] = []
        for i in range(1, len(msgs)):
            prev, curr = msgs[i - 1], msgs[i]
            if not prev.sent_at or not curr.sent_at:
                continue
            # Normalize to naive UTC to avoid mixed tz subtraction
            pa = prev.sent_at.replace(tzinfo=None) if prev.sent_at.tzinfo else prev.sent_at
            ca = curr.sent_at.replace(tzinfo=None) if curr.sent_at.tzinfo else curr.sent_at
            delta = (ca - pa).total_seconds()
            if delta < 0 or delta > 86400 * 7:
                continue
            if prev.direction == "out" and curr.direction == "in":
                their_delays.append(delta)
            elif prev.direction == "in" and curr.direction == "out":
                your_delays.append(delta)
        parts = []
        if their_delays:
            parts.append(f"them ~{self._fmt_duration(sum(their_delays) / len(their_delays))}")
        if your_delays:
            parts.append(f"you ~{self._fmt_duration(sum(your_delays) / len(your_delays))}")
        return " / ".join(parts)

    def _update_title(self) -> None:
        if not self._contact_name:
            self.border_title = "\u256c\u2500 SELECT A NODE \u256c\u2500"
            return
        platform_upper = self._platform.upper() if self._platform else "???"
        device = DEVICE_MAP.get(self._platform, "RELAY")
        phone = self._contact_phone or ""
        count = f"{self._message_count} msgs" if self._message_count else "0 msgs"
        reply = self._reply_stats()
        reply_part = f" // reply: {reply}" if reply else ""
        self.border_title = (
            f"\u256c\u2500 {self._contact_name.upper()} {phone} // {platform_upper} via {device} // {count}{reply_part} \u256c\u2500"
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
                    new_line = f"\n\n\u2500\u2500\u2500 {sep} \u2500\u2500\u2500\n"

            line = _render_message(msg, self._contact_name)
            if current and not current.endswith("\n"):
                new_line = "\n" + new_line

            full_text = current + new_line + line
            new_line_idx = full_text.count("\n")
            self._line_directions[new_line_idx] = msg.direction

            area.load_text(full_text)
            _apply_highlights(area, self._line_directions)
            area.refresh()
            self.call_after_refresh(self._scroll_to_end)
        except Exception:
            pass


# ── Confirm delete screen ─────────────────────────────────

class ConfirmDeleteScreen(ModalScreen[bool]):
    """Confirm conversation deletion."""

    DEFAULT_CSS = """
    ConfirmDeleteScreen {
        align: center middle;
    }
    #delete-box {
        width: 50;
        height: auto;
        background: #0d0d0d;
        border: heavy #f75341;
        padding: 1 2;
    }
    #delete-title {
        color: #f75341;
        text-style: bold;
        text-align: center;
        margin-bottom: 1;
    }
    #delete-detail {
        color: #e0e0e0;
        text-align: center;
    }
    #delete-hint {
        color: #555555;
        text-align: center;
        margin-top: 1;
    }
    """

    BINDINGS = [
        ("y", "confirm", "Yes"),
        ("n", "cancel", "No"),
        ("escape", "cancel", "Cancel"),
    ]

    def __init__(self, contact_name: str, message_count: int) -> None:
        super().__init__()
        self._contact_name = contact_name
        self._message_count = message_count

    def compose(self) -> ComposeResult:
        with Vertical(id="delete-box"):
            yield Static("\u25c8 DELETE CONVERSATION \u25c8", id="delete-title")
            yield Static(
                f"Purge [bold]{self._contact_name}[/bold]?\n"
                f"{self._message_count} message{'s' if self._message_count != 1 else ''} will be destroyed.",
                id="delete-detail",
            )
            yield Static("[#f75341]y[/] confirm  /  [#555555]n[/] cancel", id="delete-hint")

    def action_confirm(self) -> None:
        self.dismiss(True)

    def action_cancel(self) -> None:
        self.dismiss(False)


# ── Context menu ──────────────────────────────────────────


class ConversationContextMenu(ModalScreen[str | None]):
    """Right-click / 'm' context menu for a conversation."""

    DEFAULT_CSS = """
    ConversationContextMenu {
        align: center middle;
    }
    #ctx-box {
        width: 40;
        height: auto;
        background: #0d0d0d;
        border: heavy #00d4ff;
        padding: 1 2;
    }
    #ctx-title {
        color: #00d4ff;
        text-style: bold;
        text-align: center;
        margin-bottom: 1;
    }
    #ctx-phone {
        color: #555555;
        text-align: center;
    }
    .ctx-option {
        height: 1;
        padding: 0 1;
        color: #e0e0e0;
    }
    .ctx-option:hover {
        background: #152030;
        color: #00d4ff;
    }
    #ctx-hint {
        color: #555555;
        text-align: center;
        margin-top: 1;
    }
    """

    BINDINGS = [
        ("escape", "cancel", "Cancel"),
        ("d", "pick_delete", "Delete"),
        ("c", "pick_copy", "Copy"),
        ("r", "pick_read", "Read"),
        ("p", "pick_pin", "Pin"),
    ]

    def __init__(self, contact_name: str, phone: str, message_count: int, pinned: bool = False) -> None:
        super().__init__()
        self._contact_name = contact_name
        self._phone = phone
        self._message_count = message_count
        self._pinned = pinned

    def compose(self) -> ComposeResult:
        pin_label = "Unpin from top" if self._pinned else "Pin to top"
        with Vertical(id="ctx-box"):
            yield Static(f"\u25c8 {self._contact_name} \u25c8", id="ctx-title")
            if self._phone:
                yield Static(self._phone, id="ctx-phone")
            yield _CtxOption(f"[#ff8c00]p[/]  {pin_label}", "toggle_pin")
            yield _CtxOption("[#f75341]d[/]  Delete conversation", "delete")
            yield _CtxOption("[#00d4ff]c[/]  Copy phone number", "copy_phone")
            yield _CtxOption("[#50fa7b]r[/]  Mark as read", "mark_read")
            yield Static("Esc cancel", id="ctx-hint")

    def action_pick_delete(self) -> None:
        self.dismiss("delete")

    def action_pick_copy(self) -> None:
        self.dismiss("copy_phone")

    def action_pick_read(self) -> None:
        self.dismiss("mark_read")

    def action_pick_pin(self) -> None:
        self.dismiss("toggle_pin")

    def action_cancel(self) -> None:
        self.dismiss(None)


class _CtxOption(Static):
    """Clickable context menu item."""

    def __init__(self, label: str, action_id: str) -> None:
        super().__init__(label, classes="ctx-option")
        self._action_id = action_id

    def on_click(self, event) -> None:
        event.stop()
        self.screen.dismiss(self._action_id)


# ── New message screen ────────────────────────────────────

_PHONE_RE = re.compile(r"^\+?\d[\d\s\-]{6,}$")


class FilePickerScreen(ModalScreen[str | None]):
    """File browser for selecting attachments."""

    DEFAULT_CSS = """
    FilePickerScreen {
        align: center middle;
    }
    #filepicker-box {
        width: 70;
        height: 80%;
        background: #0d0d0d;
        border: heavy #ff8c00;
        padding: 1 2;
    }
    #filepicker-title {
        text-align: center;
        text-style: bold;
        color: #ff8c00;
        height: 1;
        margin-bottom: 1;
    }
    #filepicker-path {
        height: 1;
        color: #555555;
        margin-bottom: 1;
    }
    #filepicker-tree {
        height: 1fr;
        background: #0a0a0a;
    }
    #filepicker-hint {
        height: 1;
        color: #555555;
        margin-top: 1;
    }
    """

    BINDINGS = [("escape", "cancel", "Cancel")]

    def __init__(self, start_path: str = "~") -> None:
        super().__init__()
        self._start = Path(start_path).expanduser()

    def compose(self) -> ComposeResult:
        with Vertical(id="filepicker-box"):
            yield Static("\u25c8 SELECT FILE \u25c8", id="filepicker-title")
            yield Static(str(self._start), id="filepicker-path")
            yield DirectoryTree(str(self._start), id="filepicker-tree")
            yield Static("click file to attach // esc to cancel", id="filepicker-hint")

    def on_directory_tree_file_selected(self, event: DirectoryTree.FileSelected) -> None:
        self.dismiss(str(event.path))

    def action_cancel(self) -> None:
        self.dismiss(None)


class NewMessageScreen(ModalScreen[int | None]):
    """Autocomplete contact picker."""

    DEFAULT_CSS = """
    NewMessageScreen {
        align: center middle;
    }
    #new-msg-box {
        width: 50;
        max-height: 20;
        background: #0d0d0d;
        border: heavy #00d4ff;
        padding: 1 2;
    }
    #new-msg-title {
        color: #00d4ff;
        text-style: bold;
        text-align: center;
        margin-bottom: 1;
    }
    #new-msg-input {
        background: #111111;
        color: #e0e0e0;
        border: none;
        width: 100%;
    }
    #new-msg-input:focus {
        border: none;
    }
    #contact-results {
        height: auto;
        max-height: 10;
        background: #0a0a0a;
        color: #e0e0e0;
        margin-top: 1;
    }
    #contact-results > .option-list--option-highlighted {
        background: #111a22;
        color: #00d4ff;
    }
    #new-msg-hint {
        color: #555555;
        margin-top: 1;
    }
    """

    BINDINGS = [("escape", "cancel", "Cancel")]

    def compose(self) -> ComposeResult:
        with Vertical(id="new-msg-box"):
            yield Static("\u25c8 NEW MESSAGE \u25c8", id="new-msg-title")
            yield Input(placeholder="search contacts...", id="new-msg-input")
            yield OptionList(id="contact-results")
            yield Static("type name or phone number", id="new-msg-hint")

    def on_mount(self) -> None:
        self.query_one("#new-msg-input", Input).focus()

    def on_input_changed(self, event: Input.Changed) -> None:
        query = event.value.strip()
        option_list = self.query_one("#contact-results", OptionList)
        option_list.clear_options()
        hint = self.query_one("#new-msg-hint", Static)

        if not query:
            hint.update("type name or phone number")
            return

        with get_connection() as conn:
            contacts = search_contacts(conn, query)

        for c in contacts[:10]:
            label = f"{c.display_name}  [#555555]{c.phone or ''}[/]"
            option_list.add_option(Option(label, id=str(c.id)))

        if not contacts and _PHONE_RE.match(query):
            option_list.add_option(Option(f"[#50fa7b]+ Create new contact: {query}[/]", id="__new__"))
            hint.update("enter to create new contact")
        elif not contacts:
            hint.update("no matches")
        else:
            hint.update(f"{len(contacts)} contact{'s' if len(contacts) != 1 else ''} found")

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        option_id = event.option.id
        if option_id == "__new__":
            phone = self.query_one("#new-msg-input", Input).value.strip()
            with get_connection() as conn:
                contact_id = upsert_contact(conn, phone, phone)
            self.dismiss(contact_id)
        else:
            self.dismiss(int(option_id))

    def action_cancel(self) -> None:
        self.dismiss(None)


# ── Reply editor screen ──────────────────────────────────

class ReplyEditorScreen(Screen):
    """Full-screen editor for composing replies."""

    CSS = """
    ReplyEditorScreen {
        background: #0a0a0a;
    }

    #editor-tabs {
        dock: top;
        height: 1;
        background: #0d0d0d;
    }

    #editor-context {
        height: auto;
        max-height: 40%;
        background: #0d0d0d;
        color: #555555;
        padding: 1 2;
        border-bottom: heavy #00d4ff;
    }

    #editor-area {
        background: #0a0a0a;
        color: #e0e0e0;
        border: none;
    }

    #editor-area:focus {
        border: none;
    }

    #editor-buttons {
        dock: bottom;
        height: 1;
        background: #0d0d0d;
    }

    #send-btn {
        width: auto;
        height: 1;
        background: #1a3a1a;
        color: #50fa7b;
        text-style: bold;
        margin: 0 1 0 0;
    }

    #send-btn:hover {
        background: #50fa7b;
        color: #0a0a0a;
    }

    #cancel-btn {
        width: auto;
        height: 1;
        background: #2a1a1a;
        color: #f75341;
        text-style: bold;
        margin: 0 1 0 0;
    }

    #cancel-btn:hover {
        background: #f75341;
        color: #0a0a0a;
    }

    #reload-btn {
        width: auto;
        height: 1;
        background: #1a1a2a;
        color: #00d4ff;
        text-style: bold;
    }

    #reload-btn:hover {
        background: #00d4ff;
        color: #0a0a0a;
    }

    #claude-bar {
        dock: bottom;
        height: 1;
        background: #0d0d0d;
    }

    #claude-btn {
        width: auto;
        height: 1;
        background: #2a1a2a;
        color: #ff8c00;
        text-style: bold;
        margin: 0 1 0 0;
    }

    #claude-btn:hover {
        background: #ff8c00;
        color: #0a0a0a;
    }

    #claude-input {
        width: 1fr;
        height: 1;
        background: #111111;
        color: #e0e0e0;
        border: none;
    }

    #claude-input:focus {
        border: none;
    }
    """

    BINDINGS = [
        Binding("ctrl+q", "cancel", "Cancel", show=False),
    ]

    def __init__(self, context_lines: list[str], contact_name: str) -> None:
        super().__init__()
        self._context_lines = context_lines
        self._contact_name = contact_name
        self._result: str = ""
        self._last_file_mtime: float = 0.0
        self._last_written_body: str = ""

    def compose(self) -> ComposeResult:
        with Horizontal(id="editor-tabs"):
            yield TabButton(" MESSAGES ", id="tab-messages", classes="tab-btn")
            yield TabButton(" COMPOSE ", id="tab-editor", classes="tab-btn --active")
        context_text = "\n".join(f"  {line}" for line in self._context_lines)
        yield Static(context_text, id="editor-context")
        yield TextArea("", id="editor-area")
        with Horizontal(id="claude-bar"):
            yield _ClaudeButton(" CLAUDE ", id="claude-btn")
            yield Input(placeholder="rewrite instruction...", id="claude-input")
        with Horizontal(id="editor-buttons"):
            yield _SendButton(" SEND ", id="send-btn")
            yield _CancelButton(" CANCEL ", id="cancel-btn")
            yield _ReloadButton(" RELOAD ", id="reload-btn")

    def on_mount(self) -> None:
        self._write_file("")
        self._last_file_mtime = REPLY_PATH.stat().st_mtime if REPLY_PATH.exists() else 0
        self.query_one("#editor-area", TextArea).focus()
        self.set_interval(1.0, self._check_file_changed)

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id == "claude-input":
            self.action_claude_rewrite()

    def on_text_area_changed(self, event: TextArea.Changed) -> None:
        body = event.text_area.text
        self._write_file(body)

    def _write_file(self, body: str) -> None:
        REPLY_PATH.parent.mkdir(exist_ok=True)
        quoted = [f"# {line}" for line in self._context_lines]
        REPLY_PATH.write_text("\n".join(quoted) + "\n\n" + body)
        self._last_written_body = body
        self._last_file_mtime = REPLY_PATH.stat().st_mtime

    def _check_file_changed(self) -> None:
        if not REPLY_PATH.exists():
            return
        mtime = REPLY_PATH.stat().st_mtime
        if mtime <= self._last_file_mtime:
            return
        body = self._read_file()
        if body == self._last_written_body:
            self._last_file_mtime = mtime
            return
        self._last_file_mtime = mtime
        self._last_written_body = body
        area = self.query_one("#editor-area", TextArea)
        area.load_text(body)

    def _read_file(self) -> str:
        if not REPLY_PATH.exists():
            return ""
        lines = REPLY_PATH.read_text().splitlines()
        body_lines = [line for line in lines if not line.startswith("#")]
        return "\n".join(body_lines).strip()

    def action_send(self) -> None:
        area = self.query_one("#editor-area", TextArea)
        self._result = area.text.strip()
        if self._result:
            self._write_file(self._result)
        self.dismiss(self._result)

    def action_cancel(self) -> None:
        self.dismiss("")

    def action_reload(self) -> None:
        body = self._read_file()
        area = self.query_one("#editor-area", TextArea)
        area.load_text(body)

    def action_claude_rewrite(self) -> None:
        inp = self.query_one("#claude-input", Input)
        instruction = inp.value.strip() or "rewrite more concisely"
        inp.value = ""
        threading.Thread(
            target=self._run_rewrite, args=(instruction,), daemon=True
        ).start()

    def _run_rewrite(self, instruction: str) -> None:
        rewrite_path = Path.home() / "bin" / "rewrite"
        try:
            result = subprocess.run(
                [str(rewrite_path), instruction],
                capture_output=True,
                text=True,
                timeout=60,
            )
            if result.returncode != 0:
                stderr = result.stderr.strip() or "unknown error"
                self.app.call_from_thread(
                    self.app.notify, f"Rewrite failed: {stderr}", severity="error"
                )
        except FileNotFoundError:
            self.app.call_from_thread(
                self.app.notify, "~/bin/rewrite not found", severity="error"
            )
        except subprocess.TimeoutExpired:
            self.app.call_from_thread(
                self.app.notify, "Rewrite timed out (60s)", severity="error"
            )
        except Exception as e:
            self.app.call_from_thread(
                self.app.notify, f"Rewrite error: {e}", severity="error"
            )


class _ClaudeButton(Static):
    def on_click(self, event) -> None:
        event.stop()
        self.screen.action_claude_rewrite()


class _SendButton(Static):
    def on_click(self, event) -> None:
        event.stop()
        self.screen.action_send()


class _CancelButton(Static):
    def on_click(self, event) -> None:
        event.stop()
        self.screen.action_cancel()


class _ReloadButton(Static):
    def on_click(self, event) -> None:
        event.stop()
        self.screen.action_reload()


# ── Help screen ───────────────────────────────────────────

class HelpScreen(ModalScreen):
    """Keybinding reference overlay."""

    DEFAULT_CSS = """
    HelpScreen {
        align: center middle;
    }
    #help-box {
        width: 56;
        height: auto;
        max-height: 80%;
        background: #0d0d0d;
        border: heavy #00d4ff;
        padding: 1 2;
    }
    #help-title {
        color: #00d4ff;
        text-style: bold;
        text-align: center;
        margin-bottom: 1;
    }
    #help-body {
        color: #e0e0e0;
    }
    #help-hint {
        color: #555555;
        text-align: center;
        margin-top: 1;
    }
    """

    BINDINGS = [
        ("escape", "dismiss", "Close"),
        ("question_mark", "dismiss", "Close"),
    ]

    def compose(self) -> ComposeResult:
        with Vertical(id="help-box"):
            yield Static("\u25c8 KEYBINDINGS \u25c8", id="help-title")
            yield Static(
                "[#00d4ff bold]NAVIGATION[/]\n"
                "  [#50fa7b]j / k[/]       move down / up in sidebar\n"
                "  [#50fa7b]g / G[/]       scroll chat to top / bottom\n"
                "  [#50fa7b]Tab[/]         toggle focus: sidebar <-> compose\n"
                "  [#50fa7b]Esc[/]         close search / clear compose\n"
                "\n"
                "[#00d4ff bold]ACTIONS[/]\n"
                "  [#50fa7b]r[/]           focus compose box (quick reply)\n"
                "  [#50fa7b]n[/]           new message\n"
                "  [#50fa7b]d[/]           delete conversation\n"
                "  [#50fa7b]m[/]           context menu (delete/copy/mark read)\n"
                "  [#50fa7b]/[/]           search/filter conversations\n"
                "  [#50fa7b]?[/]           this help screen\n"
                "\n"
                "[#00d4ff bold]COMPOSE[/]\n"
                "  [#50fa7b]ctrl+g[/]      suggest reply (Claude)\n"
                "  [#50fa7b]Enter[/]       send message\n"
                "  [#50fa7b]REPLY btn[/]   open full editor\n"
                "  [#50fa7b]CLAUDE btn[/]  AI rewrite in editor\n"
                "\n"
                "[#00d4ff bold]SCHEDULE[/]\n"
                "  [#50fa7b]/at 9:30am[/]  schedule message (today)\n"
                "  [#50fa7b]/at 9am tomorrow[/] schedule for tomorrow\n"
                "  [#50fa7b]/at 3/15 2pm[/] schedule for a date\n"
                "  [#50fa7b]/cancel[/]     cancel last scheduled\n"
                "  [#50fa7b]/cancel all[/] cancel all scheduled\n"
                "  [#50fa7b]/scheduled[/]  list pending scheduled\n"
                "\n"
                "[#00d4ff bold]CHAT (read-only TextArea)[/]\n"
                "  [#50fa7b]click+drag[/]  select text\n"
                "  [#50fa7b]shift+arrows[/] select with keyboard\n"
                "  [#50fa7b]ctrl+c[/]      copy selection\n"
                "  [#50fa7b]ctrl+a[/]      select all\n"
                "  [#50fa7b]Home/End[/]    scroll to top/bottom\n",
                id="help-body",
            )
            yield Static("press [#00d4ff]?[/] or [#00d4ff]Esc[/] to close", id="help-hint")


# ── Main app ──────────────────────────────────────────────

class M0usuNetApp(App):
    """Unified messaging TUI."""

    TITLE = "m0usunet"
    CSS_PATH = CSS_PATH

    BINDINGS = [
        Binding("ctrl+c", "copy_selection", "Copy", show=False),
        Binding("tab", "toggle_focus", "Focus", show=False),
        Binding("escape", "escape", "Esc", show=False),
        Binding("slash", "search", "Search", show=False),
        Binding("r", "focus_compose", "Reply", show=False),
        Binding("n", "new_message", "New", show=False),
        Binding("d", "delete_conversation", "Delete", show=False),
        Binding("j", "nav_down", "Down", show=False),
        Binding("k", "nav_up", "Up", show=False),
        Binding("g", "chat_top", "Top", show=False),
        Binding("G", "chat_bottom", "Bottom", show=False, key_display="shift+g"),
        Binding("m", "context_menu", "Menu", show=False),
        Binding("question_mark", "show_help", "Help", show=False),
        Binding("ctrl+g", "suggest_reply", "Suggest", show=False),
        Binding("v", "view_attachment", "View", show=False),
    ]

    def __init__(self) -> None:
        super().__init__()
        self._current_contact_id: int | None = None
        self._current_contact_name: str = ""
        self._current_platform: str = ""
        self._current_phone: str = ""
        self._current_pinned: bool = False
        self._ingest_thread = None
        self._initial_select_done = False
        self._last_msg_count: int = 0
        self._prev_total_unread: int = 0
        self._suggesting = False

    def compose(self) -> ComposeResult:
        yield HeaderBar()
        yield ConversationList()
        yield ChatView()
        yield ComposeBox()

    def on_mount(self) -> None:
        ensure_schema()
        self._refresh_conversations()
        self.set_interval(5.0, self._refresh_conversations)
        try:
            from .ingest import start_background
            self._ingest_thread = start_background(interval=30)
        except Exception as e:
            log.info("Ingest poller not started (normal on Mac): %s", e)
        try:
            from .scheduler import start_background as start_scheduler
            self._scheduler_thread = start_scheduler(interval=30)
        except Exception as e:
            log.info("Scheduler not started: %s", e)

    def _refresh_conversations(self) -> None:
        # Skip refresh if a screen is pushed on top (e.g. NewMessageScreen)
        if len(self.screen_stack) > 1:
            return

        with get_connection() as conn:
            convos = conversation_list(conn)

            current_msg_count = 0
            if self._current_contact_id is not None:
                row = conn.execute(
                    "SELECT COUNT(*) AS cnt FROM messages WHERE contact_id = ?",
                    (self._current_contact_id,),
                ).fetchone()
                current_msg_count = row["cnt"] if row else 0

                if current_msg_count != self._last_msg_count:
                    self._last_msg_count = current_msg_count
                    self._load_chat()

        # Unread count + bell
        total_unread = sum(c.unread_count for c in convos)
        try:
            self.query_one(HeaderBar).update_unread(total_unread)
        except Exception:
            pass
        if total_unread > self._prev_total_unread and self._prev_total_unread >= 0:
            self.bell()
        self._prev_total_unread = total_unread

        try:
            conv_list = self.query_one(ConversationList)
        except Exception:
            return
        new_ids = [(c.contact_id, c.last_message) for c in convos]
        if hasattr(self, "_last_conv_ids") and self._last_conv_ids == new_ids:
            return
        self._last_conv_ids = new_ids

        conv_list.set_conversations(convos)

        if not self._initial_select_done and convos:
            self._initial_select_done = True
            c = convos[0]
            conv_list.post_message(
                ConversationList.Selected(c.contact_id, c.display_name, c.platform)
            )

    def on_conversation_list_selected(self, event: ConversationList.Selected) -> None:
        self._current_contact_id = event.contact_id
        self._current_contact_name = event.display_name
        self._current_platform = event.platform
        self._current_phone = event.phone
        self._current_pinned = event.pinned
        self._last_msg_count = 0
        self._load_chat()
        with get_connection() as conn:
            mark_viewed(conn, event.contact_id)
        compose = self.query_one(ComposeBox)
        compose.set_contact_name(event.display_name)
        compose.clear_status()

    def _load_chat(self) -> None:
        if self._current_contact_id is None:
            return
        with get_connection() as conn:
            msgs = get_messages(conn, self._current_contact_id)
        chat = self.query_one(ChatView)
        chat.set_messages(
            msgs, self._current_contact_name, self._current_platform,
            phone=self._current_phone, contact_id=self._current_contact_id,
        )

    # ── Navigation ─────────────────────────────────────────

    def _in_text_input(self) -> bool:
        focused = self.focused
        if focused is None:
            return False
        return isinstance(focused, Input) or (hasattr(focused, '__class__') and focused.__class__.__name__ == 'TextArea')

    def action_nav_down(self) -> None:
        if self._in_text_input():
            return
        self.query_one(ConversationList).action_down()

    def action_nav_up(self) -> None:
        if self._in_text_input():
            return
        self.query_one(ConversationList).action_up()

    def action_chat_top(self) -> None:
        if self._in_text_input():
            return
        self.query_one(ChatView).scroll_home(animate=False)

    def action_chat_bottom(self) -> None:
        if self._in_text_input():
            return
        self.query_one(ChatView).scroll_end(animate=False)

    def action_search(self) -> None:
        if self._in_text_input():
            return
        self.query_one(ConversationList).toggle_search()

    def action_focus_compose(self) -> None:
        if self._in_text_input():
            return
        try:
            self.query_one(ComposeBox).show_input()
        except Exception:
            pass

    def action_new_message(self) -> None:
        if self._in_text_input():
            return
        self.on_conversation_list_new_message_requested()

    def action_context_menu(self) -> None:
        if self._in_text_input():
            return
        if self._current_contact_id is None:
            return
        with get_connection() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS cnt FROM messages WHERE contact_id = ?",
                (self._current_contact_id,),
            ).fetchone()
            count = row["cnt"] if row else 0
        self.push_screen(
            ConversationContextMenu(
                self._current_contact_name, self._current_phone, count,
                pinned=self._current_pinned,
            ),
            callback=self._on_context_menu_done,
        )

    def on_conversation_list_context_menu_requested(
        self, event: ConversationList.ContextMenuRequested,
    ) -> None:
        self._current_contact_id = event.contact_id
        self._current_contact_name = event.display_name
        self._current_phone = event.phone
        self._current_pinned = event.pinned
        self.push_screen(
            ConversationContextMenu(
                event.display_name, event.phone, event.message_count,
                pinned=event.pinned,
            ),
            callback=self._on_context_menu_done,
        )

    def _on_context_menu_done(self, result: str | None) -> None:
        if result is None:
            return
        if result == "delete":
            self.action_delete_conversation()
        elif result == "copy_phone":
            if self._current_phone:
                copy_osc52(self._current_phone)
                self.notify(f"Copied: {self._current_phone}")
            else:
                self.notify("No phone number", severity="warning")
        elif result == "mark_read":
            if self._current_contact_id is not None:
                with get_connection() as conn:
                    mark_viewed(conn, self._current_contact_id)
                self._last_conv_ids = None
                self._refresh_conversations()
                self.notify("Marked as read")
        elif result == "toggle_pin":
            if self._current_contact_id is not None:
                with get_connection() as conn:
                    now_pinned = toggle_pin(conn, self._current_contact_id)
                self._current_pinned = now_pinned
                self._last_conv_ids = None
                self._refresh_conversations()
                self.notify("Pinned" if now_pinned else "Unpinned")

    def action_show_help(self) -> None:
        if self._in_text_input():
            return
        self.push_screen(HelpScreen())

    # ── Attachment viewer ──────────────────────────────────

    def action_attach_file(self) -> None:
        if self._current_contact_id is None:
            return
        self.push_screen(FilePickerScreen("~"), callback=self._on_file_picked)

    def _on_file_picked(self, path: str | None) -> None:
        if not path:
            return
        try:
            compose = self.query_one(ComposeBox)
            compose.set_attachment(path)
            compose.show_input()
        except Exception:
            pass

    def action_view_attachment(self) -> None:
        if self._in_text_input():
            return
        try:
            chat = self.query_one(ChatView)
        except Exception:
            return
        att = chat.get_cursor_attachment()
        if not att:
            return
        local = att.local_path
        if not local or att.download_status != "done":
            return
        mime = att.mime_type or ""
        if mime.startswith("image/"):
            self._view_image(local)
        else:
            self._view_file(local, att.filename)

    def _view_image(self, path: str) -> None:
        """Suspend TUI and display image with kitten icat."""
        import shutil
        viewer = shutil.which("kitten")
        if not viewer:
            return
        with self.suspend():
            subprocess.run([viewer, "icat", "--hold", path])

    def _view_file(self, path: str, filename: str) -> None:
        """Suspend TUI and display file info / text content."""
        with self.suspend():
            print(f"\n  File: {filename}")
            print(f"  Path: {path}")
            mime = ""
            try:
                r = subprocess.run(["file", "--mime-type", "-b", path],
                                   capture_output=True, text=True, timeout=5)
                mime = r.stdout.strip()
                print(f"  Type: {mime}")
            except Exception:
                pass
            if mime.startswith("text/"):
                print()
                try:
                    with open(path) as f:
                        print(f.read()[:2000])
                except Exception:
                    pass
            print("\n  Press Enter to return...")
            input()

    # ── New Message modal ──────────────────────────────────

    def on_conversation_list_new_message_requested(self) -> None:
        self.push_screen(NewMessageScreen(), callback=self._on_new_message_done)

    def _on_new_message_done(self, contact_id: int | None) -> None:
        if contact_id is None:
            return
        self._last_conv_ids = None
        self._refresh_conversations()
        conv_list = self.query_one(ConversationList)
        for i, c in enumerate(conv_list._visible_conversations):
            if c.contact_id == contact_id:
                conv_list.selected_index = i
                return
        with get_connection() as conn:
            contact = get_contact(conn, contact_id)
        if contact:
            self._current_contact_id = contact_id
            self._current_contact_name = contact.display_name
            self._current_platform = ""
            self._current_phone = contact.phone or ""
            self._last_msg_count = 0
            chat = self.query_one(ChatView)
            chat.set_messages([], contact.display_name, "", phone=contact.phone or "")
            compose = self.query_one(ComposeBox)
            compose.set_contact_name(contact.display_name)
            compose.clear_status()

    # ── Delete Conversation modal ─────────────────────────

    def action_delete_conversation(self) -> None:
        if self._in_text_input():
            return
        if self._current_contact_id is None:
            return
        with get_connection() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS cnt FROM messages WHERE contact_id = ?",
                (self._current_contact_id,),
            ).fetchone()
            count = row["cnt"] if row else 0
        self.push_screen(
            ConfirmDeleteScreen(self._current_contact_name, count),
            callback=self._on_delete_confirmed,
        )

    def _on_delete_confirmed(self, confirmed: bool) -> None:
        if not confirmed:
            return
        with get_connection() as conn:
            delete_messages_for_contact(conn, self._current_contact_id)
        self._current_contact_id = None
        self._current_contact_name = ""
        self._current_platform = ""
        self._current_phone = ""
        self._last_msg_count = 0
        self._last_conv_ids = None
        self._refresh_conversations()
        chat = self.query_one(ChatView)
        chat.set_messages([], "", "")

    # ── Tab styling helper ────────────────────────────────

    def _update_tabs(self, active: str) -> None:
        try:
            header = self.query_one(HeaderBar)
        except Exception:
            return
        for tab_id in ("tab-messages", "tab-compose"):
            try:
                btn = header.query_one(f"#{tab_id}")
                btn.set_class(tab_id == f"tab-{active}", "--active")
            except Exception:
                pass

    # ── Compose / send ────────────────────────────────────

    def on_compose_box_submitted(self, event: ComposeBox.Submitted) -> None:
        if self._current_contact_id is None:
            return
        body = event.body

        # Handle /cancel command
        if body.lower() in ("/cancel", "/cancel all"):
            cancel_all = body.lower() == "/cancel all"
            with get_connection() as conn:
                count = cancel_scheduled(conn, self._current_contact_id, cancel_all=cancel_all)
            compose = self.query_one(ComposeBox)
            if count:
                compose.show_status(f"\u25c9 cancelled {count} scheduled message{'s' if count != 1 else ''}")
            else:
                compose.show_status("\u25c9 no pending scheduled messages", error=True)
            self._load_chat()
            return

        # Handle /scheduled command
        if body.lower() == "/scheduled":
            with get_connection() as conn:
                scheduled = get_scheduled_for_contact(conn, self._current_contact_id)
            compose = self.query_one(ComposeBox)
            if scheduled:
                info = "; ".join(f"{s.scheduled_at}: {s.body[:30]}" for s in scheduled)
                compose.show_status(f"\u25c9 {len(scheduled)} pending: {info}")
            else:
                compose.show_status("\u25c9 no scheduled messages")
            return

        # Handle /at scheduling command
        if body.lower().startswith("/at "):
            self._handle_schedule_command(body[4:])
            return

        self._send_body(body, attachment_path=event.attachment_path)

    def action_reply_editor(self) -> None:
        if isinstance(self.screen, ReplyEditorScreen):
            return
        if self._current_contact_id is None:
            return
        chat = self.query_one(ChatView)
        contact_name = self._current_contact_name
        context_lines = []
        for direction, sender, body in chat.get_messages_text()[-10:]:
            context_lines.append(f"{sender}: {body}")
        screen = ReplyEditorScreen(context_lines, contact_name)
        self.push_screen(screen, callback=self._on_reply_editor_done)
        self._update_tabs("editor")

    def action_show_messages(self) -> None:
        if isinstance(self.screen, ReplyEditorScreen):
            self.screen.dismiss("")
        self._update_tabs("messages")

    def _on_reply_editor_done(self, result: str) -> None:
        self._update_tabs("messages")
        if result:
            self._send_body(result)

    def _handle_schedule_command(self, text: str) -> None:
        """Parse and schedule: /at <time> [tomorrow|date] <message>."""
        compose = self.query_one(ComposeBox)
        parsed = _parse_schedule_time(text)
        if parsed is None:
            compose.show_status("\u25c9 bad format. use: /at 9:30am Hey or /at 9:30am tomorrow Hey", error=True)
            return
        scheduled_at, message = parsed
        with get_connection() as conn:
            add_scheduled_message(conn, self._current_contact_id, message, scheduled_at)
        compose.show_status(f"\u25c9 scheduled for {scheduled_at}")
        self._load_chat()

    def _send_body(self, body: str, attachment_path: str = "") -> None:
        if self._current_contact_id is None:
            return
        contact_name = self._current_contact_name
        contact_id = self._current_contact_id
        compose = self.query_one(ComposeBox)
        status = "\u25c9 sending..." if not attachment_path else "\u25c9 uploading + sending..."
        compose.show_status(status)
        threading.Thread(
            target=self._do_send,
            args=(contact_id, contact_name, body, attachment_path),
            daemon=True,
        ).start()

    def _do_send(self, contact_id: int, contact_name: str, body: str,
                 attachment_path: str = "") -> None:
        # If there's an attachment, SCP it to Mini first, then send via MQTT
        if attachment_path:
            try:
                output = self._send_with_attachment(contact_name, body, attachment_path)
                success = True
            except Exception as e:
                output = str(e)
                success = False
        else:
            try:
                output = send_message(contact_name, body)
                success = True
            except RelayError as e:
                output = str(e)
                success = False

        platform_str = "sms"
        if "imessage" in output.lower() or "imsg" in output.lower():
            platform_str = "imessage"

        with get_connection() as conn:
            mid = add_message(
                conn, contact_id, platform_str, "out", body,
                delivered=success, relay_output=output,
            )
            msg = Message(
                id=mid, contact_id=contact_id, platform=platform_str,
                direction="out", body=body, delivered=success,
                relay_output=output,
            )

        self.call_from_thread(self._on_send_done, msg, output, success)

    def _on_send_done(self, msg: Message, output: str, success: bool) -> None:
        chat = self.query_one(ChatView)
        chat.append_message(msg)
        compose = self.query_one(ComposeBox)
        if success:
            compose.show_status(f"\u25c9 RELAY OK  {output}")
        else:
            compose.show_status(f"\u25c9 RELAY FAIL  {output}", error=True)

    def _send_with_attachment(self, contact_name: str, body: str,
                              local_path: str) -> str:
        """SCP file to Mini, then send via MQTT with attachment."""
        import json as _json
        filename = local_path.rsplit("/", 1)[-1]
        remote_path = f"/tmp/m0usunet-send-{filename}"

        # SCP to Mini
        r = subprocess.run(
            ["scp", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10",
             local_path, f"mini:{remote_path}"],
            capture_output=True, text=True, timeout=120,
        )
        if r.returncode != 0:
            raise RelayError(f"SCP failed: {r.stderr.strip()}")

        # Resolve phone number
        from .db import get_connection, get_contact
        with get_connection() as conn:
            contact = get_contact(conn, self._current_contact_id)
        if not contact or not contact.phone:
            raise RelayError("no phone number for contact")

        # Send via MQTT with attachment path
        payload = _json.dumps({
            "number": contact.phone,
            "message": body if body and body != "[attachment]" else "",
            "attachment": remote_path,
        })
        r = subprocess.run(
            ["mosquitto_pub", "-h", "192.168.0.15", "-p", "8883",
             "--cafile", "/home/jackpi5/mini-mqtt.crt",
             "-t", "cmd/mini/imessage/send", "-m", payload],
            capture_output=True, text=True, timeout=10,
        )
        if r.returncode != 0:
            raise RelayError(f"MQTT publish failed: {r.stderr.strip()}")

        return f"iMessage (MQTT+attachment) -> {contact_name}: {filename}"

    def action_toggle_focus(self) -> None:
        focused = self.focused
        if focused and focused.id == "compose-input":
            try:
                self.query_one(ComposeBox)._hide_input()
            except Exception:
                pass
            self.query_one(ConversationList).focus()
        else:
            try:
                self.query_one(ComposeBox).show_input()
            except Exception:
                pass

    # ── Claude suggestion ───────────────────────────────

    def action_suggest_reply(self) -> None:
        if self._current_contact_id is None:
            return
        if self._suggesting:
            return
        chat = self.query_one(ChatView)
        messages = chat.get_messages_text()
        if not messages:
            return
        compose = self.query_one(ComposeBox)
        compose.show_status("\u25c9 thinking...")
        self._suggesting = True
        contact_name = self._current_contact_name
        threading.Thread(
            target=self._run_suggest, args=(messages, contact_name), daemon=True
        ).start()

    def _run_suggest(self, messages: list, contact_name: str) -> None:
        from .suggest import suggest_reply
        result = suggest_reply(messages, contact_name)
        self.call_from_thread(self._on_suggest_done, result)

    def _on_suggest_done(self, result: str) -> None:
        self._suggesting = False
        compose = self.query_one(ComposeBox)
        if result.startswith("ERROR:"):
            compose.show_status(f"\u25c9 {result}", error=True)
            return
        try:
            inp = self.query_one("#compose-input", Input)
            inp.value = result
            inp.focus()
        except Exception:
            pass
        compose.show_status("\u25c9 suggestion loaded \u2014 edit or hit Enter to send")

    def action_copy_selection(self) -> None:
        """Copy selected text to system clipboard via OSC 52."""
        import base64, os
        try:
            area = self.query_one("#chat-area", TextArea)
            text = area.selected_text
            if not text:
                return
            b64 = base64.b64encode(text.encode()).decode()
            # Write directly to the TTY, bypassing Textual's stdout
            try:
                tty = os.open("/dev/tty", os.O_WRONLY)
                os.write(tty, f"\033]52;c;{b64}\a".encode())
                os.close(tty)
            except OSError:
                pass
        except Exception:
            pass

    def action_escape(self) -> None:
        conv_list = self.query_one(ConversationList)
        if conv_list._search_active:
            conv_list.toggle_search()
            conv_list.focus()
            return
        try:
            inp = self.query_one("#compose-input", Input)
            inp.value = ""
        except Exception:
            pass
        conv_list.focus()
