"""Clipboard via OSC 52 escape sequence (works over SSH)."""

import base64
import sys


def copy_osc52(text: str) -> None:
    """Write text to system clipboard via OSC 52.

    Works in iTerm2, Ghostty, kitty, WezTerm, most modern terminals.
    """
    encoded = base64.b64encode(text.encode()).decode()
    # Write directly to the terminal (bypass Textual's output)
    sys.stdout.write(f"\033]52;c;{encoded}\a")
    sys.stdout.flush()
