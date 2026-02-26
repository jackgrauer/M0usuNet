"""CLI entrypoint: run (default), import-contacts, seed, ingest."""

import argparse
import logging
import sys

from .constants import CONTACTS_TSV, DB_PATH
from .db.connection import ensure_schema, get_connection
from .db.contacts import import_tsv, upsert_contact
from .db.messages import add_message


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

    args = parser.parse_args()

    if args.command is None or args.command == "run":
        cmd_run(args)
    elif args.command == "import-contacts":
        cmd_import_contacts(args)
    elif args.command == "seed":
        cmd_seed(args)
    elif args.command == "ingest":
        cmd_ingest(args)
