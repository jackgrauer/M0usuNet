"""Paths, colors, and defaults."""

import os
from pathlib import Path

# Database — override with M0USUNET_DB env var
DB_PATH = Path(os.environ.get("M0USUNET_DB", str(Path.home() / "m0usunet.db")))

# Relay — ~/relay.sh on Pi (direct to iPad/Pixel)
RELAY_PATH = Path(os.environ.get("M0USUNET_RELAY", str(Path.home() / "relay.sh")))

# Contacts
CONTACTS_TSV = Path.home() / "contacts.tsv"

# Attachments
ATTACHMENTS_DIR = Path.home() / ".m0usunet" / "attachments"

# Timezone for scheduled messages
USER_TZ = os.environ.get("M0USUNET_TZ", "America/New_York")

# DedSec palette
class Color:
    BG = "#0a0a0a"
    SUBTLE_BG = "#0d0d0d"
    TEXT = "#e0e0e0"
    MUTED = "#555555"
    CYAN = "#00d4ff"       # primary accent, borders
    GREEN = "#50fa7b"      # outbound sender, sms tag
    BLUE = "#68a8e4"       # imessage tag
    MAGENTA = "#ff2d6f"    # notifications, dating tags
    ORANGE = "#ff8c00"     # alerts
    RED = "#f75341"        # errors
    DIM_CYAN = "#1a5c7a"   # timestamps, ticker
