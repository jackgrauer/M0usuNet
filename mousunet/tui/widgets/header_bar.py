"""DedSec header bar: app name, clock, mesh device status."""

from __future__ import annotations

import subprocess
import threading
from datetime import datetime

from textual.app import ComposeResult
from textual.containers import Horizontal
from textual.widget import Widget
from textual.widgets import Static


MESH_DEVICES = {
    "PIXEL": "pixel",
    "IPAD": "ipad",
}


class HeaderBar(Widget):
    """Top bar with app title, time, and mesh status indicators."""

    def __init__(self) -> None:
        super().__init__()
        self._device_status: dict[str, bool] = {name: False for name in MESH_DEVICES}

    def compose(self) -> ComposeResult:
        with Horizontal():
            yield Static("M0usuNet", id="app-title")
            yield Static("", id="mesh-status")
            yield Static("", id="clock")

    def on_mount(self) -> None:
        self.set_interval(1.0, self._update_clock)
        self.set_interval(60.0, self._check_devices_bg)
        self._update_clock()
        # Defer first device check so UI renders immediately
        self.set_timer(2.0, self._check_devices_bg)

    def _update_clock(self) -> None:
        now = datetime.now().strftime("%H:%M:%S")
        try:
            self.query_one("#clock", Static).update(now)
        except Exception:
            pass

    def _check_devices_bg(self) -> None:
        """Run pings in a background thread so they don't block the UI."""
        threading.Thread(target=self._ping_devices, daemon=True).start()

    def _ping_devices(self) -> None:
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
        # Schedule UI update back on the main thread
        self.app.call_from_thread(self._render_status)

    def _render_status(self) -> None:
        parts = []
        for label, alive in self._device_status.items():
            if alive:
                parts.append(f"[#50fa7b]\u25c9 {label}:LIVE[/]")
            else:
                parts.append(f"[#f75341]\u25c9 {label}:DOWN[/]")
        text = "  ".join(parts)
        try:
            self.query_one("#mesh-status", Static).update(text)
        except Exception:
            pass
