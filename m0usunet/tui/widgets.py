"""All TUI widget classes."""

from __future__ import annotations

import datetime as _dt
import logging
import subprocess
import threading
from datetime import datetime, timezone

from textual.app import ComposeResult
from textual.containers import Horizontal, Vertical, VerticalScroll
from textual.message import Message as TMessage
from textual.reactive import reactive
from textual.widget import Widget
from textual.widgets import Input, Static, TextArea

from ..db import (
    Attachment, ConversationSummary, Message,
    cancel_scheduled_by_id, get_all_scheduled,
    get_attachments_for_messages, get_connection,
    get_scheduled_for_contact,
)

from .helpers import (
    DEVICE_MAP, MESH_DEVICES, PLATFORM_STYLE,
    _CHAT_THEME, _apply_highlights, _date_label,
    _format_time, _local_date, _render_message,
)

log = logging.getLogger(__name__)


# ── Drag handle ──────────────────────────────────────────

class DragHandle(Static):
    """Draggable divider between two panels. Adjusts the widget above it."""

    DEFAULT_CSS = """
    DragHandle {
        height: 1;
        background: #1a3a4a;
        color: #00d4ff;
        text-align: center;
        content-align: center middle;
    }
    DragHandle:hover {
        background: #205060;
        color: #00d4ff;
    }
    DragHandle.--dragging {
        background: #00d4ff;
        color: #0a0a0a;
    }
    """

    def __init__(self, target_id: str, min_height: int = 4, max_height: int = 30, **kwargs) -> None:
        super().__init__("━━━", **kwargs)
        self._target_id = target_id
        self._min_h = min_height
        self._max_h = max_height
        self._dragging = False
        self._drag_start_y: int = 0
        self._drag_start_h: int = 0

    def _find_target(self):
        try:
            return self.app.query_one(self._target_id)
        except Exception:
            return None

    def on_mouse_down(self, event) -> None:
        self._dragging = True
        self._drag_start_y = event.screen_y
        target = self._find_target()
        if target:
            # Use rendered size — styles.height may be "1fr" which has no .value
            try:
                h = target.styles.height
                self._drag_start_h = int(h.value) if h and h.unit == "cells" else target.size.height
            except Exception:
                self._drag_start_h = target.size.height
        else:
            self._drag_start_h = 10
        self.set_class(True, "--dragging")
        self.capture_mouse()
        event.stop()

    def on_mouse_up(self, event) -> None:
        if self._dragging:
            self._dragging = False
            self.set_class(False, "--dragging")
            self.release_mouse()
            event.stop()

    def on_mouse_move(self, event) -> None:
        if not self._dragging:
            return
        delta = event.screen_y - self._drag_start_y
        new_h = max(self._min_h, min(self._max_h, int(self._drag_start_h + delta)))
        target = self._find_target()
        if target:
            target.styles.height = new_h
        event.stop()


# ── Header bar ───────────────────────────────────────────

class TabButton(Static):
    """Clickable tab button in the header."""

    def on_click(self, event) -> None:
        event.stop()
        if self.id == "tab-messages":
            self.app.action_show_messages()
        elif self.id == "tab-schedule":
            self.app.action_show_schedule()


class _HeaderReloadButton(Static):
    """Reload button in the header bar."""

    def on_click(self, event) -> None:
        event.stop()
        self.app.action_reload()


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
            yield TabButton(" MESSAGES (Tab) ", id="tab-messages", classes="tab-btn --active")
            yield TabButton(" SCHEDULE (Tab) ", id="tab-schedule", classes="tab-btn")
            yield _HeaderReloadButton(" RELOAD (^⇧R) ", id="reload-btn")
            yield Static("", id="spacer", classes="header-spacer")
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


# ── Compose box ──────────────────────────────────────────

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


class _ActionButton(Static):
    """Generic clickable button that fires an app action."""

    def __init__(self, label: str, action: str, **kwargs) -> None:
        super().__init__(label, **kwargs)
        self._action = action

    def on_click(self, event) -> None:
        event.stop()
        getattr(self.app, self._action, lambda: None)()


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


# ── Conversation list ────────────────────────────────────

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
        super().__init__("+ NEW (^N)")

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
    def _render_lines(c: ConversationSummary, index: int = -1) -> tuple[str, str]:
        tag_label, tag_color = PLATFORM_STYLE.get(c.platform, (c.platform[:4], "#555555"))
        buf_prefix = f"[#555555]{index + 1}[/] " if 0 <= index < 9 else ""

        time_str = ""
        if c.last_time:
            today = _dt.date.today()
            local_time = c.last_time.astimezone() if c.last_time.tzinfo else c.last_time.replace(tzinfo=timezone.utc).astimezone()
            if local_time.date() == today:
                time_str = local_time.strftime("%-I:%M%p").lower()
            else:
                time_str = local_time.strftime("%b %-d")

        pin = "[#ff8c00]\u2605[/] " if c.pinned else ""
        mute = "[#555555]~[/] " if c.muted else ""

        name_line = (
            f"{buf_prefix}{pin}{mute}[{tag_color}]\\[{tag_label}][/] "
            f"{c.display_name}"
            f"  [#555555]{time_str}[/]"
        )

        prefix = "[#555555]you: [/]" if c.direction == "out" else ""
        preview = c.last_message[:30] if c.last_message else ""
        preview_line = f"     [#555555]{prefix}{preview}[/]"
        return name_line, preview_line

    def compose(self) -> ComposeResult:
        name_line, preview_line = self._render_lines(self._convo, self.index)
        yield Static(name_line, classes="conv-name")
        yield Static(preview_line, classes="conv-preview")

    def update_convo(self, convo: ConversationSummary, index: int) -> None:
        self._convo = convo
        self.index = index
        name_line, preview_line = self._render_lines(convo, index)
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
    """Scrollable list of conversations with arrow key navigation."""

    BORDER_TITLE = "\u25c8 NODES \u25c8"
    can_focus = True

    class Selected(TMessage):
        """Fired when a conversation is selected."""

        def __init__(self, contact_id: int, display_name: str, platform: str = "sms", phone: str = "", pinned: bool = False, muted: bool = False) -> None:
            super().__init__()
            self.contact_id = contact_id
            self.display_name = display_name
            self.platform = platform
            self.phone = phone
            self.pinned = pinned
            self.muted = muted

    class NewMessageRequested(TMessage):
        """Fired when the user wants to start a new conversation."""

    class ContextMenuRequested(TMessage):
        """Fired on right-click for context menu."""

        def __init__(self, contact_id: int, display_name: str, phone: str, message_count: int, pinned: bool = False, muted: bool = False) -> None:
            super().__init__()
            self.contact_id = contact_id
            self.display_name = display_name
            self.phone = phone
            self.message_count = message_count
            self.pinned = pinned
            self.muted = muted

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
        self.mount(NewMessageButton())

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
            self.post_message(self.Selected(c.contact_id, c.display_name, c.platform, c.phone or "", c.pinned, c.muted))

    def action_up(self) -> None:
        if self.selected_index > 0:
            self.selected_index -= 1

    def action_down(self) -> None:
        if self.selected_index < len(self._visible_conversations) - 1:
            self.selected_index += 1

    def on_key(self, event) -> None:
        if event.key == "up":
            self.action_up()
            event.stop()
        elif event.key == "down":
            self.action_down()
            event.stop()
        elif event.key == "enter":
            try:
                self.app.query_one("ComposeBox").show_input()
            except Exception:
                pass
            event.stop()

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
                c.contact_id, c.display_name, c.phone or "", 0, c.pinned, c.muted,
            ))

    @property
    def current(self) -> ConversationSummary | None:
        convos = self._visible_conversations
        if convos and 0 <= self.selected_index < len(convos):
            return convos[self.selected_index]
        return None


# ── Chat view ────────────────────────────────────────────

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
        self._search_active = False
        self._search_matches: list[int] = []
        self._search_idx: int = 0

    @property
    def message_count(self) -> int:
        return self._message_count

    def compose(self) -> ComposeResult:
        yield Input(placeholder="search messages...", id="chat-search")
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

    def toggle_search(self) -> None:
        self._search_active = not self._search_active
        try:
            search_input = self.query_one("#chat-search", Input)
            search_input.set_class(self._search_active, "--visible")
            if self._search_active:
                search_input.value = ""
                search_input.focus()
            else:
                self._search_matches = []
                self._search_idx = 0
                search_input.value = ""
        except Exception:
            pass

    def _do_search(self, query: str) -> None:
        self._search_matches = []
        self._search_idx = 0
        if not query:
            try:
                self.query_one("#chat-search", Input).placeholder = "search messages..."
            except Exception:
                pass
            return
        q = query.lower()
        try:
            area = self.query_one("#chat-area", TextArea)
            for i, line in enumerate(area.text.splitlines()):
                if q in line.lower():
                    self._search_matches.append(i)
        except Exception:
            pass
        self._update_search_placeholder()
        if self._search_matches:
            self._scroll_to_match()

    def _update_search_placeholder(self) -> None:
        try:
            search_input = self.query_one("#chat-search", Input)
            total = len(self._search_matches)
            if total:
                search_input.placeholder = f"{self._search_idx + 1}/{total} matches"
            else:
                search_input.placeholder = "no matches"
        except Exception:
            pass

    def _scroll_to_match(self) -> None:
        if not self._search_matches:
            return
        line = self._search_matches[self._search_idx]
        try:
            area = self.query_one("#chat-area", TextArea)
            area.move_cursor((line, 0))
            area.scroll_cursor_visible()
        except Exception:
            pass
        self._update_search_placeholder()

    def search_next(self) -> None:
        if not self._search_matches:
            return
        self._search_idx = (self._search_idx + 1) % len(self._search_matches)
        self._scroll_to_match()

    def search_prev(self) -> None:
        if not self._search_matches:
            return
        self._search_idx = (self._search_idx - 1) % len(self._search_matches)
        self._scroll_to_match()

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id == "chat-search":
            self._do_search(event.value.strip())

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id == "chat-search":
            self.search_next()

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


# ── Schedule view ────────────────────────────────────────

class _SchedCancelBtn(Static):
    """Clickable cancel button on a scheduled message row."""

    def __init__(self, sched_id: int, **kwargs) -> None:
        super().__init__(" x ", **kwargs)
        self.sched_id = sched_id

    def on_click(self, event) -> None:
        event.stop()
        with get_connection() as conn:
            cancel_scheduled_by_id(conn, self.sched_id)
        self.app.notify("Cancelled", severity="information")
        try:
            self.screen.query_one(ScheduleView).refresh_data()
        except Exception:
            pass


class _SchedRow(Horizontal):
    """A single scheduled message row."""

    def __init__(self, sched_id: int, text: str, **kwargs) -> None:
        super().__init__(**kwargs)
        self.sched_id = sched_id
        self._text = text

    def compose(self) -> ComposeResult:
        yield Static(self._text, classes="sched-row-text")
        yield _SchedCancelBtn(self.sched_id, classes="sched-cancel-btn")

    def on_click(self, event) -> None:
        # Select this row
        try:
            view = self.screen.query_one(ScheduleView)
            rows = list(view.query(".sched-row"))
            idx = rows.index(self)
            view._selected_idx = idx
        except Exception:
            pass


class ScheduleView(Widget):
    """Full-screen view for scheduled messages."""

    can_focus = True
    _selected_idx: reactive[int] = reactive(0)

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self._pending_ids: list[int] = []

    def compose(self) -> ComposeResult:
        yield Static("PENDING", id="sched-pending-title", classes="sched-section-title")
        yield VerticalScroll(id="sched-pending-list")
        yield Static("SENT", id="sched-sent-title", classes="sched-section-title sched-sent-header")
        yield VerticalScroll(id="sched-sent-list")
        yield Static(
            "[#50fa7b]\u2191\u2193[/] navigate   [#f75341]x[/] cancel   [#00d4ff]Tab[/] messages",
            id="sched-hints",
        )

    def on_mount(self) -> None:
        self.set_interval(10.0, self._tick)

    def _tick(self) -> None:
        if self.display:
            self.refresh_data()

    def on_key(self, event) -> None:
        if event.key == "up":
            if self._pending_ids and self._selected_idx > 0:
                self._selected_idx -= 1
            event.stop()
        elif event.key == "down":
            if self._pending_ids and self._selected_idx < len(self._pending_ids) - 1:
                self._selected_idx += 1
            event.stop()
        elif event.key == "delete" or event.key == "backspace":
            self.cancel_selected()
            event.stop()

    def refresh_data(self) -> None:
        """Reload scheduled messages from DB."""
        with get_connection() as conn:
            pending = get_all_scheduled(conn, include_done=False)
            done = get_all_scheduled(conn, include_done=True)
        done = [r for r in done if r["status"] in ("sent", "failed")]

        self._pending_ids: list[int] = []

        # Pending section
        pending_list = self.query_one("#sched-pending-list", VerticalScroll)
        pending_list.remove_children()
        title = self.query_one("#sched-pending-title", Static)
        title.update(f"PENDING ({len(pending)})")

        if not pending:
            pending_list.mount(Static("[#555555]No scheduled messages.[/]", classes="sched-empty"))
        else:
            now = datetime.now(timezone.utc)
            for r in pending:
                self._pending_ids.append(r["id"])
                name = r["display_name"] or r["phone"]
                body = r["body"]
                if len(body) > 50:
                    body = body[:50] + "..."
                try:
                    sched_dt = datetime.fromisoformat(r["scheduled_at"]).replace(tzinfo=timezone.utc)
                    delta = sched_dt - now
                    secs = max(0, int(delta.total_seconds()))
                    if secs >= 3600:
                        countdown = f"{secs // 3600}h {(secs % 3600) // 60}m"
                    elif secs >= 60:
                        countdown = f"{secs // 60}m"
                    else:
                        countdown = f"{secs}s"
                    local_dt = sched_dt.astimezone()
                    if local_dt.date() == datetime.now().date():
                        time_str = "Today " + local_dt.strftime("%-I:%M%p").lower()
                    elif local_dt.date() == (datetime.now() + _dt.timedelta(days=1)).date():
                        time_str = "Tomorrow " + local_dt.strftime("%-I:%M%p").lower()
                    else:
                        time_str = local_dt.strftime("%b %-d %-I:%M%p").lower()
                except Exception:
                    time_str = r["scheduled_at"]
                    countdown = ""

                row_text = (
                    f"[#ff8c00]{time_str:<22}[/]"
                    f"[#00d4ff bold]{name:<14}[/]"
                    f"{body}"
                )
                if countdown:
                    row_text += f"  [#555555]{countdown}[/]"

                row = _SchedRow(r["id"], row_text, classes="sched-row")
                pending_list.mount(row)

        # Clamp selection
        if self._pending_ids:
            self._selected_idx = min(self._selected_idx, len(self._pending_ids) - 1)
        else:
            self._selected_idx = 0
        self._highlight_selected()

        # Sent section
        sent_list = self.query_one("#sched-sent-list", VerticalScroll)
        sent_list.remove_children()
        sent_title = self.query_one("#sched-sent-title", Static)
        sent_title.update(f"SENT ({len(done)})")

        if not done:
            sent_list.mount(Static("[#555555]No history yet.[/]", classes="sched-empty"))
        else:
            for r in done:
                name = r["display_name"] or r["phone"]
                body = r["body"]
                if len(body) > 50:
                    body = body[:50] + "..."
                try:
                    sched_dt = datetime.fromisoformat(r["scheduled_at"]).replace(tzinfo=timezone.utc).astimezone()
                    time_str = sched_dt.strftime("%b %-d %-I:%M%p").lower()
                except Exception:
                    time_str = r["scheduled_at"]

                if r["status"] == "sent":
                    status_str = "[#50fa7b]sent[/]"
                else:
                    status_str = "[#f75341]failed[/]"

                row_text = (
                    f"[#444]{time_str:<22}[/]"
                    f"[#335]{name:<14}[/]"
                    f"[#444]{body}[/]"
                    f"  {status_str}"
                )
                sent_list.mount(Static(row_text, classes="sched-row-sent"))

    def _highlight_selected(self) -> None:
        rows = list(self.query(".sched-row"))
        for i, row in enumerate(rows):
            row.set_class(i == self._selected_idx, "--selected")

    def watch__selected_idx(self, value: int) -> None:
        self._highlight_selected()

    def cancel_selected(self) -> None:
        if not self._pending_ids:
            return
        sched_id = self._pending_ids[self._selected_idx]
        with get_connection() as conn:
            cancel_scheduled_by_id(conn, sched_id)
        self.app.notify("Cancelled", severity="information")
        self.refresh_data()
