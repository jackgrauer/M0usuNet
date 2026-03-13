"""Hook system — run user scripts on message events.

Hooks are executable files in ~/.m0usunet/hooks/<event>/.
They receive the message as JSON on stdin and must exit 0 to
allow the message through. Exit 1 = drop the message.

Events:
    on_receive — inbound message from MQTT
    on_send    — outbound message before relay

Example hook:
    ~/.m0usunet/hooks/on_receive/log.sh
    #!/bin/bash
    cat >> ~/m0usunet-log.jsonl
"""

import json
import logging
import os
import subprocess
from pathlib import Path

from .constants import HOOKS_DIR

log = logging.getLogger(__name__)

# Cache discovered hooks so we don't stat the filesystem on every message
_hook_cache: dict[str, list[Path] | None] = {}


def _discover_hooks(event: str) -> list[Path]:
    """Find executable hooks for an event, sorted by name."""
    hook_dir = HOOKS_DIR / event
    if not hook_dir.is_dir():
        return []
    hooks = []
    for entry in sorted(hook_dir.iterdir()):
        if entry.is_file() and os.access(entry, os.X_OK):
            hooks.append(entry)
    return hooks


def invalidate_cache() -> None:
    """Clear the hook cache (call after adding/removing hooks)."""
    _hook_cache.clear()


def run_hooks(event: str, message: dict, timeout: float = 10.0) -> bool:
    """Run all hooks for an event. Returns True if message should proceed.

    Args:
        event: Hook event name ("on_receive" or "on_send").
        message: Message dict to pass as JSON on stdin.
        timeout: Max seconds per hook.

    Returns:
        True if all hooks passed (or no hooks exist). False if any hook
        rejected the message (exit != 0).
    """
    if event not in _hook_cache:
        _hook_cache[event] = _discover_hooks(event)

    hooks = _hook_cache[event]
    if not hooks:
        return True

    msg_json = json.dumps(message)

    for hook in hooks:
        try:
            result = subprocess.run(
                [str(hook)],
                input=msg_json,
                capture_output=True,
                text=True,
                timeout=timeout,
            )
            if result.returncode != 0:
                log.info(
                    "Hook %s/%s rejected message (exit %d): %s",
                    event, hook.name, result.returncode,
                    result.stderr.strip()[:200],
                )
                return False
        except subprocess.TimeoutExpired:
            log.warning("Hook %s/%s timed out after %.0fs", event, hook.name, timeout)
            return False
        except Exception:
            log.exception("Hook %s/%s failed", event, hook.name)
            # Hook errors don't block messages
            continue

    return True
