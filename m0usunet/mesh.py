"""Mesh routing — multi-hop message delivery across nodes.

Topology: any node can relay to any other node via MQTT.
Each node advertises its capabilities (transports) via heartbeats.
The router picks the best path based on node health and latency.

MQTT topics:
    mesh/heartbeat/<node_id>   — health + capabilities (see heartbeat.py)
    mesh/route/<node_id>       — routed message delivery requests
    mesh/announce              — node join/leave announcements
"""

import json
import logging
import time

from .constants import NODE_ID

log = logging.getLogger(__name__)

TOPIC_ROUTE = "mesh/route"  # mesh/route/<target_node>
TOPIC_ANNOUNCE = "mesh/announce"


def announce_join(mqtt_client, transports: list[str]) -> None:
    """Announce this node joining the mesh."""
    payload = {
        "node": NODE_ID,
        "event": "join",
        "transports": transports,
        "ts": int(time.time()),
    }
    mqtt_client.publish(TOPIC_ANNOUNCE, json.dumps(payload), qos=1)
    log.info("Mesh announce: join (transports=%s)", transports)


def announce_leave(mqtt_client) -> None:
    """Announce this node leaving the mesh."""
    payload = {
        "node": NODE_ID,
        "event": "leave",
        "ts": int(time.time()),
    }
    mqtt_client.publish(TOPIC_ANNOUNCE, json.dumps(payload), qos=1)
    log.info("Mesh announce: leave")


def route_message(
    mqtt_client,
    target_node: str,
    recipient: str,
    body: str,
    transport: str = "relay",
) -> None:
    """Route a message to a specific node for delivery.

    Used when the local node doesn't have the required transport
    but another mesh node does.
    """
    payload = {
        "from_node": NODE_ID,
        "recipient": recipient,
        "body": body,
        "transport": transport,
        "ts": int(time.time()),
    }
    topic = f"{TOPIC_ROUTE}/{target_node}"
    mqtt_client.publish(topic, json.dumps(payload), qos=1)
    log.info("Routed message to %s via %s for %s", target_node, transport, recipient)


def handle_routed_message(payload: bytes) -> None:
    """Handle an incoming routed message — deliver it locally."""
    try:
        data = json.loads(payload)
    except (json.JSONDecodeError, ValueError):
        return

    from_node = data.get("from_node", "")
    recipient = data.get("recipient", "")
    body = data.get("body", "")
    transport_name = data.get("transport", "relay")

    if not recipient or not body:
        log.warning("Routed message missing recipient/body from %s", from_node)
        return

    log.info("Received routed message from %s: %s -> %s", from_node, transport_name, recipient)

    from .transport import get_registry
    from .exceptions import RelayError

    registry = get_registry()
    try:
        output = registry.send(transport_name, recipient, body)
        log.info("Routed delivery success: %s", output[:80])
    except RelayError as e:
        log.warning("Routed delivery failed: %s", e)


def find_route(transport_name: str) -> str | None:
    """Find a mesh node that can handle a given transport.

    Returns the node_id of the best available node, or None if
    no node is available for that transport.
    """
    from .db import get_connection
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT node_id, transports, queue_depth FROM mesh_nodes "
            "WHERE status = 'online' ORDER BY queue_depth ASC"
        ).fetchall()

    for row in rows:
        try:
            node_transports = json.loads(row["transports"] or "[]")
        except (json.JSONDecodeError, ValueError):
            continue
        if transport_name in node_transports:
            return row["node_id"]

    return None


def smart_send(
    mqtt_client,
    recipient: str,
    body: str,
    preferred_transport: str = "relay",
) -> str:
    """Send a message using the best available route.

    1. Try local transport first
    2. If unavailable, find a mesh node that has it
    3. Route through that node

    Returns delivery output string.
    """
    from .transport import get_registry
    from .exceptions import RelayError

    registry = get_registry()

    # Try local first
    local_transport = registry.get(preferred_transport)
    if local_transport and local_transport.health_check():
        return local_transport.send(recipient, body)

    # Try relay fallback locally
    if preferred_transport != "relay":
        relay = registry.get("relay")
        if relay and relay.health_check():
            return relay.send(recipient, body)

    # Find a mesh node
    target = find_route(preferred_transport)
    if target:
        route_message(mqtt_client, target, recipient, body, preferred_transport)
        return f"Routed via {target} ({preferred_transport}) -> {recipient}"

    raise RelayError(f"No route available for transport={preferred_transport}")
