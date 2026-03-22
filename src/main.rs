//! m0usurouter — message router for the m0usunet mesh network.
//!
//! Daemon mode: subscribes to MQTT topics for incoming iMessage/SMS,
//! writes them to the m0usunet SQLite database.
//! Also runs the scheduled task executor (reminders, timed sends, recurring commands).
//!
//! Send mode: routes an outbound message via direct ADB SMS or
//! MQTT command to the Mac Mini iMessage relay.

mod chat;

use clap::{Parser, Subcommand};
use rumqttc::{AsyncClient, Event, MqttOptions, Packet, QoS};
use rusqlite::Connection;
use serde::{Deserialize, Serialize};
use std::collections::HashMap;
use std::path::{Path, PathBuf};
use std::process::Command;
use std::sync::{Arc, Mutex};
use std::time::Duration;
use tokio::io::{AsyncBufReadExt, AsyncWriteExt, BufReader};
use tokio::net::UnixListener;
use tokio::sync::RwLock;
use tracing::{error, info, warn};

// ── Config ──────────────────────────────────────────────

const MQTT_HOST: &str = "127.0.0.1";
const MQTT_PORT: u16 = 1883;
const PIXEL_SERIAL: &str = "192.168.0.22:5555";
const SOCKET_PATH: &str = "/tmp/m0usurouter.sock";
pub const JACK_PHONE: &str = "8563043698";

pub fn home_dir() -> PathBuf {
    PathBuf::from(std::env::var("HOME").unwrap_or_else(|_| "/home/jackpi5".into()))
}

fn contacts_path() -> PathBuf {
    home_dir().join("contacts.tsv")
}

fn cache_path() -> PathBuf {
    home_dir().join("service-cache.tsv")
}

fn log_path() -> PathBuf {
    home_dir().join("messages.log")
}

pub fn db_path() -> PathBuf {
    home_dir().join("m0usunet.db")
}

// ── CLI ─────────────────────────────────────────────────

#[derive(Parser)]
#[command(name = "m0usurouter", about = "m0usunet message router")]
struct Cli {
    #[command(subcommand)]
    command: Cmd,
}

#[derive(Subcommand)]
enum Cmd {
    /// Run the MQTT ingest daemon
    Daemon,
    /// Open the chat TUI (talk to m0usunet AI)
    Chat,
    /// Send a message (auto-routes iMessage vs SMS)
    Send {
        /// Contact name or phone number
        recipient: String,
        /// Message text
        #[arg(trailing_var_arg = true, num_args = 1..)]
        message: Vec<String>,
    },
}

// ── MQTT message payloads ───────────────────────────────

#[derive(Deserialize, Debug)]
struct IMessagePayload {
    handle_id: Option<String>,
    text: Option<String>,
    #[serde(default)]
    is_from_me: bool,
    guid: Option<String>,
}

#[derive(Deserialize, Debug)]
struct SmsPayload {
    phone: Option<String>,
    text: Option<String>,
    #[serde(default)]
    is_from_me: bool,
    guid: Option<String>,
}

#[derive(Serialize)]
struct SendCmd {
    number: String,
    message: String,
}

#[derive(Deserialize)]
struct SocketRequest {
    recipient: String,
    message: String,
}

#[derive(Serialize)]
struct SocketResponse {
    ok: bool,
    result: String,
}

// ── SQLite database ─────────────────────────────────────

const MIGRATIONS: &[(i64, &str)] = &[
    (1, "
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
    "),
    (2, "
        CREATE TABLE IF NOT EXISTS sync_state (
            source TEXT PRIMARY KEY,
            last_synced_value TEXT NOT NULL,
            updated_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        ALTER TABLE messages ADD COLUMN external_guid TEXT;
        CREATE UNIQUE INDEX IF NOT EXISTS idx_messages_external_guid
            ON messages(external_guid) WHERE external_guid IS NOT NULL;
    "),
    (3, "ALTER TABLE contacts ADD COLUMN last_viewed_at TIMESTAMP;"),
    (4, "ALTER TABLE contacts ADD COLUMN pinned INTEGER DEFAULT 0;"),
    (5, "
        CREATE TABLE attachments (
            id INTEGER PRIMARY KEY,
            message_id INTEGER NOT NULL REFERENCES messages(id) ON DELETE CASCADE,
            filename TEXT NOT NULL,
            mime_type TEXT,
            total_bytes INTEGER DEFAULT 0,
            local_path TEXT,
            remote_path TEXT,
            download_status TEXT DEFAULT 'pending'
                CHECK (download_status IN ('pending','downloading','done','failed')),
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        );
        CREATE INDEX idx_attachments_message ON attachments(message_id);
        ALTER TABLE messages ADD COLUMN has_attachments INTEGER DEFAULT 0;
    "),
    (6, "
        CREATE TABLE scheduled_messages (
            id INTEGER PRIMARY KEY,
            contact_id INTEGER NOT NULL REFERENCES contacts(id),
            body TEXT NOT NULL,
            scheduled_at TEXT NOT NULL,
            status TEXT DEFAULT 'pending'
                CHECK (status IN ('pending','sent','failed','cancelled')),
            attempts INTEGER DEFAULT 0,
            last_error TEXT,
            created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            sent_at TIMESTAMP
        );
        CREATE INDEX idx_scheduled_pending
            ON scheduled_messages(status, scheduled_at)
            WHERE status = 'pending';
    "),
    (7, "ALTER TABLE contacts ADD COLUMN muted INTEGER DEFAULT 0;"),
    (8, "
        CREATE TABLE outbox (
            id INTEGER PRIMARY KEY,
            contact_id INTEGER NOT NULL REFERENCES contacts(id),
            body TEXT NOT NULL,
            transport TEXT NOT NULL DEFAULT 'relay',
            priority INTEGER DEFAULT 0,
            attempts INTEGER DEFAULT 0,
            max_attempts INTEGER DEFAULT 5,
            next_retry_at TEXT DEFAULT (datetime('now')),
            backoff_s INTEGER DEFAULT 30,
            status TEXT DEFAULT 'queued'
                CHECK (status IN ('queued','sending','sent','failed','cancelled')),
            error TEXT,
            created_at TEXT DEFAULT (datetime('now')),
            sent_at TEXT,
            relay_output TEXT
        );
        CREATE INDEX idx_outbox_pending
            ON outbox(status, next_retry_at)
            WHERE status IN ('queued', 'sending');
        CREATE TABLE mesh_nodes (
            node_id TEXT PRIMARY KEY,
            status TEXT DEFAULT 'online'
                CHECK (status IN ('online','degraded','offline')),
            last_seen TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
            uptime_s INTEGER DEFAULT 0,
            version TEXT DEFAULT '',
            transports TEXT DEFAULT '[]',
            queue_depth INTEGER DEFAULT 0,
            public_key TEXT
        );
        ALTER TABLE contacts ADD COLUMN alias TEXT;
    "),
    (9, "
        CREATE VIRTUAL TABLE IF NOT EXISTS messages_fts USING fts5(
            body,
            content='messages',
            content_rowid='id',
            tokenize='porter unicode61'
        );
        INSERT OR IGNORE INTO messages_fts(rowid, body)
            SELECT id, body FROM messages WHERE body IS NOT NULL;
        CREATE TRIGGER IF NOT EXISTS messages_fts_insert AFTER INSERT ON messages BEGIN
            INSERT INTO messages_fts(rowid, body) VALUES (new.id, new.body);
        END;
        CREATE TRIGGER IF NOT EXISTS messages_fts_delete AFTER DELETE ON messages BEGIN
            INSERT INTO messages_fts(messages_fts, rowid, body) VALUES('delete', old.id, old.body);
        END;
        CREATE TRIGGER IF NOT EXISTS messages_fts_update AFTER UPDATE OF body ON messages BEGIN
            INSERT INTO messages_fts(messages_fts, rowid, body) VALUES('delete', old.id, old.body);
            INSERT INTO messages_fts(rowid, body) VALUES (new.id, new.body);
        END;
    "),
    (10, "
        CREATE TABLE IF NOT EXISTS scheduled_tasks (
            id INTEGER PRIMARY KEY,
            description TEXT NOT NULL,
            action_type TEXT NOT NULL
                CHECK (action_type IN ('send_message', 'run_command', 'remind')),
            action_params TEXT NOT NULL,
            scheduled_at TEXT NOT NULL,
            recurrence TEXT,
            status TEXT DEFAULT 'pending'
                CHECK (status IN ('pending', 'running', 'done', 'failed', 'cancelled')),
            created_at TEXT DEFAULT (datetime('now')),
            last_run_at TEXT,
            last_result TEXT,
            run_count INTEGER DEFAULT 0,
            max_runs INTEGER
        );
        CREATE INDEX IF NOT EXISTS idx_scheduled_tasks_pending
            ON scheduled_tasks(status, scheduled_at);
        CREATE TABLE IF NOT EXISTS bot_memory (
            id INTEGER PRIMARY KEY,
            key TEXT NOT NULL,
            content TEXT NOT NULL,
            category TEXT DEFAULT 'general'
                CHECK (category IN ('preference', 'fact', 'instruction', 'context', 'general')),
            created_at TEXT DEFAULT (datetime('now')),
            updated_at TEXT DEFAULT (datetime('now')),
            active INTEGER DEFAULT 1
        );
        CREATE UNIQUE INDEX IF NOT EXISTS idx_bot_memory_key
            ON bot_memory(key) WHERE active = 1;
    "),
];

pub fn open_db() -> anyhow::Result<Connection> {
    let path = db_path();
    info!("opening database: {}", path.display());
    let conn = Connection::open(&path)?;
    conn.execute_batch(
        "PRAGMA journal_mode = WAL;
         PRAGMA foreign_keys = ON;
         PRAGMA busy_timeout = 30000;
         PRAGMA mmap_size = 268435456;"
    )?;
    ensure_schema(&conn)?;
    Ok(conn)
}

fn ensure_schema(conn: &Connection) -> anyhow::Result<()> {
    conn.execute_batch(
        "CREATE TABLE IF NOT EXISTS schema_version (
            version INTEGER PRIMARY KEY,
            applied_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
        )"
    )?;
    let current: i64 = conn.query_row(
        "SELECT COALESCE(MAX(version), 0) FROM schema_version", [], |r| r.get(0)
    )?;

    for (version, sql) in MIGRATIONS {
        if *version > current {
            info!("running migration {}", version);
            conn.execute_batch(sql)?;
            conn.execute(
                "INSERT INTO schema_version (version) VALUES (?)",
                [version],
            )?;
        }
    }
    Ok(())
}

pub fn utc_now() -> String {
    // Format as ISO 8601 UTC without external deps
    let now = std::time::SystemTime::now()
        .duration_since(std::time::UNIX_EPOCH)
        .unwrap_or_default()
        .as_secs();
    // Convert epoch to datetime string
    let secs_per_day: u64 = 86400;
    let days = now / secs_per_day;
    let remaining = now % secs_per_day;
    let hours = remaining / 3600;
    let minutes = (remaining % 3600) / 60;
    let seconds = remaining % 60;

    // Days since epoch to Y-M-D (simplified Gregorian)
    let mut y: i64 = 1970;
    let mut d = days as i64;
    loop {
        let days_in_year = if y % 4 == 0 && (y % 100 != 0 || y % 400 == 0) { 366 } else { 365 };
        if d < days_in_year { break; }
        d -= days_in_year;
        y += 1;
    }
    let leap = y % 4 == 0 && (y % 100 != 0 || y % 400 == 0);
    let month_days: [i64; 12] = [31, if leap { 29 } else { 28 }, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31];
    let mut m: usize = 0;
    while m < 12 && d >= month_days[m] {
        d -= month_days[m];
        m += 1;
    }
    format!("{:04}-{:02}-{:02} {:02}:{:02}:{:02}", y, m + 1, d + 1, hours, minutes, seconds)
}

/// Find or create a contact by phone number. Returns contact_id.
fn upsert_contact_by_phone(
    conn: &Connection,
    phone: &str,
    display_name: &str,
) -> anyhow::Result<i64> {
    // Try exact phone match first
    let existing: Option<i64> = conn
        .query_row(
            "SELECT id FROM contacts WHERE phone = ?",
            [phone],
            |r| r.get(0),
        )
        .ok();

    if let Some(id) = existing {
        return Ok(id);
    }

    // Try name match (for contacts added manually without phone)
    let by_name: Option<i64> = conn
        .query_row(
            "SELECT id FROM contacts WHERE display_name = ? COLLATE NOCASE",
            [display_name],
            |r| r.get(0),
        )
        .ok();

    if let Some(id) = by_name {
        // Stamp the phone if missing
        conn.execute(
            "UPDATE contacts SET phone = ? WHERE id = ? AND phone IS NULL",
            rusqlite::params![phone, id],
        )?;
        return Ok(id);
    }

    // Create new contact
    conn.execute(
        "INSERT INTO contacts (display_name, phone) VALUES (?, ?)",
        rusqlite::params![display_name, phone],
    )?;
    Ok(conn.last_insert_rowid())
}

/// Insert a message with GUID dedup. Returns true if inserted.
fn insert_message(
    conn: &Connection,
    contact_id: i64,
    platform: &str,
    direction: &str,
    body: &str,
    guid: Option<&str>,
) -> anyhow::Result<bool> {
    // GUID dedup
    if let Some(guid) = guid {
        let exists: bool = conn.query_row(
            "SELECT EXISTS(SELECT 1 FROM messages WHERE external_guid = ?)",
            [guid],
            |r| r.get(0),
        )?;
        if exists {
            return Ok(false);
        }
    }

    // Fuzzy dedup for outbound: same contact, same body prefix, within 2 min
    if direction == "out" {
        let now = utc_now();
        let prefix = &body[..body.len().min(50)];
        let fuzzy: Option<i64> = conn
            .query_row(
                "SELECT id FROM messages \
                 WHERE contact_id = ? AND direction = 'out' \
                 AND SUBSTR(body, 1, 50) = ? \
                 AND ABS(CAST((julianday(sent_at) - julianday(?)) * 86400 AS INTEGER)) < 120",
                rusqlite::params![contact_id, prefix, now],
                |r| r.get(0),
            )
            .ok();
        if let Some(existing_id) = fuzzy {
            if let Some(guid) = guid {
                conn.execute(
                    "UPDATE messages SET external_guid = ? WHERE id = ? AND external_guid IS NULL",
                    rusqlite::params![guid, existing_id],
                )?;
            }
            return Ok(false);
        }
    }

    let now = utc_now();
    conn.execute(
        "INSERT INTO messages (contact_id, platform, direction, body, delivered, sent_at, external_guid) \
         VALUES (?, ?, ?, ?, 1, ?, ?)",
        rusqlite::params![contact_id, platform, direction, body, now, guid],
    )?;
    Ok(true)
}

// ── Contacts ────────────────────────────────────────────

fn load_contacts(path: &Path) -> HashMap<String, String> {
    let mut map = HashMap::new();
    let Ok(content) = std::fs::read_to_string(path) else {
        return map;
    };
    for line in content.lines() {
        let parts: Vec<&str> = line.splitn(2, '\t').collect();
        if parts.len() == 2 {
            let name = parts[0].trim();
            let number = parts[1].trim();
            if !name.is_empty() && !number.is_empty() {
                map.insert(name.to_lowercase(), number.to_string());
                map.insert(number.to_string(), number.to_string());
            }
        }
    }
    map
}

fn load_reverse_contacts(path: &Path) -> HashMap<String, String> {
    let mut map = HashMap::new();
    let Ok(content) = std::fs::read_to_string(path) else {
        return map;
    };
    for line in content.lines() {
        let parts: Vec<&str> = line.splitn(2, '\t').collect();
        if parts.len() == 2 {
            let name = parts[0].trim();
            let number = normalize_number(parts[1].trim());
            if !name.is_empty() && !number.is_empty() && !name.starts_with('+') {
                map.insert(number, name.to_string());
            }
        }
    }
    map
}

fn normalize_number(n: &str) -> String {
    let digits: String = n.chars().filter(|c| c.is_ascii_digit()).collect();
    if digits.len() == 10 {
        format!("+1{digits}")
    } else if digits.len() == 11 && digits.starts_with('1') {
        format!("+{digits}")
    } else if n.starts_with('+') {
        n.to_string()
    } else {
        n.to_string()
    }
}

fn resolve_contact(contacts: &HashMap<String, String>, input: &str) -> (String, String) {
    let is_number = input
        .trim_start_matches('+')
        .chars()
        .all(|c| c.is_ascii_digit())
        && input.len() >= 7;

    if is_number {
        let num = normalize_number(input);
        return (num.clone(), num);
    }

    let lower = input.to_lowercase();
    for (key, number) in contacts {
        if key.contains(&lower) {
            return (input.to_string(), number.clone());
        }
    }

    (input.to_string(), input.to_string())
}

fn reverse_lookup(reverse: &HashMap<String, String>, phone: &str) -> String {
    let norm = normalize_number(phone);
    reverse
        .get(&norm)
        .cloned()
        .unwrap_or_else(|| phone.to_string())
}

// ── Service cache ───────────────────────────────────────

fn load_service_cache(path: &Path) -> HashMap<String, String> {
    let mut map = HashMap::new();
    let Ok(content) = std::fs::read_to_string(path) else {
        return map;
    };
    for line in content.lines() {
        let parts: Vec<&str> = line.splitn(2, '\t').collect();
        if parts.len() == 2 {
            map.insert(parts[0].to_string(), parts[1].to_string());
        }
    }
    map
}

fn get_service_type(cache: &HashMap<String, String>, number: &str) -> String {
    if let Some(svc) = cache.get(number) {
        return svc.clone();
    }

    let output = Command::new("/home/jackpi5/imcheck.sh")
        .arg(number)
        .output();

    match output {
        Ok(o) if o.status.success() => {
            let svc = String::from_utf8_lossy(&o.stdout).trim().to_string();
            if svc == "imessage" || svc == "sms" {
                let line = format!("{}\t{}\n", number, svc);
                let _ = std::fs::OpenOptions::new()
                    .create(true)
                    .append(true)
                    .open(cache_path())
                    .and_then(|mut f| std::io::Write::write_all(&mut f, line.as_bytes()));
                return svc;
            }
            "unknown".into()
        }
        _ => "unknown".into(),
    }
}

// ── Message sending ─────────────────────────────────────

fn send_sms_via_adb(serial: &str, number: &str, message: &str) -> bool {
    let _ = Command::new("adb")
        .args(["connect", serial])
        .output();

    let cmd = format!(
        "export PATH=/data/data/com.termux/files/usr/bin:$PATH \
         LD_LIBRARY_PATH=/data/data/com.termux/files/usr/lib; \
         termux-sms-send -n '{}' '{}'",
        number.replace('\'', "'\\''"),
        message.replace('\'', "'\\''"),
    );
    let r = Command::new("adb")
        .args(["-s", serial, "shell", "su", "-c", &cmd])
        .output();

    matches!(r, Ok(o) if o.status.success())
}

fn send_via_mqtt_sync(topic: &str, payload: &str) -> bool {
    let r = Command::new("mosquitto_pub")
        .args(["-h", MQTT_HOST, "-p", "1883", "-t", topic, "-m", payload])
        .output();
    matches!(r, Ok(o) if o.status.success())
}

fn append_log(label: &str, via: &str, message: &str) {
    let ts = utc_now();
    let line = format!("{} | {} | {} | {}\n", ts, via, label, message);
    let _ = std::fs::OpenOptions::new()
        .create(true)
        .append(true)
        .open(log_path())
        .and_then(|mut f| std::io::Write::write_all(&mut f, line.as_bytes()));
}

pub fn do_send(recipient: &str, message: &str) -> String {
    let contacts = load_contacts(&contacts_path());
    let cache = load_service_cache(&cache_path());
    let (name, number) = resolve_contact(&contacts, recipient);
    let label = if name != number {
        format!("{} ({})", name, number)
    } else {
        number.clone()
    };

    let service = get_service_type(&cache, &number);

    if service == "sms" {
        if send_sms_via_adb(PIXEL_SERIAL, &number, message) {
            append_log(&label, "SMS (ADB)", message);
            return format!("SMS (ADB) -> {}: {}", label, message);
        }
        let payload = serde_json::to_string(&SendCmd {
            number: number.clone(),
            message: message.to_string(),
        }).unwrap_or_default();
        if send_via_mqtt_sync("cmd/pixel/sms/send", &payload) {
            append_log(&label, "SMS (MQTT)", message);
            return format!("SMS (MQTT) -> {}: {}", label, message);
        }
        append_log(&label, "FAILED", message);
        return format!("FAILED to send SMS to {}", label);
    }

    if service == "imessage" {
        let payload = serde_json::to_string(&SendCmd {
            number: number.clone(),
            message: message.to_string(),
        }).unwrap_or_default();
        if send_via_mqtt_sync("cmd/mini/imessage/send", &payload) {
            append_log(&label, "iMessage (MQTT)", message);
            return format!("iMessage (MQTT) -> {}: {}", label, message);
        }
        if send_sms_via_adb(PIXEL_SERIAL, &number, message) {
            append_log(&label, "SMS (fallback)", message);
            return format!("SMS (fallback) -> {}: {}", label, message);
        }
        append_log(&label, "FAILED", message);
        return format!("FAILED to send to {}", label);
    }

    // Unknown: try iMessage first, then SMS
    let payload = serde_json::to_string(&SendCmd {
        number: number.clone(),
        message: message.to_string(),
    }).unwrap_or_default();
    if send_via_mqtt_sync("cmd/mini/imessage/send", &payload) {
        append_log(&label, "iMessage (MQTT)", message);
        return format!("iMessage (MQTT) -> {}: {}", label, message);
    }
    if send_sms_via_adb(PIXEL_SERIAL, &number, message) {
        append_log(&label, "SMS (ADB)", message);
        return format!("SMS (ADB) -> {}: {}", label, message);
    }
    append_log(&label, "FAILED", message);
    format!("FAILED to send to {}", label)
}

// ── Unix socket endpoint ────────────────────────────────

async fn run_socket_server() -> anyhow::Result<()> {
    let _ = std::fs::remove_file(SOCKET_PATH);
    let listener = UnixListener::bind(SOCKET_PATH)?;
    info!("socket listening on {}", SOCKET_PATH);

    loop {
        let (stream, _) = listener.accept().await?;
        tokio::spawn(async move {
            if let Err(e) = handle_socket_client(stream).await {
                error!("socket client error: {}", e);
            }
        });
    }
}

async fn handle_socket_client(stream: tokio::net::UnixStream) -> anyhow::Result<()> {
    let (reader, mut writer) = stream.into_split();
    let mut reader = BufReader::new(reader);
    let mut line = String::new();
    reader.read_line(&mut line).await?;

    let req: SocketRequest = serde_json::from_str(line.trim())?;
    info!("socket send: {} -> {}", req.recipient, &req.message[..req.message.len().min(40)]);

    let recipient = req.recipient;
    let message = req.message;
    let result = tokio::task::spawn_blocking(move || do_send(&recipient, &message)).await?;

    let ok = !result.starts_with("FAILED");
    let resp = serde_json::to_string(&SocketResponse { ok, result })?;
    writer.write_all(resp.as_bytes()).await?;
    writer.write_all(b"\n").await?;
    writer.shutdown().await?;
    Ok(())
}

// ── Scheduled task executor ─────────────────────────────

/// Parse recurrence pattern and compute next execution time (UTC epoch seconds).
fn next_recurrence(pattern: &str, from_epoch: u64) -> Option<u64> {
    let p = pattern.trim().to_lowercase();

    // "every Nm" or "every Nh"
    if p.starts_with("every ") {
        let rest = p.strip_prefix("every ")?.trim();
        let (num_str, unit) = if rest.ends_with('m') || rest.ends_with("min") || rest.ends_with("mins") || rest.ends_with("minutes") {
            (rest.trim_end_matches(|c: char| c.is_alphabetic()).trim(), 'm')
        } else if rest.ends_with('h') || rest.ends_with("hr") || rest.ends_with("hrs") || rest.ends_with("hours") {
            (rest.trim_end_matches(|c: char| c.is_alphabetic()).trim(), 'h')
        } else {
            return None;
        };
        let n: u64 = num_str.parse().ok()?;
        return match unit {
            'm' => Some(from_epoch + n * 60),
            'h' => Some(from_epoch + n * 3600),
            _ => None,
        };
    }

    // "daily HH:MM" — approximate: add 24h from last run
    // (proper timezone handling would need more deps, this is good enough)
    if p.starts_with("daily ") {
        return Some(from_epoch + 86400);
    }

    None
}

fn execute_task_action(action_type: &str, params_json: &str) -> String {
    let params: serde_json::Value = serde_json::from_str(params_json)
        .unwrap_or(serde_json::Value::Null);

    match action_type {
        "send_message" => {
            let recipient = params["recipient"].as_str().unwrap_or(JACK_PHONE);
            let message = params["message"].as_str().unwrap_or("");
            do_send(recipient, message)
        }
        "run_command" => {
            let command = params["command"].as_str().unwrap_or("");
            match Command::new("bash")
                .args(["-c", command])
                .current_dir(home_dir())
                .output()
            {
                Ok(o) => {
                    let mut out = String::from_utf8_lossy(&o.stdout).trim().to_string();
                    if !o.status.success() {
                        let stderr = String::from_utf8_lossy(&o.stderr).trim().to_string();
                        if !stderr.is_empty() {
                            out = if out.is_empty() { stderr } else { format!("{}\nSTDERR: {}", out, stderr) };
                        }
                    }
                    if out.len() > 4000 { out.truncate(4000); }
                    if out.is_empty() { "(no output)".into() } else { out }
                }
                Err(e) => format!("ERROR: {}", e),
            }
        }
        "remind" => {
            let message = params["message"].as_str().unwrap_or("Reminder");
            // Insert into chat_messages
            if let Ok(db) = open_db() {
                let now = utc_now();
                let _ = db.execute(
                    "INSERT INTO chat_messages (role, body, created_at) VALUES (?, ?, ?)",
                    rusqlite::params!["m0usunet", format!("\u{23f0} Reminder: {}", message), now],
                );
            }
            // Also text Jack
            do_send(JACK_PHONE, &format!("Reminder: {}", message))
        }
        _ => format!("unknown action_type: {}", action_type),
    }
}

fn process_due_tasks(db: &Connection) {
    let now = utc_now();
    let mut stmt = match db.prepare(
        "SELECT id, description, action_type, action_params, recurrence, run_count, max_runs \
         FROM scheduled_tasks WHERE status = 'pending' AND scheduled_at <= ? ORDER BY scheduled_at"
    ) {
        Ok(s) => s,
        Err(e) => { error!("task query error: {}", e); return; }
    };

    let tasks: Vec<(i64, String, String, String, Option<String>, i64, Option<i64>)> =
        match stmt.query_map([&now], |row| {
            Ok((
                row.get(0)?, row.get(1)?, row.get(2)?, row.get(3)?,
                row.get(4)?, row.get(5)?, row.get(6)?,
            ))
        }) {
            Ok(rows) => rows.filter_map(|r| r.ok()).collect(),
            Err(e) => { error!("task fetch error: {}", e); return; }
        };

    for (task_id, desc, action_type, params, recurrence, run_count, max_runs) in tasks {
        info!("executing task #{}: {}", task_id, desc);

        let _ = db.execute(
            "UPDATE scheduled_tasks SET status = 'running' WHERE id = ?",
            [task_id],
        );

        let result = execute_task_action(&action_type, &params);
        let new_count = run_count + 1;
        info!("task #{} result: {}", task_id, &result[..result.len().min(200)]);

        if let Some(ref pattern) = recurrence {
            if max_runs.is_none() || new_count < max_runs.unwrap_or(0) {
                let epoch = std::time::SystemTime::now()
                    .duration_since(std::time::UNIX_EPOCH)
                    .unwrap_or_default()
                    .as_secs();
                if let Some(next_epoch) = next_recurrence(pattern, epoch) {
                    // Convert epoch back to datetime string
                    let next_str = epoch_to_datetime(next_epoch);
                    let _ = db.execute(
                        "UPDATE scheduled_tasks SET status = 'pending', scheduled_at = ?, \
                         last_run_at = ?, last_result = ?, run_count = ? WHERE id = ?",
                        rusqlite::params![next_str, now, result, new_count, task_id],
                    );
                    continue;
                }
            }
        }

        let _ = db.execute(
            "UPDATE scheduled_tasks SET status = 'done', last_run_at = ?, \
             last_result = ?, run_count = ? WHERE id = ?",
            rusqlite::params![now, result, new_count, task_id],
        );
    }
}

fn epoch_to_datetime(epoch: u64) -> String {
    let secs_per_day: u64 = 86400;
    let days = epoch / secs_per_day;
    let remaining = epoch % secs_per_day;
    let hours = remaining / 3600;
    let minutes = (remaining % 3600) / 60;
    let seconds = remaining % 60;

    let mut y: i64 = 1970;
    let mut d = days as i64;
    loop {
        let days_in_year = if y % 4 == 0 && (y % 100 != 0 || y % 400 == 0) { 366 } else { 365 };
        if d < days_in_year { break; }
        d -= days_in_year;
        y += 1;
    }
    let leap = y % 4 == 0 && (y % 100 != 0 || y % 400 == 0);
    let month_days: [i64; 12] = [31, if leap { 29 } else { 28 }, 31, 30, 31, 30, 31, 31, 30, 31, 30, 31];
    let mut m: usize = 0;
    while m < 12 && d >= month_days[m] {
        d -= month_days[m];
        m += 1;
    }
    format!("{:04}-{:02}-{:02}T{:02}:{:02}:{:02}", y, m + 1, d + 1, hours, minutes, seconds)
}

async fn run_task_scheduler(db: Arc<Mutex<Connection>>) {
    let mut interval = tokio::time::interval(Duration::from_secs(15));
    info!("task scheduler started (15s interval)");
    loop {
        interval.tick().await;
        let db = Arc::clone(&db);
        // Run in blocking task since SQLite is sync
        let _ = tokio::task::spawn_blocking(move || {
            match db.lock() {
                Ok(conn) => process_due_tasks(&conn),
                Err(e) => error!("task scheduler db lock: {}", e),
            }
        }).await;
    }
}

// ── Daemon ──────────────────────────────────────────────

async fn run_daemon() -> anyhow::Result<()> {
    let reverse = Arc::new(RwLock::new(load_reverse_contacts(&contacts_path())));

    // Open the database
    let db = Arc::new(Mutex::new(open_db()?));
    info!("database ready");

    // MQTT setup
    let mut opts = MqttOptions::new("m0usurouter", MQTT_HOST, MQTT_PORT);
    opts.set_keep_alive(Duration::from_secs(60));
    opts.set_clean_session(false); // persistent session — broker buffers while offline

    let (client, mut eventloop) = AsyncClient::new(opts, 64);

    let topics = [
        "mini/imessage/messages",
        "pixel/sms/messages",
        "t320/sms/messages",
    ];

    tokio::spawn(async {
        if let Err(e) = run_socket_server().await {
            error!("socket server failed: {}", e);
        }
    });

    // Spawn the task scheduler
    tokio::spawn(run_task_scheduler(Arc::clone(&db)));

    info!("daemon running");

    loop {
        match eventloop.poll().await {
            Ok(Event::Incoming(Packet::ConnAck(_))) => {
                for topic in &topics {
                    if let Err(e) = client.subscribe(*topic, QoS::AtLeastOnce).await {
                        error!("subscribe {} failed: {}", topic, e);
                    } else {
                        info!("subscribed: {}", topic);
                    }
                }
            }
            Ok(Event::Incoming(Packet::Publish(publish))) => {
                let topic = publish.topic.clone();
                let payload = String::from_utf8_lossy(&publish.payload).to_string();
                let reverse = Arc::clone(&reverse);
                let db = Arc::clone(&db);

                tokio::spawn(async move {
                    if let Err(e) = handle_message(&topic, &payload, &reverse, &db).await {
                        error!("handle_message error: {}", e);
                    }
                });
            }
            Ok(_) => {}
            Err(e) => {
                error!("MQTT error: {}, reconnecting in 5s", e);
                tokio::time::sleep(Duration::from_secs(5)).await;
            }
        }
    }
}

async fn handle_message(
    topic: &str,
    payload: &str,
    reverse: &RwLock<HashMap<String, String>>,
    db: &Mutex<Connection>,
) -> anyhow::Result<()> {
    let reverse = reverse.read().await;

    let (platform, sender_raw, text, guid) = if topic.contains("imessage") {
        let msg: IMessagePayload = serde_json::from_str(payload)?;
        if msg.is_from_me {
            return Ok(());
        }
        (
            "imessage",
            msg.handle_id.unwrap_or_default(),
            msg.text.unwrap_or_default(),
            msg.guid,
        )
    } else {
        let msg: SmsPayload = serde_json::from_str(payload)?;
        if msg.is_from_me {
            return Ok(());
        }
        (
            "sms",
            msg.phone.unwrap_or_default(),
            msg.text.unwrap_or_default(),
            msg.guid,
        )
    };

    if text.is_empty() || sender_raw.is_empty() {
        return Ok(());
    }

    let phone = normalize_number(&sender_raw);
    let display_name = reverse_lookup(&reverse, &sender_raw);

    info!("[{} from {}] {}", platform, display_name, &text[..text.len().min(80)]);

    // Write to database
    let db = db.lock().map_err(|e| anyhow::anyhow!("db lock: {}", e))?;
    let contact_id = upsert_contact_by_phone(&db, &phone, &display_name)?;
    let inserted = insert_message(
        &db,
        contact_id,
        platform,
        "in",
        &text,
        guid.as_deref(),
    )?;

    if inserted {
        info!("saved: contact_id={} platform={}", contact_id, platform);
    } else {
        info!("dedup: skipped duplicate message");
    }

    Ok(())
}

// ── Main ────────────────────────────────────────────────

#[tokio::main]
async fn main() {
    let cli = Cli::parse();

    match cli.command {
        Cmd::Chat => {
            if let Err(e) = chat::run_chat() {
                eprintln!("chat error: {}", e);
                std::process::exit(1);
            }
        }
        Cmd::Daemon => {
            tracing_subscriber::fmt()
                .with_env_filter(
                    tracing_subscriber::EnvFilter::try_from_default_env()
                        .unwrap_or_else(|_| "m0usurouter=info".into()),
                )
                .init();
            if let Err(e) = run_daemon().await {
                error!("daemon failed: {}", e);
                std::process::exit(1);
            }
        }
        Cmd::Send { recipient, message } => {
            let msg = message.join(" ");
            let result = do_send(&recipient, &msg);
            println!("{}", result);
        }
    }
}
