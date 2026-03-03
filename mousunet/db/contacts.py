"""Contact CRUD and TSV import."""

import csv
import sqlite3
from pathlib import Path

from .connection import get_connection
from .models import Contact


def get_contact(conn: sqlite3.Connection, contact_id: int) -> Contact | None:
    row = conn.execute("SELECT * FROM contacts WHERE id = ?", (contact_id,)).fetchone()
    if row:
        return Contact(**dict(row))
    return None


def get_contact_by_name(conn: sqlite3.Connection, name: str) -> Contact | None:
    row = conn.execute(
        "SELECT * FROM contacts WHERE display_name = ? COLLATE NOCASE", (name,)
    ).fetchone()
    if row:
        return Contact(**dict(row))
    return None


def upsert_contact(conn: sqlite3.Connection, name: str, phone: str | None = None) -> int:
    """Insert or update a contact. Returns the contact id."""
    existing = get_contact_by_name(conn, name)
    if existing:
        if phone and phone != existing.phone:
            conn.execute("UPDATE contacts SET phone = ? WHERE id = ?", (phone, existing.id))
            conn.commit()
        return existing.id
    cur = conn.execute(
        "INSERT INTO contacts (display_name, phone) VALUES (?, ?)", (name, phone)
    )
    conn.commit()
    return cur.lastrowid


def search_contacts(conn: sqlite3.Connection, query: str) -> list[Contact]:
    """Search contacts by display_name or phone using LIKE %query%."""
    pattern = f"%{query}%"
    rows = conn.execute(
        "SELECT * FROM contacts WHERE display_name LIKE ? OR phone LIKE ? ORDER BY display_name",
        (pattern, pattern),
    ).fetchall()
    return [Contact(**dict(r)) for r in rows]


def all_contacts(conn: sqlite3.Connection) -> list[Contact]:
    rows = conn.execute("SELECT * FROM contacts ORDER BY display_name").fetchall()
    return [Contact(**dict(r)) for r in rows]


def import_tsv(path: Path) -> int:
    """Import contacts from a name\\tphone TSV file. Returns count imported."""
    count = 0
    with get_connection() as conn, open(path, newline="") as f:
        reader = csv.reader(f, delimiter="\t")
        for row in reader:
            if len(row) < 2:
                continue
            name, phone = row[0].strip(), row[1].strip()
            if not name or not phone:
                continue
            # Skip non-phone entries (service codes like *225#, 411, 611)
            if not phone.startswith("+"):
                continue
            upsert_contact(conn, name, phone)
            count += 1
    return count


def mark_viewed(conn: sqlite3.Connection, contact_id: int) -> None:
    """Update last_viewed_at to now for a contact."""
    conn.execute(
        "UPDATE contacts SET last_viewed_at = CURRENT_TIMESTAMP WHERE id = ?",
        (contact_id,),
    )
    conn.commit()
