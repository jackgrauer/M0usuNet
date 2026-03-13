"""Paths, colors, and defaults."""

import os
import socket
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

# ── Mesh & security ─────────────────────────────────────

# Node identity — defaults to hostname
NODE_ID = os.environ.get("M0USUNET_NODE_ID", socket.gethostname())

# Ed25519 keypair for message signing
NODE_KEY_PATH = Path(os.environ.get(
    "M0USUNET_NODE_KEY",
    str(Path.home() / ".m0usunet" / "node.key"),
))

# Hooks directory
HOOKS_DIR = Path(os.environ.get(
    "M0USUNET_HOOKS_DIR",
    str(Path.home() / ".m0usunet" / "hooks"),
))

# Backup directory
BACKUP_DIR = Path(os.environ.get(
    "M0USUNET_BACKUP_DIR",
    str(Path.home() / ".m0usunet" / "backups"),
))

# Heartbeat interval (seconds)
HEARTBEAT_INTERVAL = int(os.environ.get("M0USUNET_HEARTBEAT_INTERVAL", "30"))
