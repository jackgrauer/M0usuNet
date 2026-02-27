"""MousuNet TUI application — DedSec edition."""

from __future__ import annotations

import logging
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
        Binding("tab", "toggle_focus", "Toggle focus", show=False),
        Binding("escape", "escape", "Escape", show=False),
        Binding("d", "delete_conversation", "Delete", show=False),
    ]

    def __init__(self) -> None:
        super().__init__()
        self._current_contact_id: int | None = None
        self._current_contact_name: str = ""
        self._current_platform: str = ""
        self._ingest_thread = None
        self._initial_select_done = False

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
            # Check if active chat has new messages
            current_msg_count = 0
            if self._current_contact_id is not None:
                row = conn.execute(
                    "SELECT COUNT(*) AS cnt FROM messages WHERE contact_id = ?",
                    (self._current_contact_id,),
                ).fetchone()
                current_msg_count = row["cnt"] if row else 0

        # Reload active chat if message count changed
        if self._current_contact_id is not None:
            prev = getattr(self, "_last_msg_count", 0)
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
        self._last_msg_count = 0  # reset so _load_chat runs fresh
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
            phone=getattr(self, "_current_phone", ""),
        )

    # ── New Message modal ──────────────────────────────────

    def on_conversation_list_new_message_requested(self) -> None:
        self.push_screen(NewMessageScreen(), callback=self._on_new_message_done)

    def _on_new_message_done(self, contact_id: int | None) -> None:
        if contact_id is None:
            return
        # Refresh sidebar so new/existing contact appears
        self._last_conv_ids = None  # force rebuild
        self._refresh_conversations()
        # Try to select the contact in the sidebar
        conv_list = self.query_one(ConversationList)
        for i, c in enumerate(conv_list._conversations):
            if c.contact_id == contact_id:
                conv_list.selected_index = i
                return
        # Contact exists but has no messages yet — load empty chat directly
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
        self._last_conv_ids = None  # force sidebar rebuild
        self._refresh_conversations()
        chat = self.query_one(ChatView)
        chat.set_messages([], "", "")

    # ── Tab styling helper ────────────────────────────────

    def _update_tabs(self, active: str) -> None:
        """Toggle --active class on the main HeaderBar's tab buttons."""
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
        """Open reply editor screen with message context."""
        # Don't stack multiple editor screens
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
        """Pop back to the main messages screen."""
        if isinstance(self.screen, ReplyEditorScreen):
            self.screen.dismiss("")
        self._update_tabs("messages")

    def _on_reply_editor_done(self, result: str) -> None:
        """Called when reply editor screen is dismissed."""
        self._update_tabs("messages")
        if result:
            self._send_body(result)

    def _send_body(self, body: str) -> None:
        """Send a message body to the current contact."""
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
            compose.show_status(f"◉ RELAY OK  {output}")
        else:
            compose.show_status(f"◉ RELAY FAIL  {output}", error=True)

    def action_toggle_focus(self) -> None:
        """Toggle focus between compose input and conversation list."""
        focused = self.focused
        if focused and focused.id == "compose-input":
            self.query_one(ConversationList).focus()
        else:
            try:
                self.query_one("#compose-input", Input).focus()
            except Exception:
                pass

    def action_escape(self) -> None:
        """Clear compose input and refocus sidebar."""
        try:
            inp = self.query_one("#compose-input", Input)
            inp.value = ""
        except Exception:
            pass
        self.query_one(ConversationList).focus()
