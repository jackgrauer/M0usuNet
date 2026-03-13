"""Database backup and replication.

Provides both local snapshots (via SQLite .backup API) and optional
SCP push to a remote node for redundancy.
"""

import logging
import sqlite3
import subprocess
from datetime import datetime
from pathlib import Path

from .constants import BACKUP_DIR, DB_PATH

log = logging.getLogger(__name__)

MAX_LOCAL_BACKUPS = 7  # Keep last 7 daily backups


def local_backup(tag: str = "") -> Path:
    """Create a local backup using SQLite's online backup API.

    Returns the path to the backup file.
    """
    BACKUP_DIR.mkdir(parents=True, exist_ok=True)
    ts = datetime.now().strftime("%Y%m%d_%H%M%S")
    suffix = f"_{tag}" if tag else ""
    dest = BACKUP_DIR / f"m0usunet_{ts}{suffix}.db"

    source = sqlite3.connect(str(DB_PATH))
    target = sqlite3.connect(str(dest))
    try:
        source.backup(target)
        log.info("Backup created: %s (%.1f KB)", dest.name, dest.stat().st_size / 1024)
    finally:
        target.close()
        source.close()

    _prune_old_backups()
    return dest


def _prune_old_backups() -> None:
    """Remove old backups beyond MAX_LOCAL_BACKUPS."""
    backups = sorted(BACKUP_DIR.glob("m0usunet_*.db"), key=lambda p: p.stat().st_mtime)
    while len(backups) > MAX_LOCAL_BACKUPS:
        old = backups.pop(0)
        old.unlink()
        log.info("Pruned old backup: %s", old.name)


def remote_backup(
    remote_host: str = "mini",
    remote_dir: str = "~/.m0usunet/backups",
    timeout: float = 120.0,
) -> bool:
    """SCP the latest backup to a remote host for redundancy.

    Returns True on success.
    """
    # Create a fresh local backup first
    local = local_backup(tag="remote")

    try:
        # Ensure remote directory exists
        subprocess.run(
            ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=5",
             remote_host, "mkdir", "-p", remote_dir],
            capture_output=True, timeout=10,
        )
        result = subprocess.run(
            ["scp", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10",
             str(local), f"{remote_host}:{remote_dir}/"],
            capture_output=True, text=True, timeout=timeout,
        )
        if result.returncode == 0:
            log.info("Remote backup pushed to %s:%s", remote_host, remote_dir)
            return True
        else:
            log.warning("Remote backup failed: %s", result.stderr.strip())
            return False
    except subprocess.TimeoutExpired:
        log.warning("Remote backup timed out after %.0fs", timeout)
        return False
    except Exception as e:
        log.warning("Remote backup error: %s", e)
        return False
