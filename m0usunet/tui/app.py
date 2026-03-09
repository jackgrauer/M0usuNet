"""M0usuNetApp — the Textual App subclass."""

from __future__ import annotations

import base64
import json as _json
import logging
import os
import subprocess
import threading
from pathlib import Path

from textual.app import App, ComposeResult
from textual.binding import Binding
from textual.widgets import Input, TextArea

from ..db import (
    ConversationSummary, Message,
    add_message, add_scheduled_message, cancel_scheduled,
    conversation_list, delete_messages_for_contact,
    ensure_schema, get_connection, get_contact,
    get_messages, get_scheduled_for_contact,
    mark_viewed, toggle_pin,
)
from ..exceptions import RelayError
from ..relay import send_message

from .clipboard import copy_osc52
from .helpers import _parse_schedule_time
from .screens import (
    ConfirmDeleteScreen, ConversationContextMenu,
    EditContactScreen, FilePickerScreen, HelpScreen,
    NewMessageScreen, ReplyEditorScreen,
)
from .widgets import (
    ChatView, ComposeBox, ConversationList, DragHandle,
    HeaderBar, ScheduleView,
)

log = logging.getLogger(__name__)

CSS_PATH = Path(__file__).parent / "sorbet.tcss"


class M0usuNetApp(App):
    """Unified messaging TUI."""

    TITLE = "m0usunet"
    CSS_PATH = CSS_PATH

    BINDINGS = [
        Binding("ctrl+c", "copy_selection", "Copy", show=False),
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
        Binding("m", "context_menu", "Menu", show=False),
        Binding("question_mark", "show_help", "Help", show=False),
        Binding("ctrl+g", "suggest_reply", "Suggest", show=False),
        Binding("v", "view_attachment", "View", show=False),
        Binding("ctrl+r", "reload", "Reload", show=False),
    ]

    def __init__(self) -> None:
        super().__init__()
        self._current_contact_id: int | None = None
        self._current_contact_name: str = ""
        self._current_platform: str = ""
        self._current_phone: str = ""
        self._current_pinned: bool = False
        self._ingest_thread = None
        self._initial_select_done = False
        self._last_msg_count: int = 0
        self._prev_total_unread: int = 0
        self._suggesting = False

    def compose(self) -> ComposeResult:
        yield HeaderBar()
        yield ConversationList()
        yield DragHandle("ConversationList", min_height=4, max_height=30, id="drag-conv")
        yield ChatView()
        yield DragHandle("ChatView", min_height=4, max_height=50, id="drag-chat")
        yield ComposeBox()
        yield ScheduleView(id="schedule-view")

    def on_mount(self) -> None:
        ensure_schema()
        self._refresh_conversations()
        self.set_interval(5.0, self._refresh_conversations)
        try:
            from ..ingest import start_background
            self._ingest_thread = start_background(interval=30)
        except Exception as e:
            log.info("Ingest poller not started (normal on Mac): %s", e)
        try:
            from ..scheduler import set_on_sent, start_background as start_scheduler
            set_on_sent(self._on_scheduled_sent)
            self._scheduler_thread = start_scheduler(interval=30)
        except Exception as e:
            log.info("Scheduler not started: %s", e)

    def _on_scheduled_sent(self, contact_name: str, body: str) -> None:
        """Called from scheduler thread when a message fires."""
        preview = body[:30] + "..." if len(body) > 30 else body
        self.call_from_thread(
            self.notify, f"Sent to {contact_name}: {preview}", severity="information"
        )

    def _refresh_conversations(self) -> None:
        # Skip refresh if a screen is pushed on top (e.g. NewMessageScreen)
        if len(self.screen_stack) > 1:
            return

        with get_connection() as conn:
            convos = conversation_list(conn)

            current_msg_count = 0
            if self._current_contact_id is not None:
                row = conn.execute(
                    "SELECT COUNT(*) AS cnt FROM messages WHERE contact_id = ?",
                    (self._current_contact_id,),
                ).fetchone()
                current_msg_count = row["cnt"] if row else 0

                if current_msg_count != self._last_msg_count:
                    self._last_msg_count = current_msg_count
                    self._load_chat()

        try:
            conv_list = self.query_one(ConversationList)
        except Exception:
            return
        new_ids = [(c.contact_id, c.last_message) for c in convos]
        if hasattr(self, "_last_conv_ids") and self._last_conv_ids == new_ids:
            return
        self._last_conv_ids = new_ids

        conv_list.set_conversations(convos)

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
        self._current_pinned = event.pinned
        self._last_msg_count = 0
        self._load_chat()
        with get_connection() as conn:
            mark_viewed(conn, event.contact_id)
        compose = self.query_one(ComposeBox)
        compose.set_contact_name(event.display_name)
        compose.clear_status()
        compose.show_input()

    def _load_chat(self) -> None:
        if self._current_contact_id is None:
            return
        with get_connection() as conn:
            msgs = get_messages(conn, self._current_contact_id)
        chat = self.query_one(ChatView)
        chat.set_messages(
            msgs, self._current_contact_name, self._current_platform,
            phone=self._current_phone, contact_id=self._current_contact_id,
        )

    # ── Navigation ─────────────────────────────────────────

    def _in_text_input(self) -> bool:
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
            self.query_one(ComposeBox).show_input()
        except Exception:
            pass

    def action_new_message(self) -> None:
        if self._in_text_input():
            return
        self.on_conversation_list_new_message_requested()

    def action_context_menu(self) -> None:
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
            ConversationContextMenu(
                self._current_contact_name, self._current_phone, count,
                pinned=self._current_pinned,
            ),
            callback=self._on_context_menu_done,
        )

    def on_conversation_list_context_menu_requested(
        self, event: ConversationList.ContextMenuRequested,
    ) -> None:
        self._current_contact_id = event.contact_id
        self._current_contact_name = event.display_name
        self._current_phone = event.phone
        self._current_pinned = event.pinned
        self.push_screen(
            ConversationContextMenu(
                event.display_name, event.phone, event.message_count,
                pinned=event.pinned,
            ),
            callback=self._on_context_menu_done,
        )

    def _on_context_menu_done(self, result: str | None) -> None:
        if result is None:
            return
        if result == "delete":
            self.action_delete_conversation()
        elif result == "copy_phone":
            if self._current_phone:
                copy_osc52(self._current_phone)
                self.notify(f"Copied: {self._current_phone}")
            else:
                self.notify("No phone number", severity="warning")
        elif result == "mark_read":
            if self._current_contact_id is not None:
                with get_connection() as conn:
                    mark_viewed(conn, self._current_contact_id)
                self._last_conv_ids = None
                self._refresh_conversations()
                self.notify("Marked as read")
        elif result == "toggle_pin":
            if self._current_contact_id is not None:
                with get_connection() as conn:
                    now_pinned = toggle_pin(conn, self._current_contact_id)
                self._current_pinned = now_pinned
                self._last_conv_ids = None
                self._refresh_conversations()
                self.notify("Pinned" if now_pinned else "Unpinned")

    def action_show_help(self) -> None:
        if self._in_text_input():
            return
        self.push_screen(HelpScreen())

    def action_reload(self) -> None:
        """Hot reload — exit with special code so wrapper restarts the app."""
        self._reload_requested = True
        self.exit()

    # ── Attachment viewer ──────────────────────────────────

    def action_attach_file(self) -> None:
        if self._current_contact_id is None:
            return
        self.push_screen(FilePickerScreen("~"), callback=self._on_file_picked)

    def _on_file_picked(self, path: str | None) -> None:
        if not path:
            return
        try:
            compose = self.query_one(ComposeBox)
            compose.set_attachment(path)
            compose.show_input()
        except Exception:
            pass

    def action_view_attachment(self) -> None:
        if self._in_text_input():
            return
        try:
            chat = self.query_one(ChatView)
        except Exception:
            return
        att = chat.get_cursor_attachment()
        if not att:
            return
        local = att.local_path
        if not local or att.download_status != "done":
            return
        mime = att.mime_type or ""
        if mime.startswith("image/"):
            self._view_image(local)
        else:
            self._view_file(local, att.filename)

    def _view_image(self, path: str) -> None:
        """Suspend TUI and display image with kitten icat."""
        import shutil
        viewer = shutil.which("kitten")
        if not viewer:
            return
        with self.suspend():
            subprocess.run([viewer, "icat", "--hold", path])

    def _view_file(self, path: str, filename: str) -> None:
        """Suspend TUI and display file info / text content."""
        with self.suspend():
            print(f"\n  File: {filename}")
            print(f"  Path: {path}")
            mime = ""
            try:
                r = subprocess.run(["file", "--mime-type", "-b", path],
                                   capture_output=True, text=True, timeout=5)
                mime = r.stdout.strip()
                print(f"  Type: {mime}")
            except Exception:
                pass
            if mime.startswith("text/"):
                print()
                try:
                    with open(path) as f:
                        print(f.read()[:2000])
                except Exception:
                    pass
            print("\n  Press Enter to return...")
            input()

    # ── New Message modal ──────────────────────────────────

    def on_conversation_list_new_message_requested(self) -> None:
        self.push_screen(NewMessageScreen(), callback=self._on_new_message_done)

    def _on_new_message_done(self, contact_id: int | None) -> None:
        if contact_id is None:
            return
        with get_connection() as conn:
            contact = get_contact(conn, contact_id)
        if not contact:
            return

        # Force refresh and try to find them in the list
        self._last_conv_ids = None
        self._refresh_conversations()
        conv_list = self.query_one(ConversationList)
        found = False
        for i, c in enumerate(conv_list._visible_conversations):
            if c.contact_id == contact_id:
                conv_list.selected_index = i
                found = True
                break

        if not found:
            # Contact has no messages yet — inject a placeholder entry
            placeholder = ConversationSummary(
                contact_id=contact_id,
                display_name=contact.display_name,
                phone=contact.phone or "",
                platform="",
                last_message="",
                last_time=None,
                direction="out",
                pinned=False,
                unread_count=0,
            )
            convos = [placeholder] + conv_list._conversations
            conv_list.set_conversations(convos)
            conv_list.selected_index = 0

        # Set current contact and show compose
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
        compose.show_input()

    # ── Delete Conversation modal ─────────────────────────

    def action_edit_contact(self) -> None:
        if self._current_contact_id is None:
            return
        self.push_screen(
            EditContactScreen(
                self._current_contact_id,
                self._current_contact_name,
                self._current_phone,
            ),
            callback=self._on_edit_contact_done,
        )

    def _on_edit_contact_done(self, new_name: str | None) -> None:
        if new_name:
            self._current_contact_name = new_name
            self._last_conv_ids = None
            self._refresh_conversations()
            self._load_chat()
            compose = self.query_one(ComposeBox)
            compose.set_contact_name(new_name)

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

    _active_tab: str = "messages"

    def _update_tabs(self, active: str) -> None:
        self._active_tab = active
        try:
            header = self.query_one(HeaderBar)
        except Exception:
            return
        for tab_id in ("tab-messages", "tab-schedule"):
            try:
                btn = header.query_one(f"#{tab_id}")
                btn.set_class(tab_id == f"tab-{active}", "--active")
            except Exception:
                pass
        # Toggle visibility of views
        msg_visible = active == "messages"
        try:
            self.query_one(ConversationList).display = msg_visible
            self.query_one("#drag-conv").display = msg_visible
            self.query_one(ChatView).display = msg_visible
            self.query_one("#drag-chat").display = msg_visible
            self.query_one(ComposeBox).display = msg_visible
        except Exception:
            pass
        try:
            sched = self.query_one(ScheduleView)
            sched.display = active == "schedule"
            if active == "schedule":
                sched.refresh_data()
                sched.focus()
        except Exception:
            pass

    # ── Compose / send ────────────────────────────────────

    def on_compose_box_submitted(self, event: ComposeBox.Submitted) -> None:
        if self._current_contact_id is None:
            return
        body = event.body

        # Handle /cancel command
        if body.lower() in ("/cancel", "/cancel all"):
            cancel_all = body.lower() == "/cancel all"
            with get_connection() as conn:
                count = cancel_scheduled(conn, self._current_contact_id, cancel_all=cancel_all)
            compose = self.query_one(ComposeBox)
            if count:
                compose.show_status(f"\u25c9 cancelled {count} scheduled message{'s' if count != 1 else ''}")
            else:
                compose.show_status("\u25c9 no pending scheduled messages", error=True)
            self._load_chat()
            return

        # Handle /scheduled command
        if body.lower() == "/scheduled":
            with get_connection() as conn:
                scheduled = get_scheduled_for_contact(conn, self._current_contact_id)
            compose = self.query_one(ComposeBox)
            if scheduled:
                info = "; ".join(f"{s.scheduled_at}: {s.body[:30]}" for s in scheduled)
                compose.show_status(f"\u25c9 {len(scheduled)} pending: {info}")
            else:
                compose.show_status("\u25c9 no scheduled messages")
            return

        # Handle /at scheduling command
        if body.lower().startswith("/at "):
            self._handle_schedule_command(body[4:])
            return

        self._send_body(body, attachment_path=event.attachment_path)

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

    def action_show_schedule(self) -> None:
        self._update_tabs("schedule")

    def _on_reply_editor_done(self, result: str) -> None:
        self._update_tabs("messages")
        if result:
            self._send_body(result)

    def _handle_schedule_command(self, text: str) -> None:
        """Parse and schedule: /at <time> [tomorrow|date] <message>."""
        compose = self.query_one(ComposeBox)
        parsed = _parse_schedule_time(text)
        if parsed is None:
            compose.show_status("\u25c9 bad format. use: /at 9:30am Hey or /at 9:30am tomorrow Hey", error=True)
            return
        scheduled_at, message = parsed
        with get_connection() as conn:
            add_scheduled_message(conn, self._current_contact_id, message, scheduled_at)
        compose.show_status(f"\u25c9 scheduled for {scheduled_at}")
        self._load_chat()

    def _send_body(self, body: str, attachment_path: str = "") -> None:
        if self._current_contact_id is None:
            return
        contact_name = self._current_contact_name
        contact_id = self._current_contact_id
        compose = self.query_one(ComposeBox)
        status = "\u25c9 sending..." if not attachment_path else "\u25c9 uploading + sending..."
        compose.show_status(status)
        threading.Thread(
            target=self._do_send,
            args=(contact_id, contact_name, body, attachment_path),
            daemon=True,
        ).start()

    def _do_send(self, contact_id: int, contact_name: str, body: str,
                 attachment_path: str = "") -> None:
        # If there's an attachment, SCP it to Mini first, then send via MQTT
        if attachment_path:
            try:
                output = self._send_with_attachment(contact_name, body, attachment_path)
                success = True
            except Exception as e:
                output = str(e)
                success = False
        else:
            try:
                output = send_message(contact_name, body)
                success = True
            except RelayError as e:
                output = str(e)
                success = False

        platform_str = "sms"
        if "imessage" in output.lower() or "imsg" in output.lower():
            platform_str = "imessage"

        with get_connection() as conn:
            mid = add_message(
                conn, contact_id, platform_str, "out", body,
                delivered=success, relay_output=output,
            )
            msg = Message(
                id=mid, contact_id=contact_id, platform=platform_str,
                direction="out", body=body, delivered=success,
                relay_output=output,
            )

        self.call_from_thread(self._on_send_done, msg, output, success)

    def _on_send_done(self, msg: Message, output: str, success: bool) -> None:
        chat = self.query_one(ChatView)
        chat.append_message(msg)
        compose = self.query_one(ComposeBox)
        if success:
            compose.show_status(f"\u25c9 RELAY OK  {output}")
        else:
            compose.show_status(f"\u25c9 RELAY FAIL  {output}", error=True)

    def _send_with_attachment(self, contact_name: str, body: str,
                              local_path: str) -> str:
        """SCP file to Mini, then send via MQTT with attachment."""
        filename = local_path.rsplit("/", 1)[-1]
        remote_path = f"/tmp/m0usunet-send-{filename}"

        # SCP to Mini
        r = subprocess.run(
            ["scp", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10",
             local_path, f"mini:{remote_path}"],
            capture_output=True, text=True, timeout=120,
        )
        if r.returncode != 0:
            raise RelayError(f"SCP failed: {r.stderr.strip()}")

        # Resolve phone number
        with get_connection() as conn:
            contact = get_contact(conn, self._current_contact_id)
        if not contact or not contact.phone:
            raise RelayError("no phone number for contact")

        # Send via MQTT with attachment path
        payload = _json.dumps({
            "number": contact.phone,
            "message": body if body and body != "[attachment]" else "",
            "attachment": remote_path,
        })
        r = subprocess.run(
            ["mosquitto_pub", "-h", "192.168.0.15", "-p", "8883",
             "--cafile", "/home/jackpi5/mini-mqtt.crt",
             "-t", "cmd/mini/imessage/send", "-m", payload],
            capture_output=True, text=True, timeout=10,
        )
        if r.returncode != 0:
            raise RelayError(f"MQTT publish failed: {r.stderr.strip()}")

        return f"iMessage (MQTT+attachment) -> {contact_name}: {filename}"

    def action_toggle_focus(self) -> None:
        # If in text input, Tab cycles focus within messages view
        focused = self.focused
        if focused and focused.id == "compose-input":
            try:
                self.query_one(ComposeBox)._hide_input()
            except Exception:
                pass
            self.query_one(ConversationList).focus()
            return
        # Otherwise, Tab toggles between MESSAGES and SCHEDULE
        if self._active_tab == "messages":
            self.action_show_schedule()
        else:
            self.action_show_messages()

    # ── Claude suggestion ───────────────────────────────

    def action_suggest_reply(self) -> None:
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
        compose.show_status("\u25c9 suggestion loaded \u2014 edit or hit Enter to send")

    def action_copy_selection(self) -> None:
        """Copy selected text to system clipboard via OSC 52."""
        try:
            area = self.query_one("#chat-area", TextArea)
            text = area.selected_text
            if not text:
                return
            b64 = base64.b64encode(text.encode()).decode()
            # Write directly to the TTY, bypassing Textual's stdout
            try:
                tty = os.open("/dev/tty", os.O_WRONLY)
                os.write(tty, f"\033]52;c;{b64}\a".encode())
                os.close(tty)
            except OSError:
                pass
        except Exception:
            pass

    def action_escape(self) -> None:
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
