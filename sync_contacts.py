#!/usr/bin/env python3
"""Sync contacts from Mac Mini's AddressBook (iCloud) into m0usunet's SQLite DB.

Runs on the Pi. SSHes to Mini, reads the AddressBook, and upserts into
m0usunet's contacts table. Also regenerates contacts.tsv for relay.sh.

Source of truth: Mini's AddressBook (synced from iPhone/Mac via iCloud).

Only updates contacts whose display_name is still a raw phone number
(i.e., never manually named). Pass --force to update all.
"""
import re
import sqlite3
import subprocess
import sys

SSH_HOST = "mini"
ADDRESSBOOK_DBS = [
    "/Users/owner/Library/Application Support/AddressBook/"
    "Sources/E70820C4-A42D-4321-A0F9-B329716F57DB/AddressBook-v22.abcddb",
]
M0USUNET_DB = "/home/jackpi5/m0usunet.db"

PHONE_RE = re.compile(r"^\+?\d[\d\s\-()]{6,}$")


def normalize_phone(raw: str) -> str:
    """Strip to digits and leading +."""
    digits = re.sub(r"[^\d+]", "", raw)
    # Ensure +1 prefix for 10-digit US numbers
    if len(digits) == 10 and not digits.startswith("+"):
        digits = "+1" + digits
    elif len(digits) == 11 and digits.startswith("1"):
        digits = "+" + digits
    return digits


def fetch_addressbook_contacts() -> list[dict]:
    """SSH to laptop and pull name+phone pairs from both AddressBook sources."""
    query = (
        "SELECT "
        "COALESCE(r.ZFIRSTNAME, '') || ' ' || COALESCE(r.ZLASTNAME, ''), "
        "p.ZFULLNUMBER "
        "FROM ZABCDRECORD r "
        "JOIN ZABCDPHONENUMBER p ON p.ZOWNER = r.Z_PK "
        "WHERE p.ZFULLNUMBER IS NOT NULL;"
    )
    contacts = []
    for db_path in ADDRESSBOOK_DBS:
        ssh_cmd = f'sqlite3 -separator "\t" "{db_path}"'
        result = subprocess.run(
            ["ssh", SSH_HOST, ssh_cmd],
            input=query,
            capture_output=True,
            text=True,
            timeout=15,
        )
        if result.returncode != 0:
            print(f"Warning: failed to read {db_path}: {result.stderr}", file=sys.stderr)
            continue

        for line in result.stdout.strip().split("\n"):
            if not line or "\t" not in line:
                continue
            name, phone = line.split("\t", 1)
            name = name.strip()
            if not name or not phone:
                continue
            contacts.append({"name": name, "phone": normalize_phone(phone)})
    return contacts


def sync(force: bool = False):
    """Update m0usunet contacts with AddressBook names, then regenerate contacts.tsv."""
    ab_contacts = fetch_addressbook_contacts()
    print(f"Fetched {len(ab_contacts)} contacts from Mini AddressBook")

    # Build phone → name lookup (first match wins)
    phone_to_name: dict[str, str] = {}
    for c in ab_contacts:
        if c["phone"] not in phone_to_name:
            phone_to_name[c["phone"]] = c["name"]

    conn = sqlite3.connect(M0USUNET_DB)
    conn.row_factory = sqlite3.Row
    rows = conn.execute("SELECT id, display_name, phone FROM contacts").fetchall()

    updated = 0
    for row in rows:
        cid, display_name, phone = row["id"], row["display_name"], row["phone"]
        if not phone:
            continue

        # Match by normalized phone
        ab_name = phone_to_name.get(phone)

        # Also try without +1 prefix
        if not ab_name and phone.startswith("+1"):
            ab_name = phone_to_name.get(phone[2:])

        if not ab_name:
            continue

        # Skip if already has a real name (unless --force)
        if not force and not PHONE_RE.match(display_name):
            continue

        if ab_name != display_name:
            conn.execute(
                "UPDATE contacts SET display_name = ? WHERE id = ?",
                (ab_name, cid),
            )
            print(f"  {phone}: {display_name!r} → {ab_name!r}")
            updated += 1

    conn.commit()

    # Regenerate contacts.tsv from the now-updated SQLite DB
    # This keeps relay.sh in sync without relay.sh needing to query SQLite
    all_rows = conn.execute(
        "SELECT display_name, phone FROM contacts WHERE phone IS NOT NULL ORDER BY display_name"
    ).fetchall()
    tsv_lines = []
    for r in all_rows:
        name, phone = r["display_name"], r["phone"]
        if phone:
            tsv_lines.append(f"{name}\t{phone}")
    tsv_path = M0USUNET_DB.replace("m0usunet.db", "contacts.tsv")
    with open(tsv_path, "w") as f:
        f.write("\n".join(tsv_lines) + "\n")
    print(f"Wrote {len(tsv_lines)} entries to {tsv_path}")

    conn.close()
    print(f"Updated {updated} contacts")


if __name__ == "__main__":
    force = "--force" in sys.argv
    sync(force=force)
