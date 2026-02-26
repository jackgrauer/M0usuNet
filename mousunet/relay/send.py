"""Subprocess wrapper for ~/relay.sh."""

import subprocess

from ..constants import RELAY_PATH
from ..exceptions import RelayError


def send_message(recipient: str, body: str, timeout: float = 30.0) -> str:
    """Call relay.sh and return its stdout.

    Args:
        recipient: Contact name or phone number.
        body: Message text.
        timeout: Seconds before giving up.

    Returns:
        Relay stdout (e.g. "iMessage -> Mom (+16099809954): hey").

    Raises:
        RelayError: If relay exits non-zero or times out.
    """
    try:
        result = subprocess.run(
            [str(RELAY_PATH), recipient, body],
            capture_output=True,
            text=True,
            timeout=timeout,
        )
    except subprocess.TimeoutExpired as e:
        raise RelayError(f"relay timed out after {timeout}s") from e
    except FileNotFoundError as e:
        raise RelayError(f"relay script not found: {RELAY_PATH}") from e

    if result.returncode != 0:
        stderr = result.stderr.strip()
        raise RelayError(f"relay exited {result.returncode}: {stderr}")

    return result.stdout.strip()
