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
