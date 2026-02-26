"""MousuNet TUI application — DedSec edition."""

from __future__ import annotations

import logging
from pathlib import Path

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal, Vertical
from textual.widgets import Input

from ..db.connection import ensure_schema, get_connection
from ..db.contacts import get_contact
from ..db.messages import (
    add_message, conversation_list, delete_messages_for_contact, get_messages,
)
from ..db.models import Message
from ..exceptions import RelayError
from ..relay.send import send_message

from .screens import ConfirmDeleteScreen, NewMessageScreen
from .widgets.conversation_list import ConversationList
from .widgets.chat_view import ChatView
from .widgets.compose_box import ComposeBox
from .widgets.header_bar import HeaderBar
from .clipboard import copy_osc52

log = logging.getLogger(__name__)

CSS_PATH = Path(__file__).parent / "sorbet.tcss"


class MousuNetApp(App):
    """Unified messaging TUI."""

    TITLE = "mousunet"
    CSS_PATH = CSS_PATH

    BINDINGS = [
        Binding("k", "conv_up", "Up", show=False),
        Binding("j", "conv_down", "Down", show=False),
        Binding("up", "conv_up", "Up", show=False),
        Binding("down", "conv_down", "Down", show=False),
        Binding("tab", "toggle_focus", "Toggle focus", show=False),
        Binding("escape", "escape", "Escape", show=False),
        Binding("n", "new_message", "New message", show=False),
        Binding("d", "delete_conversation", "Delete", show=False),
        Binding("y", "copy_last", "Yank", show=False),
        Binding("q", "quit", "Quit", show=False),
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

        # Skip rebuild if nothing changed
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
        chat.set_messages(msgs, self._current_contact_name, self._current_platform)

    def on_compose_box_submitted(self, event: ComposeBox.Submitted) -> None:
        if self._current_contact_id is None:
            return

        body = event.body
        contact_name = self._current_contact_name
        contact_id = self._current_contact_id
        compose = self.query_one(ComposeBox)

        try:
            output = send_message(contact_name, body)
            success = True
        except RelayError as e:
            output = str(e)
            success = False

        # Detect platform from relay output
        platform = "sms"
        if "imessage" in output.lower() or "imsg" in output.lower():
            platform = "imessage"

        with get_connection() as conn:
            mid = add_message(
                conn,
                contact_id,
                platform,
                "out",
                body,
                delivered=success,
                relay_output=output,
            )
            msg = Message(
                id=mid,
                contact_id=contact_id,
                platform=platform,
                direction="out",
                body=body,
                delivered=success,
                relay_output=output,
            )

        chat = self.query_one(ChatView)
        chat.append_message(msg)

        if success:
            compose.show_status(f"◉ RELAY OK  {output}")
        else:
            compose.show_status(f"◉ RELAY FAIL  {output}", error=True)

    def action_copy_last(self) -> None:
        """Copy last message body to clipboard via OSC 52."""
        focused = self.focused
        if focused and focused.id == "compose-input":
            # If in compose input, copy the input value
            inp = self.query_one("#compose-input", Input)
            if inp.value:
                copy_osc52(inp.value)
                self.query_one(ComposeBox).show_status("copied to clipboard")
            return
        chat = self.query_one(ChatView)
        body = chat.get_last_message_body()
        if body:
            copy_osc52(body)
            self.query_one(ComposeBox).show_status("copied to clipboard")

    def action_select_all(self) -> None:
        """Select all text in compose input."""
        try:
            inp = self.query_one("#compose-input", Input)
            inp.selection = inp.Selection(0, len(inp.value))
            inp.focus()
        except Exception:
            pass

    def action_conv_up(self) -> None:
        focused = self.focused
        if focused and focused.id == "compose-input":
            return
        self.query_one(ConversationList).action_up()

    def action_conv_down(self) -> None:
        focused = self.focused
        if focused and focused.id == "compose-input":
            return
        self.query_one(ConversationList).action_down()

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
