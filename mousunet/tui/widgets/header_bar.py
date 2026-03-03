"""DedSec header bar: app name, clock, mesh status, ingest indicator."""

from __future__ import annotations

import subprocess
import threading
from datetime import datetime

from textual.app import ComposeResult
from textual.containers import Horizontal
from textual.widget import Widget
from textual.widgets import Static


MESH_DEVICES = {
    "PIXEL": "192.168.0.22",
    "IPAD": "192.168.0.11",
}


class TabButton(Static):
    """Clickable tab button in the header."""

    def on_click(self, event) -> None:
        event.stop()
        if self.id == "tab-messages":
            self.app.action_show_messages()
        elif self.id == "tab-editor":
            self.app.action_reply_editor()


class HeaderBar(Widget):
    """Top bar with app title, time, mesh status, and ingest health."""

    def __init__(self) -> None:
        super().__init__()
        self._device_status: dict[str, bool] = {name: False for name in MESH_DEVICES}
        self._pinging = False
        self._ingest_ok = True

    def compose(self) -> ComposeResult:
        with Horizontal():
            yield Static("[#00d4ff bold]M0usu[/][#50fa7b bold]Net[/]", id="app-title")
            yield TabButton(" MSG ", id="tab-messages", classes="tab-btn --active")
            yield TabButton(" EDIT ", id="tab-editor", classes="tab-btn")
            yield Static("", id="ingest-status")
            yield Static("", id="mesh-status")
            yield Static("", id="clock")

    def on_mount(self) -> None:
        self.set_interval(1.0, self._update_clock)
        self.set_interval(60.0, self._check_devices_bg)
        self.set_interval(30.0, self._check_ingest)
        self._update_clock()
        self.set_timer(2.0, self._check_devices_bg)
        self.set_timer(3.0, self._check_ingest)

    def _update_clock(self) -> None:
        now = datetime.now().strftime("%H:%M:%S")
        try:
            self.query_one("#clock", Static).update(f"[#555555]{now}[/]")
        except Exception:
            pass

    def _check_ingest(self) -> None:
        """Check if ingest daemon service is running."""
        threading.Thread(target=self._do_check_ingest, daemon=True).start()

    def _do_check_ingest(self) -> None:
        try:
            result = subprocess.run(
                ["systemctl", "is-active", "mousunet-ingest"],
                capture_output=True, text=True, timeout=5,
            )
            active = result.stdout.strip() == "active"
            self.app.call_from_thread(self._render_ingest, active)
        except Exception:
            self.app.call_from_thread(self._render_ingest, False)

    def _render_ingest(self, active: bool) -> None:
        try:
            widget = self.query_one("#ingest-status", Static)
            if active:
                widget.update("[#50fa7b]\u25c9 INGEST[/]")
            else:
                widget.update("[#f75341]\u25c9 INGEST:OFF[/]")
        except Exception:
            pass

    def _check_devices_bg(self) -> None:
        if self._pinging:
            return
        self._pinging = True
        threading.Thread(target=self._ping_devices, daemon=True).start()

    def _ping_devices(self) -> None:
        try:
            for label, host in MESH_DEVICES.items():
                try:
                    result = subprocess.run(
                        ["ping", "-c", "1", "-W", "1", host],
                        capture_output=True,
                        timeout=3,
                    )
                    self._device_status[label] = result.returncode == 0
                except Exception:
                    self._device_status[label] = False
            self.app.call_from_thread(self._render_status)
        finally:
            self._pinging = False

    def _render_status(self) -> None:
        parts = []
        for label, alive in self._device_status.items():
            if alive:
                parts.append(f"[#50fa7b]\u25c9 {label}[/]")
            else:
                parts.append(f"[#f75341]\u25cb {label}[/]")
        text = "  ".join(parts)
        try:
            self.query_one("#mesh-status", Static).update(text)
        except Exception:
            pass
