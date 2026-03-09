"""Clipboard helpers."""

import base64
import platform
import subprocess


def copy_osc52(text: str) -> None:
    """Copy text to system clipboard.

    Uses pbcopy on macOS (native, always works).
    Falls back to OSC 52 escape sequence for Linux/SSH.
    """
    if platform.system() == "Darwin":
        subprocess.run(["pbcopy"], input=text.encode(), check=True)
    else:
        encoded = base64.b64encode(text.encode()).decode()
        osc = f"\033]52;c;{encoded}\a"
        with open("/dev/tty", "w") as tty:
            tty.write(osc)
            tty.flush()
