"""Left panel — conversation list with DedSec styling."""

from __future__ import annotations

from textual.app import ComposeResult
from textual.containers import VerticalScroll
from textual.message import Message as TMessage
from textual.reactive import reactive
from textual.widget import Widget
from textual.widgets import Static

from ...db.models import ConversationSummary


PLATFORM_STYLE = {
    "imessage": ("imsg", "#68a8e4"),
    "sms":      ("sms",  "#50fa7b"),
    "bumble":   ("bmbl", "#ff2d6f"),
    "hinge":    ("hnge", "#ff2d6f"),
}


class NewMessageButton(Static):
    """Clickable '+ NEW MESSAGE' button at top of conversation list."""

    DEFAULT_CSS = """
    NewMessageButton {
        height: 2;
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
        super().__init__("[ + NEW MESSAGE ]")

    def on_click(self) -> None:
        for ancestor in self.ancestors:
            if isinstance(ancestor, ConversationList):
                ancestor.post_message(ConversationList.NewMessageRequested())
                break


class ConversationList(VerticalScroll):
    """Scrollable list of conversations."""

    BORDER_TITLE = "◈ NODES ◈"

    class Selected(TMessage):
        """Fired when a conversation is selected."""

        def __init__(self, contact_id: int, display_name: str, platform: str = "sms") -> None:
            super().__init__()
            self.contact_id = contact_id
            self.display_name = display_name
            self.platform = platform

    class NewMessageRequested(TMessage):
        """Fired when the user wants to start a new conversation."""

    selected_index: reactive[int] = reactive(0)

    def __init__(self) -> None:
        super().__init__()
        self._conversations: list[ConversationSummary] = []
        self.border_title = self.BORDER_TITLE

    def set_conversations(self, convos: list[ConversationSummary]) -> None:
        self._conversations = convos
        self._render_list()

    def _render_list(self) -> None:
        self.remove_children()
        self.mount(NewMessageButton())
        if not self._conversations:
            self.mount(Static("no nodes online", classes="empty-state"))
            return
        for i, c in enumerate(self._conversations):
            item = ConversationItem(c, i)
            self.mount(item)
        self._highlight()

    def _highlight(self) -> None:
        for child in self.children:
            if isinstance(child, ConversationItem):
                child.set_class(child.index == self.selected_index, "--highlight")

    def watch_selected_index(self) -> None:
        self._highlight()
        if self._conversations and 0 <= self.selected_index < len(self._conversations):
            c = self._conversations[self.selected_index]
            self.post_message(self.Selected(c.contact_id, c.display_name, c.platform))

    def action_up(self) -> None:
        if self.selected_index > 0:
            self.selected_index -= 1

    def action_down(self) -> None:
        if self.selected_index < len(self._conversations) - 1:
            self.selected_index += 1

    @property
    def current(self) -> ConversationSummary | None:
        if self._conversations and 0 <= self.selected_index < len(self._conversations):
            return self._conversations[self.selected_index]
        return None


class ConversationItem(Widget):
    """A single conversation row with DedSec styling."""

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
        tag_label, tag_color = PLATFORM_STYLE.get(c.platform, (c.platform, "#555555"))

        name_line = f"{c.display_name}  [{tag_color}]\\[{tag_label}][/]"

        prefix = "you: " if c.direction == "out" else ""
        preview = prefix + (c.last_message[:24] if c.last_message else "")

        yield Static(name_line, classes="conv-name")
        yield Static(f"  [#555555]{preview}[/]", classes="conv-preview")

    def on_click(self) -> None:
        for ancestor in self.ancestors:
            if isinstance(ancestor, ConversationList):
                ancestor.selected_index = self.index
                break
