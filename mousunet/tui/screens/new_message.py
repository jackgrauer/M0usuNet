"""New message modal — contact autocomplete with DedSec styling."""

from __future__ import annotations

import re

from textual.app import ComposeResult
from textual.containers import Vertical
from textual.screen import ModalScreen
from textual.widgets import Input, OptionList, Static
from textual.widgets.option_list import Option

from ...db.connection import get_connection
from ...db.contacts import search_contacts, upsert_contact


_PHONE_RE = re.compile(r"^\+?\d[\d\s\-]{6,}$")


class NewMessageScreen(ModalScreen[int | None]):
    """Autocomplete contact picker. Dismisses with contact_id or None."""

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
            yield Static("◈ NEW MESSAGE ◈", id="new-msg-title")
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
