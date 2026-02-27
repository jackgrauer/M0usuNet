"""Full-screen reply editor with CUA keybindings."""

from __future__ import annotations

import subprocess
import threading
from pathlib import Path

from textual.app import ComposeResult
from textual.binding import Binding
from textual.containers import Horizontal
from textual.screen import Screen
from textual.widgets import Input, Static, TextArea

from ..widgets.header_bar import TabButton


REPLY_PATH = Path.home() / ".mousunet" / "reply.txt"


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
            yield TabButton(" COMPOSE ", id="tab-editor", classes="tab-btn --active")
        context_text = "\n".join(f"  {line}" for line in self._context_lines)
        yield Static(context_text, id="editor-context")
        yield TextArea("", id="editor-area")
        with Horizontal(id="claude-bar"):
            yield ClaudeButton(" CLAUDE ", id="claude-btn")
            yield Input(placeholder="rewrite instruction...", id="claude-input")
        with Horizontal(id="editor-buttons"):
            yield SendButton(" SEND ", id="send-btn")
            yield CancelButton(" CANCEL ", id="cancel-btn")
            yield ReloadButton(" RELOAD ", id="reload-btn")

    def on_mount(self) -> None:
        self._write_file("")
        self._last_file_mtime = REPLY_PATH.stat().st_mtime if REPLY_PATH.exists() else 0
        self.query_one("#editor-area", TextArea).focus()
        # Poll file for external changes (Claude Code edits)
        self.set_interval(1.0, self._check_file_changed)

    def on_input_submitted(self, event: Input.Submitted) -> None:
        """Enter in claude-input triggers rewrite."""
        if event.input.id == "claude-input":
            self.action_claude_rewrite()

    def on_text_area_changed(self, event: TextArea.Changed) -> None:
        """Write to disk on every edit so Claude Code sees current text."""
        body = event.text_area.text
        self._write_file(body)

    def _write_file(self, body: str) -> None:
        """Write current state to disk so Claude Code can edit it."""
        REPLY_PATH.parent.mkdir(exist_ok=True)
        quoted = [f"# {line}" for line in self._context_lines]
        REPLY_PATH.write_text("\n".join(quoted) + "\n\n" + body)
        self._last_written_body = body
        self._last_file_mtime = REPLY_PATH.stat().st_mtime

    def _check_file_changed(self) -> None:
        """Poll for external edits to the reply file."""
        if not REPLY_PATH.exists():
            return
        mtime = REPLY_PATH.stat().st_mtime
        if mtime <= self._last_file_mtime:
            return
        # File changed externally — read it back
        body = self._read_file()
        if body == self._last_written_body:
            self._last_file_mtime = mtime
            return
        self._last_file_mtime = mtime
        self._last_written_body = body
        area = self.query_one("#editor-area", TextArea)
        area.load_text(body)

    def _read_file(self) -> str:
        """Read reply from disk, stripping comment lines."""
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
        """Reload from disk — use after Claude Code edits the file."""
        body = self._read_file()
        area = self.query_one("#editor-area", TextArea)
        area.load_text(body)

    def action_claude_rewrite(self) -> None:
        """Run ~/bin/rewrite with the instruction from the input."""
        inp = self.query_one("#claude-input", Input)
        instruction = inp.value.strip() or "rewrite more concisely"
        inp.value = ""
        # Run in background thread so UI doesn't freeze
        threading.Thread(
            target=self._run_rewrite, args=(instruction,), daemon=True
        ).start()

    def _run_rewrite(self, instruction: str) -> None:
        """Call ~/bin/rewrite in a thread. Auto-sync picks up the result."""
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


class ClaudeButton(Static):
    def on_click(self, event) -> None:
        event.stop()
        self.screen.action_claude_rewrite()


class SendButton(Static):
    def on_click(self, event) -> None:
        event.stop()
        self.screen.action_send()


class CancelButton(Static):
    def on_click(self, event) -> None:
        event.stop()
        self.screen.action_cancel()


class ReloadButton(Static):
    def on_click(self, event) -> None:
        event.stop()
        self.screen.action_reload()
