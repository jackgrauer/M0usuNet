"""Mesh heartbeat system — publish and track node health.

Each node publishes its status to mesh/heartbeat/<node_id> every
HEARTBEAT_INTERVAL seconds. Other nodes subscribe and track health
in the mesh_nodes table.

States:
    online    — heartbeat received within 2 intervals
    degraded  — missed 1-2 heartbeats (3-6 intervals)
    offline   — missed >2 heartbeats
"""

import json
import logging
import os
import threading
import time
from datetime import datetime, timezone

from . import __version__
from .constants import HEARTBEAT_INTERVAL, NODE_ID

log = logging.getLogger(__name__)

TOPIC_HEARTBEAT = "mesh/heartbeat"  # publish to mesh/heartbeat/<node_id>
TOPIC_HEARTBEAT_WILDCARD = "mesh/heartbeat/+"  # subscribe pattern

# Thresholds (in seconds)
DEGRADED_AFTER = HEARTBEAT_INTERVAL * 3   # 90s at default 30s interval
OFFLINE_AFTER = HEARTBEAT_INTERVAL * 6    # 180s

_start_time = time.monotonic()


def _build_heartbeat(transports: list[str] | None = None, queue_depth: int = 0) -> dict:
    """Build a heartbeat payload for this node."""
    return {
        "node": NODE_ID,
        "uptime_s": int(time.monotonic() - _start_time),
        "version": __version__,
        "transports": transports or [],
        "queue_depth": queue_depth,
        "ts": datetime.now(timezone.utc).isoformat(),
        "pid": os.getpid(),
    }


def publish_heartbeat(mqtt_client, transports: list[str] | None = None, queue_depth: int = 0) -> None:
    """Publish a heartbeat to the MQTT broker."""
    payload = _build_heartbeat(transports, queue_depth)
    topic = f"{TOPIC_HEARTBEAT}/{NODE_ID}"
    mqtt_client.publish(topic, json.dumps(payload), qos=0)


def start_heartbeat_publisher(
    mqtt_client,
    interval: float = HEARTBEAT_INTERVAL,
    transports: list[str] | None = None,
) -> threading.Thread:
    """Start a background thread that publishes heartbeats."""
    def _loop():
        while True:
            try:
                # Get queue depth from outbox
                queue_depth = _get_queue_depth()
                publish_heartbeat(mqtt_client, transports, queue_depth)
            except Exception:
                log.debug("Heartbeat publish failed", exc_info=True)
            time.sleep(interval)

    t = threading.Thread(target=_loop, daemon=True)
    t.start()
    log.info("Heartbeat publisher started (interval=%ds, node=%s)", interval, NODE_ID)
    return t


def _get_queue_depth() -> int:
    """Count pending outbox messages."""
    try:
        from .db import get_connection
        with get_connection() as conn:
            row = conn.execute(
                "SELECT COUNT(*) AS cnt FROM outbox WHERE status IN ('queued', 'sending')"
            ).fetchone()
            return row["cnt"] if row else 0
    except Exception:
        return 0


def handle_heartbeat(payload: bytes) -> None:
    """Process an incoming heartbeat from another node."""
    try:
        data = json.loads(payload)
    except (json.JSONDecodeError, ValueError):
        return

    node_id = data.get("node", "")
    if not node_id or node_id == NODE_ID:
        return  # Ignore our own heartbeats

    from .db import get_connection
    with get_connection() as conn:
        conn.execute(
            "INSERT INTO mesh_nodes (node_id, status, last_seen, uptime_s, version, transports, queue_depth) "
            "VALUES (?, 'online', CURRENT_TIMESTAMP, ?, ?, ?, ?) "
            "ON CONFLICT(node_id) DO UPDATE SET "
            "status = 'online', last_seen = CURRENT_TIMESTAMP, "
            "uptime_s = excluded.uptime_s, version = excluded.version, "
            "transports = excluded.transports, queue_depth = excluded.queue_depth",
            (
                node_id,
                data.get("uptime_s", 0),
                data.get("version", ""),
                json.dumps(data.get("transports", [])),
                data.get("queue_depth", 0),
            ),
        )
        conn.commit()


def sweep_stale_nodes() -> None:
    """Mark nodes as degraded or offline based on last_seen time."""
    from .db import get_connection
    with get_connection() as conn:
        # Degraded: last seen > DEGRADED_AFTER seconds ago
        conn.execute(
            "UPDATE mesh_nodes SET status = 'degraded' "
            "WHERE status = 'online' "
            "AND (julianday('now') - julianday(last_seen)) * 86400 > ?",
            (DEGRADED_AFTER,),
        )
        # Offline: last seen > OFFLINE_AFTER seconds ago
        conn.execute(
            "UPDATE mesh_nodes SET status = 'offline' "
            "WHERE status IN ('online', 'degraded') "
            "AND (julianday('now') - julianday(last_seen)) * 86400 > ?",
            (OFFLINE_AFTER,),
        )
        conn.commit()


def start_stale_sweeper(interval: float = HEARTBEAT_INTERVAL * 2) -> threading.Thread:
    """Background thread that marks stale nodes."""
    def _loop():
        while True:
            try:
                sweep_stale_nodes()
            except Exception:
                log.debug("Stale sweeper error", exc_info=True)
            time.sleep(interval)

    t = threading.Thread(target=_loop, daemon=True)
    t.start()
    log.info("Stale node sweeper started (interval=%ds)", interval)
    return t


def get_mesh_status() -> list[dict]:
    """Return current mesh node status for display."""
    from .db import get_connection
    with get_connection() as conn:
        rows = conn.execute(
            "SELECT node_id, status, last_seen, uptime_s, version, transports, queue_depth "
            "FROM mesh_nodes ORDER BY node_id"
        ).fetchall()
        return [dict(r) for r in rows]
