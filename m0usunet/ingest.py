"""Inbound message ingestion from Mac Mini and Pixel.

Can run as a standalone daemon (m0usunet ingest) or as a background
thread started by the TUI on mount.

Mac Mini iMessage and Pixel SMS are both ingested via MQTT push from
their respective daemons. Uses paho-mqtt in-process with QoS 1 and
persistent sessions so the broker buffers messages during downtime.

v0.3.0: integrated rate limiting, hooks, heartbeats, mesh routing,
and message signing.
"""

import json
import logging
import re
import ssl
import subprocess
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone

import paho.mqtt.client as mqtt

from .constants import ATTACHMENTS_DIR, NODE_ID, NODE_KEY_PATH
from .db import (
    get_connection, upsert_contact, add_message_with_guid,
    add_attachment, update_attachment_status,
)
from .hooks import run_hooks
from .ratelimit import RateLimiter

log = logging.getLogger(__name__)

NTFY_TOPIC = "jack-mesh-alerts"
POLL_INTERVAL = 30  # seconds (watchdog check interval)

# MQTT config — broker runs on Mac Mini
MQTT_BROKER = "192.168.0.15"
MQTT_PORT = 8883
MQTT_CA = "/home/jackpi5/mini-mqtt.crt"
MQTT_CLIENT_ID = "m0usunet-daemon"

# Topics
TOPIC_MINI_MESSAGES = "mini/imessage/messages"
TOPIC_PIXEL_MESSAGES = "pixel/sms/messages"
TOPIC_REPLAY_REQUEST = "cmd/mini/imessage/replay"

# Rate limiter: 10 burst, 2/s sustained per topic
_limiter = RateLimiter(rate=2.0, burst=10)

# Signing — lazy-loaded
_private_key = None
_trusted_keys: dict = {}


def _get_private_key():
    """Lazy-load the node's Ed25519 private key."""
    global _private_key
    if _private_key is None:
        try:
            from .signing import load_private_key
            _private_key = load_private_key(NODE_KEY_PATH)
            log.info("Loaded Ed25519 key from %s", NODE_KEY_PATH)
        except Exception:
            log.warning("Ed25519 key not available — signing disabled", exc_info=True)
    return _private_key


def _load_trusted_keys():
    """Load public keys for known mesh nodes from DB."""
    global _trusted_keys
    try:
        from .signing import load_public_key
        with get_connection() as conn:
            rows = conn.execute(
                "SELECT node_id, public_key FROM mesh_nodes WHERE public_key IS NOT NULL"
            ).fetchall()
        for row in rows:
            try:
                _trusted_keys[row["node_id"]] = load_public_key(row["public_key"].encode())
            except Exception:
                log.debug("Failed to load key for node %s", row["node_id"])
    except Exception:
        log.debug("Could not load trusted keys", exc_info=True)


def _epoch_ms_to_iso(ms: int) -> str:
    """Convert epoch milliseconds to ISO 8601."""
    dt = datetime.fromtimestamp(ms / 1000, tz=timezone.utc)
    return dt.isoformat()


# ── Sync state + contact resolution ──────────────────────

def _get_sync_state(source: str) -> str:
    """Read last_synced_value for a source."""
    with get_connection() as conn:
        row = conn.execute(
            "SELECT last_synced_value FROM sync_state WHERE source = ?",
            (source,),
        ).fetchone()
        return row["last_synced_value"] if row else ""


def _set_sync_state(source: str, value: str) -> None:
    """Upsert sync state."""
    with get_connection() as conn:
        conn.execute(
            "INSERT INTO sync_state (source, last_synced_value, updated_at) "
            "VALUES (?, ?, CURRENT_TIMESTAMP) "
            "ON CONFLICT(source) DO UPDATE SET "
            "last_synced_value = excluded.last_synced_value, "
            "updated_at = excluded.updated_at",
            (source, value),
        )
        conn.commit()


def _resolve_contact(phone: str) -> int | None:
    """Find contact_id by phone number (exact or normalized)."""
    normalized = re.sub(r"[^\d+]", "", phone)
    with get_connection() as conn:
        row = conn.execute(
            "SELECT id FROM contacts WHERE phone = ?", (normalized,)
        ).fetchone()
        if row:
            return row["id"]
        if normalized.startswith("+1") and len(normalized) == 12:
            short = normalized[2:]
            row = conn.execute(
                "SELECT id FROM contacts WHERE phone LIKE ?", (f"%{short}",)
            ).fetchone()
            if row:
                return row["id"]
        new_id = upsert_contact(conn, normalized, normalized)
        log.info("Auto-created contact for %s (id=%d)", normalized, new_id)
        return new_id


def _contact_name(contact_id: int) -> str:
    """Look up display name for a contact id."""
    with get_connection() as conn:
        row = conn.execute(
            "SELECT display_name FROM contacts WHERE id = ?", (contact_id,)
        ).fetchone()
        return row["display_name"] if row else f"#{contact_id}"


def _send_ntfy(title: str, body: str) -> None:
    """Send a push notification via ntfy.sh (fire and forget)."""
    try:
        subprocess.run(
            ["curl", "-s",
             "-H", f"Title: {title}",
             "-d", body,
             f"https://ntfy.sh/{NTFY_TOPIC}"],
            capture_output=True,
            timeout=10,
        )
    except Exception:
        log.debug("ntfy send failed", exc_info=True)


# ── Attachment downloads ─────────────────────────────────

_download_pool = ThreadPoolExecutor(max_workers=2)


def _download_attachment(att_id: int, contact_id: int, remote_path: str, filename: str) -> None:
    """SCP an attachment from the Mini to local storage."""
    dest_dir = ATTACHMENTS_DIR / str(contact_id)
    dest_dir.mkdir(parents=True, exist_ok=True)
    local_path = dest_dir / f"{att_id}_{filename}"

    with get_connection() as conn:
        update_attachment_status(conn, att_id, "downloading")

    try:
        result = subprocess.run(
            ["scp", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10",
             f"mini:{remote_path}", str(local_path)],
            capture_output=True, text=True, timeout=120,
        )
        if result.returncode == 0:
            with get_connection() as conn:
                update_attachment_status(conn, att_id, "done", str(local_path))
            log.info("Attachment downloaded: %s", filename)
        else:
            with get_connection() as conn:
                update_attachment_status(conn, att_id, "failed")
            log.warning("SCP failed for %s: %s", filename, result.stderr.strip())
    except subprocess.TimeoutExpired:
        with get_connection() as conn:
            update_attachment_status(conn, att_id, "failed")
        log.warning("SCP timed out for %s", filename)
    except Exception as e:
        with get_connection() as conn:
            update_attachment_status(conn, att_id, "failed")
        log.warning("Attachment download error for %s: %s", filename, e)


# ── Message handlers ─────────────────────────────────────

def _handle_mini_msg(payload: bytes) -> None:
    """Process a message from mini/imessage/messages."""
    # Rate limit
    if not _limiter.allow("mini"):
        log.warning("Rate limited: mini topic")
        return

    try:
        msg = json.loads(payload)
    except (json.JSONDecodeError, ValueError):
        return

    handle_id = msg.get("handle_id", "")
    text = msg.get("text", "")
    attachments = msg.get("attachments", [])

    # Strip U+FFFC (object replacement character used for inline attachments)
    if text:
        text = text.replace("\ufffc", "").strip()

    if not handle_id or (not text and not attachments):
        return

    # If text is empty but there are attachments, use placeholder
    if not text and attachments:
        text = "[attachment]"

    contact_id = _resolve_contact(handle_id)
    if contact_id is None:
        return

    service = msg.get("service", "")
    platform = "imessage" if "imessage" in service else "sms"
    direction = "out" if msg.get("is_from_me", False) else "in"
    guid = f"mini:{msg.get('guid', '')}"
    sent_at = msg.get("sent_at_iso", "")

    # Run on_receive hooks
    hook_msg = {
        "source": "mini", "platform": platform, "direction": direction,
        "contact_id": contact_id, "body": text, "handle_id": handle_id,
    }
    if not run_hooks("on_receive", hook_msg):
        log.info("Message dropped by on_receive hook: %s", text[:40])
        return

    with get_connection() as conn:
        added = add_message_with_guid(
            conn,
            contact_id=contact_id,
            platform=platform,
            direction=direction,
            body=text,
            sent_at=sent_at,
            external_guid=guid,
        )

    if added:
        log.info("Mini MQTT: %s %s: %s", direction, handle_id, text[:40])
        apple_date = msg.get("apple_date", 0)
        if apple_date:
            _set_sync_state("mini", str(apple_date))
        if direction == "in":
            name = _contact_name(contact_id)
            _send_ntfy(name, text[:80])

        # Process attachments
        if attachments:
            with get_connection() as conn:
                # Find the message we just inserted
                row = conn.execute(
                    "SELECT id FROM messages WHERE external_guid = ?", (guid,)
                ).fetchone()
                if row:
                    msg_id = row["id"]
                    for att in attachments:
                        att_id = add_attachment(
                            conn, msg_id,
                            filename=att.get("transfer_name", att.get("filename", "unknown")),
                            mime_type=att.get("mime_type"),
                            total_bytes=att.get("total_bytes", 0),
                            remote_path=att.get("filename"),  # full path on Mini
                        )
                        # Queue background download
                        remote = att.get("filename", "")
                        if remote and att_id:
                            _download_pool.submit(
                                _download_attachment, att_id, contact_id,
                                remote, att.get("transfer_name", "unknown"),
                            )


def _handle_pixel_msg(payload: bytes) -> None:
    """Process a message from pixel/sms/messages."""
    # Rate limit
    if not _limiter.allow("pixel"):
        log.warning("Rate limited: pixel topic")
        return

    try:
        msg = json.loads(payload)
    except (json.JSONDecodeError, ValueError):
        return

    phone = msg.get("phone", "")
    text = msg.get("text", "")
    ts_ms = msg.get("timestamp_ms", 0)
    is_from_me = msg.get("is_from_me", False)
    msg_id = msg.get("msg_id", 0)

    if not phone or not text:
        return

    contact_id = _resolve_contact(phone)
    if contact_id is None:
        return

    direction = "out" if is_from_me else "in"
    sent_at = _epoch_ms_to_iso(ts_ms) if ts_ms else ""
    guid = f"pixel:{msg_id}"

    # Run on_receive hooks
    hook_msg = {
        "source": "pixel", "platform": "sms", "direction": direction,
        "contact_id": contact_id, "body": text, "phone": phone,
    }
    if not run_hooks("on_receive", hook_msg):
        log.info("Message dropped by on_receive hook: %s", text[:40])
        return

    with get_connection() as conn:
        added = add_message_with_guid(
            conn,
            contact_id=contact_id,
            platform="sms",
            direction=direction,
            body=text,
            sent_at=sent_at,
            external_guid=guid,
        )

    if added:
        log.info("Pixel MQTT: %s %s: %s", direction, phone, text[:40])
        _set_sync_state("pixel", str(ts_ms))
        if direction == "in":
            name = _contact_name(contact_id)
            _send_ntfy(name, text[:80])


# ── MQTT client ──────────────────────────────────────────

def _request_replay(client: mqtt.Client) -> None:
    """Ask Mini daemon to replay messages since our last sync point."""
    since = _get_sync_state("mini")
    since_ts = int(since) if since else 0
    payload = json.dumps({"since_apple_ts": since_ts})
    client.publish(TOPIC_REPLAY_REQUEST, payload, qos=1)
    log.info("Requested replay since apple_ts=%d", since_ts)


def _create_mqtt_client() -> mqtt.Client:
    """Create and configure the paho MQTT client."""
    client = mqtt.Client(
        mqtt.CallbackAPIVersion.VERSION2,
        client_id=MQTT_CLIENT_ID,
        clean_session=False,  # broker buffers QoS 1 messages while we're offline
    )

    tls_ctx = ssl.create_default_context(cafile=MQTT_CA)
    tls_ctx.check_hostname = False
    tls_ctx.verify_mode = ssl.CERT_REQUIRED
    client.tls_set_context(tls_ctx)

    def on_connect(c, userdata, flags, reason_code, properties):
        if reason_code == 0:
            log.info("MQTT connected (session_present=%s)", flags.session_present)
            c.subscribe(TOPIC_MINI_MESSAGES, qos=1)
            c.subscribe(TOPIC_PIXEL_MESSAGES, qos=1)
            # Mesh topics
            from .heartbeat import TOPIC_HEARTBEAT_WILDCARD
            from .mesh import TOPIC_ANNOUNCE
            c.subscribe(TOPIC_HEARTBEAT_WILDCARD, qos=0)
            c.subscribe(f"mesh/route/{NODE_ID}", qos=1)
            c.subscribe(TOPIC_ANNOUNCE, qos=1)
            # If broker lost our session, request replay to catch up
            if not flags.session_present:
                _request_replay(c)
            # Announce mesh join
            from .mesh import announce_join
            from .transport import get_registry
            announce_join(c, get_registry().available())
        else:
            log.warning("MQTT connect failed: %s", reason_code)

    def on_disconnect(c, userdata, flags, reason_code, properties):
        log.warning("MQTT disconnected: %s", reason_code)

    def on_mini(c, userdata, msg):
        try:
            _handle_mini_msg(msg.payload)
        except Exception:
            log.exception("Error handling Mini message")

    def on_pixel(c, userdata, msg):
        try:
            _handle_pixel_msg(msg.payload)
        except Exception:
            log.exception("Error handling Pixel message")

    def on_heartbeat(c, userdata, msg):
        try:
            from .heartbeat import handle_heartbeat
            handle_heartbeat(msg.payload)
        except Exception:
            log.debug("Error handling heartbeat", exc_info=True)

    def on_mesh_route(c, userdata, msg):
        try:
            from .mesh import handle_routed_message
            handle_routed_message(msg.payload)
        except Exception:
            log.exception("Error handling routed message")

    def on_mesh_announce(c, userdata, msg):
        try:
            data = json.loads(msg.payload)
            node = data.get("node", "?")
            event = data.get("event", "?")
            log.info("Mesh announce: %s %s", node, event)
        except Exception:
            pass

    client.on_connect = on_connect
    client.on_disconnect = on_disconnect
    client.message_callback_add(TOPIC_MINI_MESSAGES, on_mini)
    client.message_callback_add(TOPIC_PIXEL_MESSAGES, on_pixel)
    client.message_callback_add("mesh/heartbeat/+", on_heartbeat)
    client.message_callback_add(f"mesh/route/{NODE_ID}", on_mesh_route)
    client.message_callback_add("mesh/announce", on_mesh_announce)

    return client


# ── Main loop ─────────────────────────────────────────────

def run_forever(interval: float = POLL_INTERVAL) -> None:
    """Blocking loop — paho-mqtt handles reconnection internally."""
    log.info("Ingest daemon started (node=%s)", NODE_ID)

    # Initialize signing
    _get_private_key()
    _load_trusted_keys()

    client = _create_mqtt_client()

    while True:
        try:
            client.connect(MQTT_BROKER, MQTT_PORT, keepalive=60)

            # Start heartbeat publisher + stale sweeper
            from .transport import get_registry
            from .heartbeat import start_heartbeat_publisher, start_stale_sweeper
            start_heartbeat_publisher(client, transports=get_registry().available())
            start_stale_sweeper()

            client.loop_forever()
        except KeyboardInterrupt:
            log.info("Ingest daemon stopping")
            from .mesh import announce_leave
            try:
                announce_leave(client)
            except Exception:
                pass
            client.disconnect()
            return
        except Exception:
            log.exception("MQTT connection error, retrying in %ds", interval)
            time.sleep(interval)


def start_background(interval: float = POLL_INTERVAL) -> threading.Thread:
    """Start ingest in a daemon thread. Returns the thread."""
    t = threading.Thread(target=run_forever, args=(interval,), daemon=True)
    t.start()
    log.info("Ingest background thread started")
    return t
