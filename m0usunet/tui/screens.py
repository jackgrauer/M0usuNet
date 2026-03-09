"""All modal and full screens."""

from __future__ import annotations

import subprocess
import threading
from pathlib import Path

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.screen import ModalScreen, Screen
from textual.widgets import DirectoryTree, Input, OptionList, Static, TextArea
from textual.widgets.option_list import Option

from ..db import conversation_list, get_connection, search_contacts, upsert_contact

from .helpers import REPLY_PATH, _PHONE_RE
from .widgets import TabButton


# ── Confirm delete screen ────────────────────────────────

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


# ── Edit contact ─────────────────────────────────────────

class EditContactScreen(ModalScreen[str | None]):
    """Rename a contact."""

    DEFAULT_CSS = """
    EditContactScreen {
        align: center middle;
    }
    #edit-contact-box {
        width: 50;
        height: auto;
        background: #0d0d0d;
        border: heavy #00d4ff;
        padding: 1 2;
    }
    #edit-contact-title {
        color: #00d4ff;
        text-style: bold;
        text-align: center;
        margin-bottom: 1;
    }
    #edit-contact-phone {
        color: #555555;
        text-align: center;
        margin-bottom: 1;
    }
    #edit-contact-input {
        margin: 0 2;
    }
    #edit-contact-hint {
        color: #555555;
        text-align: center;
        margin-top: 1;
    }
    """

    BINDINGS = [("escape", "cancel", "Cancel")]

    def __init__(self, contact_id: int, current_name: str, phone: str) -> None:
        super().__init__()
        self._contact_id = contact_id
        self._current_name = current_name
        self._phone = phone

    def compose(self) -> ComposeResult:
        with Vertical(id="edit-contact-box"):
            yield Static("\u25c8 EDIT CONTACT \u25c8", id="edit-contact-title")
            yield Static(self._phone, id="edit-contact-phone")
            yield Input(
                value=self._current_name if not self._current_name.startswith("+") else "",
                placeholder="enter name...",
                id="edit-contact-input",
            )
            yield Static("Enter to save, Esc to cancel", id="edit-contact-hint")

    def on_mount(self) -> None:
        self.query_one("#edit-contact-input", Input).focus()

    def on_input_submitted(self, event: Input.Submitted) -> None:
        name = event.value.strip()
        if name:
            with get_connection() as conn:
                conn.execute(
                    "UPDATE contacts SET display_name = ? WHERE id = ?",
                    (name, self._contact_id),
                )
                conn.commit()
            self.dismiss(name)
        else:
            self.dismiss(None)

    def action_cancel(self) -> None:
        self.dismiss(None)


# ── Context menu ─────────────────────────────────────────

class _CtxOption(Static):
    """Clickable context menu item."""

    def __init__(self, label: str, action_id: str) -> None:
        super().__init__(label, classes="ctx-option")
        self._action_id = action_id

    def on_click(self, event) -> None:
        event.stop()
        self.screen.dismiss(self._action_id)


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
        ("m", "pick_mute", "Mute"),
    ]

    def __init__(self, contact_name: str, phone: str, message_count: int, pinned: bool = False, muted: bool = False) -> None:
        super().__init__()
        self._contact_name = contact_name
        self._phone = phone
        self._message_count = message_count
        self._pinned = pinned
        self._muted = muted

    def compose(self) -> ComposeResult:
        pin_label = "Unpin from top" if self._pinned else "Pin to top"
        mute_label = "Unmute" if self._muted else "Mute"
        with Vertical(id="ctx-box"):
            yield Static(f"\u25c8 {self._contact_name} \u25c8", id="ctx-title")
            if self._phone:
                yield Static(self._phone, id="ctx-phone")
            yield _CtxOption(f"[#ff8c00]p[/]  {pin_label}", "toggle_pin")
            yield _CtxOption(f"[#555555]m[/]  {mute_label}", "toggle_mute")
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

    def action_pick_mute(self) -> None:
        self.dismiss("toggle_mute")

    def action_cancel(self) -> None:
        self.dismiss(None)


# ── File picker ──────────────────────────────────────────

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


# ── New message screen ───────────────────────────────────

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


# ── Reply editor screen ─────────────────────────────────

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
        context_text = "\n".join(f"  {line}" for line in self._context_lines)
        yield Static(context_text, id="editor-context")
        yield TextArea("", id="editor-area")
        with Horizontal(id="claude-bar"):
            yield _ClaudeButton(" CLAUDE ", id="claude-btn")
            yield Input(placeholder="rewrite instruction...", id="claude-input")
        with Horizontal(id="editor-buttons"):
            yield _SendButton(" SEND (Enter) ", id="send-btn")
            yield _CancelButton(" CANCEL (^Q) ", id="cancel-btn")
            yield _ReloadButton(" RELOAD (^R) ", id="reload-btn")

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


# ── Quick switcher ────────────────────────────────────────

class QuickSwitchScreen(ModalScreen[int | None]):
    """Ctrl+K quick switcher for jumping between conversations."""

    DEFAULT_CSS = """
    QuickSwitchScreen {
        align: center middle;
    }
    #quick-switch-box {
        width: 50;
        max-height: 20;
        background: #0d0d0d;
        border: heavy #00d4ff;
        padding: 1 2;
    }
    #quick-switch-title {
        color: #00d4ff;
        text-style: bold;
        text-align: center;
        margin-bottom: 1;
    }
    #quick-switch-input {
        background: #111111;
        color: #e0e0e0;
        border: none;
        width: 100%;
    }
    #quick-switch-input:focus {
        border: none;
    }
    #quick-switch-results {
        height: auto;
        max-height: 10;
        background: #0a0a0a;
        color: #e0e0e0;
        margin-top: 1;
    }
    #quick-switch-results > .option-list--option-highlighted {
        background: #111a22;
        color: #00d4ff;
    }
    """

    BINDINGS = [("escape", "cancel", "Cancel")]

    def __init__(self) -> None:
        super().__init__()
        self._convos = []

    def compose(self) -> ComposeResult:
        with Vertical(id="quick-switch-box"):
            yield Static("\u25c8 QUICK SWITCH \u25c8", id="quick-switch-title")
            yield Input(placeholder="jump to conversation...", id="quick-switch-input")
            yield OptionList(id="quick-switch-results")

    def on_mount(self) -> None:
        with get_connection() as conn:
            self._convos = conversation_list(conn)
        self._populate("")
        self.query_one("#quick-switch-input", Input).focus()

    def _populate(self, query: str) -> None:
        option_list = self.query_one("#quick-switch-results", OptionList)
        option_list.clear_options()
        q = query.lower()
        for c in self._convos:
            if q and q not in c.display_name.lower():
                continue
            unread = f" [#f75341]({c.unread_count})[/]" if c.unread_count else ""
            label = f"{c.display_name}{unread}  [#555555]{c.phone or ''}[/]"
            option_list.add_option(Option(label, id=str(c.contact_id)))

    def on_input_changed(self, event: Input.Changed) -> None:
        if event.input.id == "quick-switch-input":
            self._populate(event.value.strip())

    def on_option_list_option_selected(self, event: OptionList.OptionSelected) -> None:
        self.dismiss(int(event.option.id))

    def action_cancel(self) -> None:
        self.dismiss(None)


# ── Help screen ──────────────────────────────────────────

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
        ("f1", "dismiss", "Close"),
    ]

    def compose(self) -> ComposeResult:
        with Vertical(id="help-box"):
            yield Static("\u25c8 KEYBINDINGS \u25c8", id="help-title")
            yield Static(
                "[#00d4ff bold]NAVIGATION[/]\n"
                "  [#50fa7b]\u2191 / \u2193[/]       move up / down in sidebar\n"
                "  [#50fa7b]Enter[/]       focus compose box\n"
                "  [#50fa7b]Tab[/]         toggle sidebar <-> compose\n"
                "  [#50fa7b]Esc[/]         close search / clear compose\n"
                "  [#50fa7b]Alt+1..9[/]    jump to conversation by number\n"
                "\n"
                "[#00d4ff bold]ACTIONS[/]\n"
                "  [#50fa7b]Ctrl+N[/]      new message\n"
                "  [#50fa7b]Ctrl+D[/]      delete conversation\n"
                "  [#50fa7b]Ctrl+F[/]      filter/search conversations\n"
                "  [#50fa7b]Ctrl+K[/]      quick switcher\n"
                "  [#50fa7b]Ctrl+R[/]      search within chat\n"
                "  [#50fa7b]Ctrl+B[/]      bare display (fullscreen chat)\n"
                "  [#50fa7b]Ctrl+G[/]      suggest reply (Claude)\n"
                "  [#50fa7b]Ctrl+Shift+R[/] reload (hot restart)\n"
                "  [#50fa7b]?[/]           this help screen\n"
                "\n"
                "[#00d4ff bold]COMPOSE[/]\n"
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
                "[#00d4ff bold]CHAT[/]\n"
                "  [#50fa7b]click+drag[/]  select text\n"
                "  [#50fa7b]shift+arrows[/] select with keyboard\n"
                "  [#50fa7b]Ctrl+C[/]      copy selection\n"
                "  [#50fa7b]Ctrl+A[/]      select all\n"
                "  [#50fa7b]Home/End[/]    scroll to top/bottom\n",
                id="help-body",
            )
            yield Static("press [#00d4ff]?[/] or [#00d4ff]Esc[/] to close", id="help-hint")
