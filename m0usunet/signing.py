"""Ed25519 message signing and verification for mesh trust."""

import base64
import json
import logging
import time
from pathlib import Path

from cryptography.hazmat.primitives.asymmetric.ed25519 import (
    Ed25519PrivateKey,
    Ed25519PublicKey,
)
from cryptography.hazmat.primitives import serialization
from cryptography.exceptions import InvalidSignature

log = logging.getLogger(__name__)


def generate_keypair(key_path: Path) -> Ed25519PrivateKey:
    """Generate a new Ed25519 keypair and save to disk."""
    private_key = Ed25519PrivateKey.generate()
    key_path.parent.mkdir(parents=True, exist_ok=True)
    key_path.write_bytes(
        private_key.private_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PrivateFormat.PKCS8,
            encryption_algorithm=serialization.NoEncryption(),
        )
    )
    key_path.chmod(0o600)
    # Write public key alongside
    pub_path = key_path.with_suffix(".pub")
    pub_path.write_bytes(
        private_key.public_key().public_bytes(
            encoding=serialization.Encoding.PEM,
            format=serialization.PublicFormat.SubjectPublicKeyInfo,
        )
    )
    log.info("Generated Ed25519 keypair: %s", key_path)
    return private_key


def load_private_key(key_path: Path) -> Ed25519PrivateKey:
    """Load or generate the node's private key."""
    if not key_path.exists():
        return generate_keypair(key_path)
    return serialization.load_pem_private_key(key_path.read_bytes(), password=None)


def load_public_key(pem_bytes: bytes) -> Ed25519PublicKey:
    """Load a public key from PEM bytes."""
    return serialization.load_pem_public_key(pem_bytes)


def get_public_key_pem(private_key: Ed25519PrivateKey) -> bytes:
    """Export the public key as PEM bytes."""
    return private_key.public_key().public_bytes(
        encoding=serialization.Encoding.PEM,
        format=serialization.PublicFormat.SubjectPublicKeyInfo,
    )


def sign_envelope(
    node_id: str,
    payload: dict,
    private_key: Ed25519PrivateKey,
) -> bytes:
    """Wrap a payload in a signed envelope. Returns JSON bytes."""
    payload_bytes = json.dumps(payload, separators=(",", ":")).encode()
    signature = private_key.sign(payload_bytes)
    envelope = {
        "node": node_id,
        "sig": base64.b64encode(signature).decode(),
        "ts": int(time.time()),
        "payload": payload,
    }
    return json.dumps(envelope, separators=(",", ":")).encode()


def verify_envelope(
    envelope_bytes: bytes,
    trusted_keys: dict[str, Ed25519PublicKey],
) -> dict | None:
    """Verify a signed envelope. Returns the payload if valid, None if not.

    Args:
        envelope_bytes: Raw JSON envelope.
        trusted_keys: node_id -> public key mapping.

    Returns:
        The inner payload dict if signature is valid, None otherwise.
    """
    try:
        envelope = json.loads(envelope_bytes)
    except (json.JSONDecodeError, ValueError):
        log.warning("Envelope parse failed")
        return None

    # Allow unsigned messages (backward compat) — just return payload directly
    if "sig" not in envelope:
        return envelope

    node_id = envelope.get("node", "")
    sig_b64 = envelope.get("sig", "")
    payload = envelope.get("payload")

    if not node_id or not sig_b64 or payload is None:
        log.warning("Malformed envelope from node=%s", node_id)
        return None

    pub_key = trusted_keys.get(node_id)
    if pub_key is None:
        # Unknown node — accept but warn (allow bootstrapping)
        log.warning("No trusted key for node=%s, accepting unsigned", node_id)
        return payload

    try:
        sig = base64.b64decode(sig_b64)
        payload_bytes = json.dumps(payload, separators=(",", ":")).encode()
        pub_key.verify(sig, payload_bytes)
        return payload
    except (InvalidSignature, Exception) as e:
        log.warning("Bad signature from node=%s: %s", node_id, e)
        return None
