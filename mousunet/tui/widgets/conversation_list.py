"""Left panel — conversation list with search, vim nav, unread counts."""

from __future__ import annotations

from textual.app import ComposeResult
from textual.containers import VerticalScroll
from textual.message import Message as TMessage
from textual.reactive import reactive
from textual.widget import Widget
from textual.widgets import Input, Static

from ...db.models import ConversationSummary


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


class ConversationList(VerticalScroll):
    """Scrollable list of conversations with search and vim nav."""

    BORDER_TITLE = "\u25c8 NODES \u25c8"

    class Selected(TMessage):
        """Fired when a conversation is selected."""

        def __init__(self, contact_id: int, display_name: str, platform: str = "sms", phone: str = "") -> None:
            super().__init__()
            self.contact_id = contact_id
            self.display_name = display_name
            self.platform = platform
            self.phone = phone

    class NewMessageRequested(TMessage):
        """Fired when the user wants to start a new conversation."""

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
        self.remove_children()

        # Search bar
        search = SearchBar()
        if self._search_active:
            search.add_class("--visible")
        self.mount(search)
        self.mount(NewMessageButton())

        convos = self._visible_conversations
        if not convos:
            if self._search_query:
                self.mount(Static(f"[#555555]no matches for '{self._search_query}'[/]", classes="empty-state"))
            else:
                self.mount(Static("[#555555]no nodes online[/]", classes="empty-state"))
            return

        total = len(self._conversations)
        showing = len(convos)
        if self._search_query:
            self.border_title = f"\u25c8 NODES ({showing}/{total}) \u25c8"
        else:
            self.border_title = f"\u25c8 NODES ({total}) \u25c8"

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
            self.post_message(self.Selected(c.contact_id, c.display_name, c.platform, c.phone or ""))

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
            old_idx = self.selected_index
            self._apply_filter()
            self._render_list()
            # Reset selection to 0 when filter changes
            if self.selected_index >= len(self._visible_conversations):
                self.selected_index = 0

    def on_input_submitted(self, event: Input.Submitted) -> None:
        if event.input.id == "conv-search":
            # Close search, keep filter active, focus list
            self._search_active = False
            self._render_list()
            self.focus()

    @property
    def current(self) -> ConversationSummary | None:
        convos = self._visible_conversations
        if convos and 0 <= self.selected_index < len(convos):
            return convos[self.selected_index]
        return None


class ConversationItem(Widget):
    """A single conversation row with platform badge and preview."""

    DEFAULT_CSS = """
    ConversationItem {
        height: 2;
        padding: 0 1;
    }
    ConversationItem.--highlight {
        background: #111a22;
    }
    """

    def __init__(self, convo: ConversationSummary, index: int) -> None:
        super().__init__()
        self._convo = convo
        self.index = index

    def compose(self) -> ComposeResult:
        c = self._convo
        tag_label, tag_color = PLATFORM_STYLE.get(c.platform, (c.platform[:4], "#555555"))

        # Timestamp
        time_str = ""
        if c.last_time:
            today = __import__("datetime").date.today()
            if c.last_time.date() == today:
                time_str = c.last_time.strftime("%-I:%M%p").lower()
            else:
                time_str = c.last_time.strftime("%b %-d")

        # Unread badge
        unread = ""
        if c.unread_count > 0:
            unread = f" [bold #00d4ff]({c.unread_count})[/]"

        name_line = (
            f"[{tag_color}]\\[{tag_label}][/] "
            f"{c.display_name}{unread}"
            f"  [#555555]{time_str}[/]"
        )

        prefix = "[#555555]you: [/]" if c.direction == "out" else ""
        preview = c.last_message[:30] if c.last_message else ""

        yield Static(name_line, classes="conv-name")
        yield Static(f"     [#555555]{prefix}{preview}[/]", classes="conv-preview")

    def on_click(self) -> None:
        for ancestor in self.ancestors:
            if isinstance(ancestor, ConversationList):
                ancestor.selected_index = self.index
                break
