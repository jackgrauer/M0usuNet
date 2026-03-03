"""MousuNet TUI application — DedSec edition."""

from __future__ import annotations

import logging
import threading
from pathlib import Path

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.widgets import Input

from ..db.connection import ensure_schema, get_connection
from ..db.contacts import get_contact
from ..db.messages import (
    add_message, conversation_list, delete_messages_for_contact, get_messages,
)
from ..db.models import Message
from ..exceptions import RelayError
from ..relay.send import send_message

from .screens import ConfirmDeleteScreen, NewMessageScreen, ReplyEditorScreen
from .widgets.conversation_list import ConversationList
from .widgets.chat_view import ChatView
from .widgets.compose_box import ComposeBox
from .widgets.header_bar import HeaderBar

log = logging.getLogger(__name__)

CSS_PATH = Path(__file__).parent / "sorbet.tcss"


class MousuNetApp(App):
    """Unified messaging TUI."""

    TITLE = "mousunet"
    CSS_PATH = CSS_PATH

    BINDINGS = [
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
        Binding("question_mark", "show_help", "Help", show=False),
        Binding("ctrl+g", "suggest_reply", "Suggest", show=False),
    ]

    def __init__(self) -> None:
        super().__init__()
        self._current_contact_id: int | None = None
        self._current_contact_name: str = ""
        self._current_platform: str = ""
        self._current_phone: str = ""
        self._ingest_thread = None
        self._initial_select_done = False
        self._last_msg_count: int = 0
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
        # Start ingest poller as background thread
        try:
            from ..ingest.poller import start_background
            self._ingest_thread = start_background(interval=30)
        except Exception as e:
            log.info("Ingest poller not started (normal on Mac): %s", e)

    def _refresh_conversations(self) -> None:
        with get_connection() as conn:
            convos = conversation_list(conn)
            current_msg_count = 0
            if self._current_contact_id is not None:
                row = conn.execute(
                    "SELECT COUNT(*) AS cnt FROM messages WHERE contact_id = ?",
                    (self._current_contact_id,),
                ).fetchone()
                current_msg_count = row["cnt"] if row else 0

        # Reload active chat if message count changed
        if self._current_contact_id is not None:
            prev = self._last_msg_count
            if current_msg_count != prev:
                self._last_msg_count = current_msg_count
                self._load_chat()

        # Skip sidebar rebuild if nothing changed
        conv_list = self.query_one(ConversationList)
        new_ids = [(c.contact_id, c.last_message) for c in convos]
        if hasattr(self, "_last_conv_ids") and self._last_conv_ids == new_ids:
            return
        self._last_conv_ids = new_ids

        conv_list.set_conversations(convos)

        # Auto-select first conversation on initial load
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
        self._last_msg_count = 0
        self._load_chat()
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
            phone=self._current_phone,
        )

    # ── Navigation ─────────────────────────────────────────

    def _in_text_input(self) -> bool:
        """True if focus is in a text input (don't intercept single-key bindings)."""
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
            self.query_one("#compose-input", Input).focus()
        except Exception:
            pass

    def action_new_message(self) -> None:
        if self._in_text_input():
            return
        self.on_conversation_list_new_message_requested()

    def action_show_help(self) -> None:
        if self._in_text_input():
            return
        self.push_screen(HelpScreen())

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
        header = self.query_one(HeaderBar)
        for tab_id in ("tab-messages", "tab-editor"):
            try:
                btn = header.query_one(f"#{tab_id}")
                btn.set_class(tab_id == f"tab-{active}", "--active")
            except Exception:
                pass

    # ── Compose / send ────────────────────────────────────

    def on_compose_box_submitted(self, event: ComposeBox.Submitted) -> None:
        if self._current_contact_id is None:
            return
        self._send_body(event.body)

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

    def _send_body(self, body: str) -> None:
        if self._current_contact_id is None:
            return
        contact_name = self._current_contact_name
        contact_id = self._current_contact_id
        compose = self.query_one(ComposeBox)

        try:
            output = send_message(contact_name, body)
            success = True
        except RelayError as e:
            output = str(e)
            success = False

        platform = "sms"
        if "imessage" in output.lower() or "imsg" in output.lower():
            platform = "imessage"

        with get_connection() as conn:
            mid = add_message(
                conn, contact_id, platform, "out", body,
                delivered=success, relay_output=output,
            )
            msg = Message(
                id=mid, contact_id=contact_id, platform=platform,
                direction="out", body=body, delivered=success,
                relay_output=output,
            )

        chat = self.query_one(ChatView)
        chat.append_message(msg)

        if success:
            compose.show_status(f"\u25c9 RELAY OK  {output}")
        else:
            compose.show_status(f"\u25c9 RELAY FAIL  {output}", error=True)

    def action_toggle_focus(self) -> None:
        focused = self.focused
        if focused and focused.id == "compose-input":
            self.query_one(ConversationList).focus()
        else:
            try:
                self.query_one("#compose-input", Input).focus()
            except Exception:
                pass

    # ── Claude suggestion ───────────────────────────────

    def action_suggest_reply(self) -> None:
        """Ctrl+G: generate a reply suggestion via Claude."""
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
        from ..suggest import suggest_reply
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
        compose.show_status("\u25c9 suggestion loaded — edit or hit Enter to send")

    def action_escape(self) -> None:
        # Close search if active
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


# ── Help overlay ──────────────────────────────────────────

from textual.screen import ModalScreen
from textual.containers import Vertical


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
                "  [#50fa7b]/[/]           search/filter conversations\n"
                "  [#50fa7b]?[/]           this help screen\n"
                "\n"
                "[#00d4ff bold]COMPOSE[/]\n"
                "  [#50fa7b]ctrl+g[/]      suggest reply (Claude)\n"
                "  [#50fa7b]Enter[/]       send message\n"
                "  [#50fa7b]REPLY btn[/]   open full editor\n"
                "  [#50fa7b]CLAUDE btn[/]  AI rewrite in editor\n"
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
