"""Bottom panel — message input with DedSec styling."""

from __future__ import annotations

from textual.app import ComposeResult
from textual.containers import Horizontal
from textual.message import Message as TMessage
from textual.widget import Widget
from textual.widgets import Input, Static


class ReplyButton(Static):
    """Clickable button that opens the reply editor."""

    def on_click(self, event) -> None:
        event.stop()
        self.app.action_reply_editor()


class ComposeBox(Widget):
    """Reply input that fires a Submitted message."""

    class Submitted(TMessage):
        """User pressed Enter with a message."""

        def __init__(self, body: str) -> None:
            super().__init__()
            self.body = body

    def __init__(self) -> None:
        super().__init__()
        self._placeholder = "select a node..."
        self._status = Static("", id="relay-status")

    def compose(self) -> ComposeResult:
        with Horizontal():
            yield ReplyButton(" REPLY ", id="reply-btn")
            yield Static(" ▸ ", id="compose-prompt")
            yield Input(placeholder=self._placeholder, id="compose-input")
        yield self._status

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

    def on_input_submitted(self, event: Input.Submitted) -> None:
        body = event.value.strip()
        if not body:
            return
        event.input.value = ""
        self.post_message(self.Submitted(body))
