"""CLI entrypoint: daemon, import-contacts, seed, ingest, link, sources, merge, mesh, backup."""

import argparse
import json
import logging
import sys

from .constants import CONTACTS_TSV, DB_PATH, HOOKS_DIR, NODE_ID
from .db import (
    ensure_schema, get_connection, get_contact, get_contact_by_name,
    import_tsv, search_contacts, set_alias, upsert_contact,
    get_outbox_stats,
)


def cmd_daemon(args: argparse.Namespace) -> None:
    """Run ingest + scheduler as a headless daemon."""
    ensure_schema()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
    )
    log = logging.getLogger("m0usunet")

    # Initialize transports
    from .transport import get_registry, IMessageTransport, SMSTransport
    registry = get_registry()
    registry.register(IMessageTransport())
    registry.register(SMSTransport())
    log.info("Transports: %s", registry.all_names())

    # Ensure hooks directory exists
    for event in ("on_receive", "on_send"):
        (HOOKS_DIR / event).mkdir(parents=True, exist_ok=True)

    from .scheduler import start_background as start_scheduler
    start_scheduler(interval=30)
    from .ingest import run_forever
    interval = getattr(args, "interval", 30)
    run_forever(interval=interval)


def cmd_import_contacts(_args: argparse.Namespace) -> None:
    ensure_schema()
    if not CONTACTS_TSV.exists():
        print(f"contacts.tsv not found at {CONTACTS_TSV}", file=sys.stderr)
        sys.exit(1)
    count = import_tsv(CONTACTS_TSV)
    print(f"Imported {count} contacts into {DB_PATH}")


def cmd_seed(_args: argparse.Namespace) -> None:
    """Create fake conversations for testing."""
    ensure_schema()
    convos = [
        ("Mom", "+16099809954", "imessage", [
            ("in", "hey are you at work?"),
            ("out", "yeah on my way"),
            ("in", "ok be safe"),
        ]),
        ("Dad", "+16099809672", "sms", [
            ("in", "call me when you get a chance"),
            ("out", "will do"),
        ]),
        ("Beans", "+12676256627", "imessage", [
            ("out", "you coming out tonight?"),
            ("in", "maybe, what time"),
            ("out", "like 9"),
            ("in", "bet"),
        ]),
        ("Jace", "+12677343699", "imessage", [
            ("in", "yo"),
            ("out", "sup"),
            ("in", "nm just got off work"),
            ("out", "same"),
            ("in", "tryna play later?"),
        ]),
        ("Charlie", "+18563576381", "sms", [
            ("in", "hey man long time"),
            ("out", "for real whats good"),
        ]),
    ]

    with get_connection() as conn:
        from datetime import datetime, timedelta
        base = datetime.now() - timedelta(hours=len(convos))
        for ci, (name, phone, platform, msgs) in enumerate(convos):
            cid = upsert_contact(conn, name, phone)
            conv_start = base + timedelta(hours=ci)
            for mi, (direction, body) in enumerate(msgs):
                ts = conv_start + timedelta(minutes=mi)
                conn.execute(
                    "INSERT INTO messages (contact_id, platform, direction, body, "
                    "delivered, sent_at) VALUES (?, ?, ?, ?, 1, ?)",
                    (cid, platform, direction, body, ts.isoformat()),
                )
            conn.commit()

    print(f"Seeded {len(convos)} conversations into {DB_PATH}")


def cmd_contacts(args: argparse.Namespace) -> None:
    """List all contacts, optionally filtered by a search term."""
    ensure_schema()
    with get_connection() as conn:
        if args.query:
            contacts = search_contacts(conn, args.query)
        else:
            rows = conn.execute(
                "SELECT * FROM contacts ORDER BY display_name COLLATE NOCASE"
            ).fetchall()
            from .db import Contact
            contacts = [Contact(**dict(r)) for r in rows]

    if not contacts:
        print("No contacts found.")
        return

    for c in contacts:
        phone = c.phone or "-"
        alias = f" [{c.alias}]" if c.alias else ""
        print(f"  {c.id:4d}  {c.display_name:<25s} {phone}{alias}")
    print(f"\n{len(contacts)} contacts")


def cmd_ingest(args: argparse.Namespace) -> None:
    """Run ingest daemon (standalone polling loop)."""
    ensure_schema()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
    )
    from .ingest import run_forever
    interval = getattr(args, "interval", 30)
    run_forever(interval=interval)


def _find_contact(name: str):
    """Find a contact by name, exit if not found."""
    with get_connection() as conn:
        contact = get_contact_by_name(conn, name)
        if contact:
            return contact
        matches = search_contacts(conn, name)
        if len(matches) == 1:
            return matches[0]
        if matches:
            print(f"Ambiguous name '{name}'. Matches:", file=sys.stderr)
            for c in matches:
                print(f"  {c.id}: {c.display_name} ({c.phone})", file=sys.stderr)
            sys.exit(1)
    print(f"No contact found for '{name}'", file=sys.stderr)
    sys.exit(1)


def cmd_link(args: argparse.Namespace) -> None:
    """Link a platform identity to a contact."""
    ensure_schema()
    contact = _find_contact(args.contact)
    platform = args.platform
    platform_id = args.platform_id or ""
    profile_data = args.profile_data or ""

    with get_connection() as conn:
        existing = conn.execute(
            "SELECT id FROM contact_sources WHERE contact_id = ? AND platform = ? AND platform_id = ?",
            (contact.id, platform, platform_id),
        ).fetchone()
        if existing:
            print(f"Link already exists: {contact.display_name} <- {platform}:{platform_id}")
            return
        conn.execute(
            "INSERT INTO contact_sources (contact_id, platform, platform_id, profile_data) VALUES (?, ?, ?, ?)",
            (contact.id, platform, platform_id, profile_data),
        )
        conn.commit()
    print(f"Linked: {contact.display_name} <- {platform}:{platform_id}")


def cmd_sources(args: argparse.Namespace) -> None:
    """List all platform sources for a contact."""
    ensure_schema()
    contact = _find_contact(args.contact)

    with get_connection() as conn:
        rows = conn.execute(
            "SELECT platform, platform_id, profile_data, created_at FROM contact_sources "
            "WHERE contact_id = ? ORDER BY created_at",
            (contact.id,),
        ).fetchall()

    print(f"{contact.display_name} (id={contact.id}, phone={contact.phone})")
    if not rows:
        print("  (no linked sources)")
        return
    for r in rows:
        pid = r["platform_id"] or ""
        profile = r["profile_data"] or ""
        extra = f"  {profile}" if profile else ""
        print(f"  {r['platform']}:{pid}{extra}")


def cmd_merge(args: argparse.Namespace) -> None:
    """Merge one contact into another — reassign all messages and sources."""
    ensure_schema()
    source = _find_contact(args.from_contact)
    target = _find_contact(args.into_contact)

    if source.id == target.id:
        print("Cannot merge a contact into itself", file=sys.stderr)
        sys.exit(1)

    with get_connection() as conn:
        row = conn.execute(
            "SELECT COUNT(*) AS cnt FROM messages WHERE contact_id = ?", (source.id,)
        ).fetchone()
        msg_count = row["cnt"]

        conn.execute(
            "UPDATE messages SET contact_id = ? WHERE contact_id = ?",
            (target.id, source.id),
        )
        conn.execute(
            "UPDATE contact_sources SET contact_id = ? WHERE contact_id = ? "
            "AND NOT EXISTS (SELECT 1 FROM contact_sources cs2 "
            "WHERE cs2.contact_id = ? AND cs2.platform = contact_sources.platform "
            "AND cs2.platform_id = contact_sources.platform_id)",
            (target.id, source.id, target.id),
        )
        conn.execute("DELETE FROM contact_sources WHERE contact_id = ?", (source.id,))
        conn.execute("DELETE FROM contacts WHERE id = ?", (source.id,))
        conn.commit()

    print(f"Merged: {source.display_name} -> {target.display_name} ({msg_count} messages moved)")


def cmd_alias(args: argparse.Namespace) -> None:
    """Set or clear a contact alias (codename)."""
    ensure_schema()
    contact = _find_contact(args.contact)
    alias = args.alias if args.alias != "clear" else None

    with get_connection() as conn:
        set_alias(conn, contact.id, alias)

    if alias:
        print(f"Alias set: {contact.display_name} -> {alias}")
    else:
        print(f"Alias cleared for {contact.display_name}")


def cmd_mesh(args: argparse.Namespace) -> None:
    """Show mesh node status."""
    ensure_schema()
    from .heartbeat import get_mesh_status

    nodes = get_mesh_status()
    if not nodes:
        print(f"No mesh nodes seen yet. This node: {NODE_ID}")
        return

    print(f"MESH STATUS (self: {NODE_ID})")
    print(f"{'NODE':<16s} {'STATUS':<10s} {'UPTIME':<12s} {'QUEUE':<6s} {'VERSION':<10s} {'TRANSPORTS'}")
    print("-" * 72)
    for n in nodes:
        uptime = n.get("uptime_s", 0)
        if uptime > 86400:
            up_str = f"{uptime // 86400}d {(uptime % 86400) // 3600}h"
        elif uptime > 3600:
            up_str = f"{uptime // 3600}h {(uptime % 3600) // 60}m"
        else:
            up_str = f"{uptime // 60}m {uptime % 60}s"

        transports_raw = n.get("transports", "[]")
        try:
            transports = ", ".join(json.loads(transports_raw))
        except (json.JSONDecodeError, TypeError):
            transports = transports_raw

        status = n.get("status", "?")
        print(
            f"{n['node_id']:<16s} {status:<10s} {up_str:<12s} "
            f"{n.get('queue_depth', 0):<6d} {n.get('version', ''):<10s} {transports}"
        )


def cmd_outbox(args: argparse.Namespace) -> None:
    """Show outbox status and stats."""
    ensure_schema()
    with get_connection() as conn:
        stats = get_outbox_stats(conn)

        # Show pending items
        rows = conn.execute(
            "SELECT o.*, c.display_name FROM outbox o "
            "JOIN contacts c ON c.id = o.contact_id "
            "WHERE o.status IN ('queued', 'sending') "
            "ORDER BY o.priority DESC, o.created_at ASC LIMIT 20"
        ).fetchall()

    print("OUTBOX STATS")
    print(f"  queued:  {stats.get('queued', 0)}")
    print(f"  sending: {stats.get('sending', 0)}")
    print(f"  sent:    {stats.get('sent', 0)}")
    print(f"  failed:  {stats.get('failed', 0)}")

    if rows:
        print(f"\nPENDING ({len(rows)}):")
        for r in rows:
            r = dict(r)
            print(
                f"  #{r['id']} -> {r['display_name']} via {r['transport']} "
                f"[attempt {r['attempts']}/{r['max_attempts']}] "
                f"{r['body'][:50]}"
            )


def cmd_backup(args: argparse.Namespace) -> None:
    """Create a database backup."""
    ensure_schema()
    from .backup import local_backup, remote_backup

    if args.remote:
        ok = remote_backup(remote_host=args.remote)
        if ok:
            print(f"Remote backup pushed to {args.remote}")
        else:
            print("Remote backup failed", file=sys.stderr)
            sys.exit(1)
    else:
        path = local_backup(tag=args.tag or "manual")
        print(f"Backup created: {path}")


def cmd_keygen(args: argparse.Namespace) -> None:
    """Generate or display the node's Ed25519 keypair."""
    from .constants import NODE_KEY_PATH
    from .signing import load_private_key, get_public_key_pem

    private_key = load_private_key(NODE_KEY_PATH)
    pub_pem = get_public_key_pem(private_key).decode()

    print(f"Node ID:     {NODE_ID}")
    print(f"Key path:    {NODE_KEY_PATH}")
    print(f"Public key:\n{pub_pem}")

    if args.register:
        # Store our public key in mesh_nodes for other nodes to find
        with get_connection() as conn:
            conn.execute(
                "INSERT INTO mesh_nodes (node_id, public_key) VALUES (?, ?) "
                "ON CONFLICT(node_id) DO UPDATE SET public_key = excluded.public_key",
                (NODE_ID, pub_pem),
            )
            conn.commit()
        print(f"Public key registered in mesh_nodes for {NODE_ID}")


def main() -> None:
    parser = argparse.ArgumentParser(prog="m0usunet", description="Headless messaging daemon")
    sub = parser.add_subparsers(dest="command")

    daemon_parser = sub.add_parser("daemon", help="Run ingest + scheduler daemon (default)")
    daemon_parser.add_argument(
        "--interval", type=int, default=30,
        help="Poll interval in seconds (default: 30)",
    )

    sub.add_parser("import-contacts", help="Import from ~/contacts.tsv")
    sub.add_parser("seed", help="Create test conversations")

    contacts_parser = sub.add_parser("contacts", help="List all contacts")
    contacts_parser.add_argument("query", nargs="?", default="", help="Optional search term")

    ingest_parser = sub.add_parser("ingest", help="Run ingest only (no scheduler)")
    ingest_parser.add_argument(
        "--interval", type=int, default=30,
        help="Poll interval in seconds (default: 30)",
    )

    link_parser = sub.add_parser("link", help="Link a platform identity to a contact")
    link_parser.add_argument("contact", help="Contact name")
    link_parser.add_argument("platform", help="Platform (bumble, hinge, tinder, imessage, sms, etc)")
    link_parser.add_argument("platform_id", nargs="?", default="", help="Platform-specific ID or username")
    link_parser.add_argument("--profile", dest="profile_data", default="", help="Extra profile data")

    sources_parser = sub.add_parser("sources", help="List platform sources for a contact")
    sources_parser.add_argument("contact", help="Contact name")

    merge_parser = sub.add_parser("merge", help="Merge one contact into another")
    merge_parser.add_argument("from_contact", help="Contact to merge FROM (will be deleted)")
    merge_parser.add_argument("into_contact", help="Contact to merge INTO (keeps this one)")

    alias_parser = sub.add_parser("alias", help="Set a contact alias (codename)")
    alias_parser.add_argument("contact", help="Contact name")
    alias_parser.add_argument("alias", help="Alias to set (use 'clear' to remove)")

    sub.add_parser("mesh", help="Show mesh node status")

    outbox_parser = sub.add_parser("outbox", help="Show outbox status")

    backup_parser = sub.add_parser("backup", help="Create a database backup")
    backup_parser.add_argument("--remote", help="SCP to remote host (e.g. 'mini')")
    backup_parser.add_argument("--tag", default="", help="Backup tag/label")

    keygen_parser = sub.add_parser("keygen", help="Generate/display Ed25519 node key")
    keygen_parser.add_argument("--register", action="store_true", help="Register public key in mesh_nodes")

    args = parser.parse_args()

    if args.command is None:
        parser.print_help()
        sys.exit(0)

    commands = {
        "daemon": cmd_daemon,
        "import-contacts": cmd_import_contacts,
        "seed": cmd_seed,
        "contacts": cmd_contacts,
        "ingest": cmd_ingest,
        "link": cmd_link,
        "sources": cmd_sources,
        "merge": cmd_merge,
        "alias": cmd_alias,
        "mesh": cmd_mesh,
        "outbox": cmd_outbox,
        "backup": cmd_backup,
        "keygen": cmd_keygen,
    }

    handler = commands.get(args.command)
    if handler:
        handler(args)
