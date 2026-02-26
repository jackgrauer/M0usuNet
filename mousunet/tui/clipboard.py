"""Clipboard via OSC 52 escape sequence (works over SSH)."""

import base64


def copy_osc52(text: str) -> None:
    """Write text to system clipboard via OSC 52.

    Writes to /dev/tty to bypass Textual's stdout capture.
    Works in kitty, Ghostty, iTerm2, WezTerm over SSH.
    """
    encoded = base64.b64encode(text.encode()).decode()
    osc = f"\033]52;c;{encoded}\a"
    with open("/dev/tty", "w") as tty:
        tty.write(osc)
        tty.flush()
