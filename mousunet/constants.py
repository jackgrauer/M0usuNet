"""Paths, colors, and defaults."""

from pathlib import Path

# Database
DB_PATH = Path.home() / "mousunet.db"

# Relay
RELAY_PATH = Path.home() / "relay.sh"

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
    DIM_CYAN = "#1a5c7a"  # timestamps, ticker
