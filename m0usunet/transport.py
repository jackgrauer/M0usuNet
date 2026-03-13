"""Multi-transport abstraction for outbound message delivery.

Each transport knows how to send a message to a recipient via a specific
channel (iMessage, SMS, Signal, etc.) and can report its health status.
"""

import logging
import subprocess
from typing import Protocol, runtime_checkable

from .constants import RELAY_PATH
from .exceptions import RelayError

log = logging.getLogger(__name__)


@runtime_checkable
class Transport(Protocol):
    """Interface that all transports must implement."""

    name: str

    def send(self, recipient: str, body: str, timeout: float = 60.0) -> str:
        """Send a message. Returns delivery confirmation string.

        Raises:
            RelayError: If delivery fails.
        """
        ...

    def health_check(self) -> bool:
        """Returns True if the transport is operational."""
        ...


class RelayTransport:
    """Send via relay.sh (routes to iMessage or SMS via Mini)."""

    name = "relay"

    def send(self, recipient: str, body: str, timeout: float = 60.0) -> str:
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
        except PermissionError as e:
            raise RelayError(f"relay script not executable: {RELAY_PATH}") from e

        if result.returncode != 0:
            stderr = result.stderr.strip()
            raise RelayError(f"relay exited {result.returncode}: {stderr}")

        return result.stdout.strip()

    def health_check(self) -> bool:
        return RELAY_PATH.exists() and RELAY_PATH.stat().st_mode & 0o111


class IMessageTransport:
    """Send iMessage via SSH to Mac Mini."""

    name = "imessage"

    def __init__(self, mini_host: str = "mini"):
        self.mini_host = mini_host

    def send(self, recipient: str, body: str, timeout: float = 60.0) -> str:
        escaped_body = body.replace("\\", "\\\\").replace('"', '\\"')
        script = (
            f'tell application "Messages" to send "{escaped_body}" '
            f'to buddy "{recipient}" of service "iMessage"'
        )
        try:
            result = subprocess.run(
                ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=5",
                 self.mini_host, "osascript", "-e", script],
                capture_output=True, text=True, timeout=timeout,
            )
        except subprocess.TimeoutExpired as e:
            raise RelayError(f"iMessage send timed out after {timeout}s") from e
        except Exception as e:
            raise RelayError(f"iMessage transport error: {e}") from e

        if result.returncode != 0:
            raise RelayError(f"iMessage send failed: {result.stderr.strip()}")
        return f"iMessage -> {recipient}: {body[:40]}"

    def health_check(self) -> bool:
        try:
            result = subprocess.run(
                ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=3",
                 self.mini_host, "echo", "ok"],
                capture_output=True, text=True, timeout=5,
            )
            return result.returncode == 0
        except Exception:
            return False


class SMSTransport:
    """Send SMS via MQTT command to Pixel."""

    name = "sms"

    def __init__(self, mqtt_client=None):
        self._mqtt_client = mqtt_client

    def set_mqtt_client(self, client) -> None:
        self._mqtt_client = client

    def send(self, recipient: str, body: str, timeout: float = 60.0) -> str:
        if self._mqtt_client is None:
            raise RelayError("SMS transport: no MQTT client configured")
        import json
        payload = json.dumps({"phone": recipient, "text": body})
        info = self._mqtt_client.publish("cmd/pixel/sms/send", payload, qos=1)
        info.wait_for_publish(timeout=timeout)
        return f"SMS -> {recipient}: {body[:40]}"

    def health_check(self) -> bool:
        return self._mqtt_client is not None and self._mqtt_client.is_connected()


class TransportRegistry:
    """Registry of available transports with routing logic."""

    def __init__(self):
        self._transports: dict[str, Transport] = {}

    def register(self, transport: Transport) -> None:
        self._transports[transport.name] = transport
        log.info("Registered transport: %s", transport.name)

    def get(self, name: str) -> Transport | None:
        return self._transports.get(name)

    def available(self) -> list[str]:
        """Return names of healthy transports."""
        return [
            name for name, t in self._transports.items()
            if t.health_check()
        ]

    def all_names(self) -> list[str]:
        return list(self._transports.keys())

    def send(self, transport_name: str, recipient: str, body: str) -> str:
        """Send via a named transport, with fallback to relay."""
        transport = self._transports.get(transport_name)
        if transport is None:
            # Fall back to relay
            transport = self._transports.get("relay")
        if transport is None:
            raise RelayError(f"No transport available: {transport_name}")
        return transport.send(recipient, body)


# Global registry — initialized once at daemon startup
_registry: TransportRegistry | None = None


def get_registry() -> TransportRegistry:
    """Get or create the global transport registry."""
    global _registry
    if _registry is None:
        _registry = TransportRegistry()
        _registry.register(RelayTransport())
    return _registry
