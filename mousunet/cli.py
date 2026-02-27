"""CLI entrypoint: run (default), import-contacts, seed, ingest, link, sources, merge."""

import argparse
import logging
import sys

from .constants import CONTACTS_TSV, DB_PATH
from .db.connection import ensure_schema, get_connection
from .db.contacts import import_tsv, upsert_contact, get_contact_by_name, search_contacts


def cmd_run(_args: argparse.Namespace) -> None:
    ensure_schema()
    from .tui.app import MousuNetApp
    app = MousuNetApp()
    app.run()


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


def cmd_ingest(args: argparse.Namespace) -> None:
    """Run ingest daemon (standalone polling loop)."""
    ensure_schema()
    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(name)s %(levelname)s %(message)s",
        datefmt="%H:%M:%S",
    )
    from .ingest.poller import run_forever
    interval = getattr(args, "interval", 30)
    run_forever(interval=interval)


def _find_contact(name: str):
    """Find a contact by name, exit if not found."""
    with get_connection() as conn:
        contact = get_contact_by_name(conn, name)
        if contact:
            return contact
        # Try fuzzy search
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
        # Check for existing link
        existing = conn.execute(
            "SELECT id FROM contact_sources WHERE contact_id = ? AND platform = ? AND platform_id = ?",
            (contact.id, platform, platform_id),
        ).fetchone()
        if existing:
            print(f"Link already exists: {contact.display_name} ← {platform}:{platform_id}")
            return
        conn.execute(
            "INSERT INTO contact_sources (contact_id, platform, platform_id, profile_data) VALUES (?, ?, ?, ?)",
            (contact.id, platform, platform_id, profile_data),
        )
        conn.commit()
    print(f"Linked: {contact.display_name} ← {platform}:{platform_id}")


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
        # Count messages to move
        row = conn.execute(
            "SELECT COUNT(*) AS cnt FROM messages WHERE contact_id = ?", (source.id,)
        ).fetchone()
        msg_count = row["cnt"]

        # Reassign messages
        conn.execute(
            "UPDATE messages SET contact_id = ? WHERE contact_id = ?",
            (target.id, source.id),
        )
        # Move sources (skip duplicates)
        conn.execute(
            "UPDATE contact_sources SET contact_id = ? WHERE contact_id = ? "
            "AND NOT EXISTS (SELECT 1 FROM contact_sources cs2 "
            "WHERE cs2.contact_id = ? AND cs2.platform = contact_sources.platform "
            "AND cs2.platform_id = contact_sources.platform_id)",
            (target.id, source.id, target.id),
        )
        # Delete remaining duplicate sources
        conn.execute("DELETE FROM contact_sources WHERE contact_id = ?", (source.id,))
        # Delete the empty contact
        conn.execute("DELETE FROM contacts WHERE id = ?", (source.id,))
        conn.commit()

    print(f"Merged: {source.display_name} → {target.display_name} ({msg_count} messages moved)")


def main() -> None:
    parser = argparse.ArgumentParser(prog="mousunet", description="Unified messaging TUI")
    sub = parser.add_subparsers(dest="command")

    sub.add_parser("run", help="Launch TUI (default)")
    sub.add_parser("import-contacts", help="Import from ~/contacts.tsv")
    sub.add_parser("seed", help="Create test conversations")

    ingest_parser = sub.add_parser("ingest", help="Run ingest daemon")
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

    args = parser.parse_args()

    if args.command is None or args.command == "run":
        cmd_run(args)
    elif args.command == "import-contacts":
        cmd_import_contacts(args)
    elif args.command == "seed":
        cmd_seed(args)
    elif args.command == "ingest":
        cmd_ingest(args)
    elif args.command == "link":
        cmd_link(args)
    elif args.command == "sources":
        cmd_sources(args)
    elif args.command == "merge":
        cmd_merge(args)
