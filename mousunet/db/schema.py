"""DDL and versioned migrations."""

MIGRATIONS: list[tuple[int, str]] = [
    (1, """
        CREATE TABLE IF NOT EXISTS contacts (
            id INTEGER PRIMARY KEY,
            display_name TEXT NOT NULL,
            phone TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        CREATE TABLE IF NOT EXISTS contact_sources (
            id INTEGER PRIMARY KEY,
            contact_id INTEGER NOT NULL REFERENCES contacts(id),
            platform TEXT NOT NULL,
            platform_id TEXT,
            profile_data TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        CREATE INDEX IF NOT EXISTS idx_contact_sources_contact
            ON contact_sources(contact_id);

        CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY,
            contact_id INTEGER NOT NULL REFERENCES contacts(id),
            platform TEXT NOT NULL,
            direction TEXT NOT NULL CHECK (direction IN ('in', 'out')),
            body TEXT NOT NULL,
            sent_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            delivered INTEGER DEFAULT 0,
            relay_output TEXT
        );
        CREATE INDEX IF NOT EXISTS idx_messages_contact_time
            ON messages(contact_id, sent_at DESC);

        CREATE TABLE IF NOT EXISTS suggestions (
            id INTEGER PRIMARY KEY,
            message_id INTEGER NOT NULL REFERENCES messages(id),
            suggested_text TEXT,
            used INTEGER DEFAULT 0,
            edited_text TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
    """),
    (2, """
        CREATE TABLE IF NOT EXISTS sync_state (
            source TEXT PRIMARY KEY,
            last_synced_value TEXT NOT NULL,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );

        ALTER TABLE messages ADD COLUMN external_guid TEXT;
        CREATE UNIQUE INDEX IF NOT EXISTS idx_messages_external_guid
            ON messages(external_guid) WHERE external_guid IS NOT NULL;
    """),
    (3, """
        ALTER TABLE contacts ADD COLUMN last_viewed_at TIMESTAMP;
    """),
]
