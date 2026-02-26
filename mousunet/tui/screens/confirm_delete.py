"""Delete confirmation modal — red DedSec styling."""

from __future__ import annotations

from textual.app import ComposeResult
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import Static


class ConfirmDeleteScreen(ModalScreen[bool]):
    """Confirm conversation deletion. Dismisses with True/False."""

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
            yield Static("◈ DELETE CONVERSATION ◈", id="delete-title")
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
