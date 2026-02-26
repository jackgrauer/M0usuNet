"""Paths, colors, and defaults."""

import os
from pathlib import Path

# Database — override with MOUSUNET_DB env var
DB_PATH = Path(os.environ.get("MOUSUNET_DB", str(Path.home() / "mousunet.db")))

# Relay — override with MOUSUNET_RELAY env var
# Pi: ~/relay.sh (local)
# Mac: ~/bin/msg (routes through Pi)
RELAY_PATH = Path(os.environ.get("MOUSUNET_RELAY", str(Path.home() / "relay.sh")))

# Contacts
CONTACTS_TSV = Path.home() / "contacts.tsv"

# Polling
POLL_INTERVAL = 5.0  # seconds (TUI refresh)
INGEST_INTERVAL = 30  # seconds (message ingestion)

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
