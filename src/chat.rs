//! Chat TUI — ratatui full-screen chat with Claude AI.
//!
//! Replaces the Python chatcli.py. Connects to the same SQLite DB,
//! calls the Claude API via HTTP, executes tools locally.

use crate::{do_send, home_dir, open_db, utc_now};
use crossterm::event::{self, Event, KeyCode, KeyEventKind, KeyModifiers, MouseEventKind};
use crossterm::terminal::{
    disable_raw_mode, enable_raw_mode, EnterAlternateScreen, LeaveAlternateScreen,
};
use crossterm::ExecutableCommand;
use ratatui::backend::CrosstermBackend;
use ratatui::layout::{Constraint, Direction, Layout};
use ratatui::style::{Color, Modifier, Style};
use ratatui::text::{Line, Span};
use ratatui::widgets::{Block, Borders, Paragraph, Scrollbar, ScrollbarOrientation, ScrollbarState, Wrap};
use ratatui::Terminal;
use rusqlite::Connection;
use std::io;
use std::sync::mpsc;
use std::time::Duration;

// ── Device detection ────────────────────────────────────

fn detect_device() -> &'static str {
    let ssh = std::env::var("M0USU_SSH_CLIENT")
        .or_else(|_| std::env::var("SSH_CLIENT"))
        .unwrap_or_default();
    let ip = ssh.split_whitespace().next().unwrap_or("");
    match ip {
        "192.168.0.13" | "100.82.246.99" => "jack-macbook",
        "192.168.0.17" | "100.64.13.62" => "jack-iphone",
        "192.168.0.11" => "jack-ipad",
        "" => "jack-pi",
        _ => "jack",
    }
}

fn role_color(role: &str) -> Color {
    match role {
        "m0usunet" => Color::Rgb(0, 215, 175),
        "jack-macbook" => Color::Rgb(215, 175, 0),
        "jack-iphone" => Color::Rgb(0, 255, 0),
        "jack-ipad" => Color::Rgb(255, 175, 0),
        _ => Color::Rgb(215, 215, 215),
    }
}

// ── Claude API ──────────────────────────────────────────

const CLAUDE_API_URL: &str = "https://api.anthropic.com/v1/messages";
const CLAUDE_MODEL: &str = "claude-opus-4-6";
const MAX_TOKENS: u32 = 800;
const MAX_TOOL_ROUNDS: usize = 8;

fn load_api_key() -> String {
    let path = home_dir().join(".anthropic_api_key");
    std::fs::read_to_string(path)
        .unwrap_or_default()
        .trim()
        .to_string()
}

fn build_system_prompt(db: &Connection, narrow: bool) -> String {
    let mut prompt = String::from(
        "You are m0usunet, an AI assistant running on Jack's Raspberry Pi mesh network.\n\
         This is a persistent chat room. Jack connects from multiple devices — messages \
         are prefixed with [jack-macbook], [jack-iphone], etc. Respond to whoever is talking.\n\
         Be concise, direct, a little dry.\n\n\
         ## Messaging\n\
         Use send_message to send SMS/iMessage. Auto-detects iMessage vs SMS.\n\
         Use check_inbox to read recent incoming messages — this is your inbox.\n\n\
         ## Scheduling\n\
         Use schedule_task to set reminders, schedule messages, or run recurring commands.\n\
         Use list_tasks to see pending tasks. Use cancel_task to cancel one.\n\
         Jack is in America/New_York timezone. Convert times to UTC for scheduled_at.\n\n\
         ## Memory\n\
         Use save_memory to remember facts, preferences, or instructions across sessions.\n\
         Use recall_memory to search your memories. Memories are loaded automatically.\n\
         Save things Jack tells you to remember. Also save preferences you learn implicitly.\n\n\
         ## Pi shell access\n\
         Use run_on_pi for shell commands — checking services, logs, MQTT, network, files.\n\
         Do NOT run destructive commands without Jack's approval.\n\n\
         ## T320 PTT Radio\n\
         Use notify_t320 to send notifications to Jack's T320 radio.\n\n\
         ## Guidelines\n\
         - Keep responses short unless asked for detail.\n\
         - You have full access to the Pi — use it.\n\
         - You are m0usunet. The jack-* names are Jack on different devices.\n",
    );

    if narrow {
        prompt.push_str(
            "\nIMPORTANT: Jack is on a small phone screen (~45 chars wide). \
             Keep responses very short - one or two sentences max. No bullet lists, \
             no markdown headers, no bold. Plain short text only.\n",
        );
    }

    // Load memories
    if let Ok(rows) = db.prepare(
        "SELECT key, content, category FROM bot_memory WHERE active = 1 ORDER BY category, key",
    )
    .and_then(|mut stmt| {
        stmt.query_map([], |row| {
            Ok((
                row.get::<_, String>(0)?,
                row.get::<_, String>(1)?,
                row.get::<_, String>(2)?,
            ))
        })
        .map(|rows| rows.filter_map(|r| r.ok()).collect::<Vec<_>>())
    }) {
        if !rows.is_empty() {
            prompt.push_str("\n## Memories\n");
            let mut last_cat = String::new();
            for (key, content, cat) in &rows {
                if *cat != last_cat {
                    prompt.push_str(&format!("### {}\n", cat));
                    last_cat = cat.clone();
                }
                prompt.push_str(&format!("- {}: {}\n", key, content));
            }
        }
    }

    prompt
}

fn tools_json() -> serde_json::Value {
    serde_json::json!([
        {
            "name": "send_message",
            "description": "Send an SMS or iMessage on Jack's behalf.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "recipient": {"type": "string", "description": "Contact name or phone number"},
                    "message": {"type": "string", "description": "Message text"}
                },
                "required": ["recipient", "message"]
            }
        },
        {
            "name": "run_on_pi",
            "description": "Run a shell command on the Pi and return the output.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "command": {"type": "string", "description": "Shell command to run"}
                },
                "required": ["command"]
            }
        },
        {
            "name": "notify_t320",
            "description": "Send a notification to the T320 PTT radio via MQTT.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "message": {"type": "string", "description": "Notification text"}
                },
                "required": ["message"]
            }
        },
        {
            "name": "check_inbox",
            "description": "Check recent incoming SMS/iMessage messages.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "contact": {"type": "string", "description": "Filter by contact name or number (optional)"},
                    "limit": {"type": "integer", "description": "Number of messages (default 10, max 50)"}
                },
                "required": []
            }
        },
        {
            "name": "schedule_task",
            "description": "Schedule a future action: send a message, run a command, or set a reminder.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "description": {"type": "string"},
                    "action_type": {"type": "string", "enum": ["send_message", "run_command", "remind"]},
                    "action_params": {"type": "object"},
                    "scheduled_at": {"type": "string", "description": "ISO 8601 UTC datetime"},
                    "recurrence": {"type": "string", "description": "e.g. 'every 5m', 'daily 07:00'. Omit for one-shot."},
                    "max_runs": {"type": "integer"}
                },
                "required": ["description", "action_type", "action_params", "scheduled_at"]
            }
        },
        {
            "name": "list_tasks",
            "description": "List scheduled tasks.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "status": {"type": "string"},
                    "limit": {"type": "integer"}
                },
                "required": []
            }
        },
        {
            "name": "cancel_task",
            "description": "Cancel a scheduled task by ID.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "task_id": {"type": "integer"}
                },
                "required": ["task_id"]
            }
        },
        {
            "name": "save_memory",
            "description": "Save a fact/preference/instruction to persistent memory.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "key": {"type": "string"},
                    "content": {"type": "string"},
                    "category": {"type": "string", "enum": ["preference", "fact", "instruction", "context", "general"]}
                },
                "required": ["key", "content"]
            }
        },
        {
            "name": "recall_memory",
            "description": "Search persistent memories or list all.",
            "input_schema": {
                "type": "object",
                "properties": {
                    "query": {"type": "string"},
                    "category": {"type": "string"}
                },
                "required": []
            }
        }
    ])
}

// ── Tool execution ──────────────────────────────────────

fn execute_tool(name: &str, args: &serde_json::Value) -> String {
    match name {
        "send_message" => {
            let recipient = args["recipient"].as_str().unwrap_or("");
            let message = args["message"].as_str().unwrap_or("");
            do_send(recipient, message)
        }
        "run_on_pi" => {
            let command = args["command"].as_str().unwrap_or("");
            match std::process::Command::new("bash")
                .args(["-c", command])
                .current_dir(home_dir())
                .output()
            {
                Ok(o) => {
                    let mut out = String::from_utf8_lossy(&o.stdout).trim().to_string();
                    if !o.status.success() {
                        let stderr = String::from_utf8_lossy(&o.stderr).trim().to_string();
                        if !stderr.is_empty() {
                            out = if out.is_empty() {
                                stderr
                            } else {
                                format!("{}\nSTDERR: {}", out, stderr)
                            };
                        }
                    }
                    if out.len() > 4000 {
                        out.truncate(4000);
                    }
                    if out.is_empty() {
                        "(no output)".into()
                    } else {
                        out
                    }
                }
                Err(e) => format!("ERROR: {}", e),
            }
        }
        "notify_t320" => {
            let message = args["message"].as_str().unwrap_or("");
            let payload = serde_json::json!({"title": "m0usunet", "text": message}).to_string();
            match std::process::Command::new("mosquitto_pub")
                .args(["-h", "127.0.0.1", "-p", "1883", "-t", "cmd/t320/notify", "-m", &payload])
                .output()
            {
                Ok(o) if o.status.success() => format!("sent to T320: {}", message),
                Ok(o) => format!("FAILED: {}", String::from_utf8_lossy(&o.stderr).trim()),
                Err(e) => format!("FAILED: {}", e),
            }
        }
        "check_inbox" => {
            let contact = args["contact"].as_str().unwrap_or("");
            let limit = args["limit"].as_i64().unwrap_or(10).clamp(1, 50);
            match open_db() {
                Ok(db) => {
                    let result = if contact.is_empty() {
                        db.prepare(
                            "SELECT m.direction, c.display_name, c.phone, m.body, m.sent_at, m.platform \
                             FROM messages m JOIN contacts c ON c.id = m.contact_id \
                             ORDER BY m.id DESC LIMIT ?",
                        )
                        .and_then(|mut s| {
                            s.query_map([limit], |r| {
                                Ok((
                                    r.get::<_, String>(0)?,
                                    r.get::<_, Option<String>>(1)?,
                                    r.get::<_, Option<String>>(2)?,
                                    r.get::<_, String>(3)?,
                                    r.get::<_, Option<String>>(4)?,
                                    r.get::<_, String>(5)?,
                                ))
                            })
                            .map(|rows| rows.filter_map(|r| r.ok()).collect::<Vec<_>>())
                        })
                    } else {
                        let pat = format!("%{}%", contact);
                        db.prepare(
                            "SELECT m.direction, c.display_name, c.phone, m.body, m.sent_at, m.platform \
                             FROM messages m JOIN contacts c ON c.id = m.contact_id \
                             WHERE (c.display_name LIKE ?1 OR c.phone LIKE ?1) \
                             ORDER BY m.id DESC LIMIT ?2",
                        )
                        .and_then(|mut s| {
                            s.query_map(rusqlite::params![pat, limit], |r| {
                                Ok((
                                    r.get::<_, String>(0)?,
                                    r.get::<_, Option<String>>(1)?,
                                    r.get::<_, Option<String>>(2)?,
                                    r.get::<_, String>(3)?,
                                    r.get::<_, Option<String>>(4)?,
                                    r.get::<_, String>(5)?,
                                ))
                            })
                            .map(|rows| rows.filter_map(|r| r.ok()).collect::<Vec<_>>())
                        })
                    };
                    match result {
                        Ok(rows) if rows.is_empty() => "No messages found.".into(),
                        Ok(rows) => {
                            let lines: Vec<String> = rows
                                .iter()
                                .rev()
                                .map(|(dir, name, phone, body, ts, plat)| {
                                    let arrow = if dir == "out" { "\u{2192}" } else { "\u{2190}" };
                                    let label = name.as_deref().or(phone.as_deref()).unwrap_or("unknown");
                                    let t = ts.as_deref().map(|s| &s[..s.len().min(16)]).unwrap_or("");
                                    format!("{} {} {} [{}]: {}", t, arrow, label, plat, body)
                                })
                                .collect();
                            lines.join("\n")
                        }
                        Err(e) => format!("ERROR: {}", e),
                    }
                }
                Err(e) => format!("ERROR: {}", e),
            }
        }
        "schedule_task" => {
            let desc = args["description"].as_str().unwrap_or("");
            let action_type = args["action_type"].as_str().unwrap_or("");
            let params = args["action_params"].to_string();
            let scheduled_at = args["scheduled_at"].as_str().unwrap_or("");
            let recurrence = args["recurrence"].as_str();
            let max_runs = args["max_runs"].as_i64();
            let effective_max = if max_runs.is_none() && recurrence.is_none() {
                Some(1i64)
            } else {
                max_runs
            };
            match open_db() {
                Ok(db) => match db.execute(
                    "INSERT INTO scheduled_tasks (description, action_type, action_params, scheduled_at, recurrence, max_runs) \
                     VALUES (?1, ?2, ?3, ?4, ?5, ?6)",
                    rusqlite::params![desc, action_type, params, scheduled_at, recurrence, effective_max],
                ) {
                    Ok(_) => {
                        let id = db.last_insert_rowid();
                        let mut msg = format!("Scheduled task #{}: {} at {}", id, desc, scheduled_at);
                        if let Some(r) = recurrence {
                            msg.push_str(&format!(" (recurring: {})", r));
                        }
                        msg
                    }
                    Err(e) => format!("ERROR: {}", e),
                },
                Err(e) => format!("ERROR: {}", e),
            }
        }
        "list_tasks" => {
            let status = args["status"].as_str().unwrap_or("pending");
            let limit = args["limit"].as_i64().unwrap_or(20).clamp(1, 50);
            match open_db() {
                Ok(db) => {
                    match db.prepare(
                        "SELECT id, description, action_type, scheduled_at, recurrence, status, run_count, max_runs \
                         FROM scheduled_tasks WHERE status = ? ORDER BY scheduled_at LIMIT ?",
                    )
                    .and_then(|mut s| {
                        s.query_map(rusqlite::params![status, limit], |r| {
                            Ok((
                                r.get::<_, i64>(0)?,
                                r.get::<_, String>(1)?,
                                r.get::<_, String>(2)?,
                                r.get::<_, String>(3)?,
                                r.get::<_, Option<String>>(4)?,
                                r.get::<_, String>(5)?,
                                r.get::<_, i64>(6)?,
                                r.get::<_, Option<i64>>(7)?,
                            ))
                        })
                        .map(|rows| rows.filter_map(|r| r.ok()).collect::<Vec<_>>())
                    }) {
                        Ok(rows) if rows.is_empty() => "No tasks found.".into(),
                        Ok(rows) => rows
                            .iter()
                            .map(|(id, desc, at, sched, recur, st, runs, maxr)| {
                                let mut line = format!("#{} [{}] {} ({}) @ {}", id, st, desc, at, sched);
                                if let Some(r) = recur {
                                    line.push_str(&format!(" | recur: {}", r));
                                }
                                if let Some(m) = maxr {
                                    line.push_str(&format!(" | {}/{} runs", runs, m));
                                }
                                line
                            })
                            .collect::<Vec<_>>()
                            .join("\n"),
                        Err(e) => format!("ERROR: {}", e),
                    }
                }
                Err(e) => format!("ERROR: {}", e),
            }
        }
        "cancel_task" => {
            let task_id = args["task_id"].as_i64().unwrap_or(0);
            match open_db() {
                Ok(db) => {
                    match db.execute(
                        "UPDATE scheduled_tasks SET status = 'cancelled' WHERE id = ? AND status = 'pending'",
                        [task_id],
                    ) {
                        Ok(n) if n > 0 => format!("Cancelled task #{}.", task_id),
                        Ok(_) => format!("Task #{} not found or not pending.", task_id),
                        Err(e) => format!("ERROR: {}", e),
                    }
                }
                Err(e) => format!("ERROR: {}", e),
            }
        }
        "save_memory" => {
            let key = args["key"].as_str().unwrap_or("");
            let content = args["content"].as_str().unwrap_or("");
            let category = args["category"].as_str().unwrap_or("general");
            let now = utc_now();
            match open_db() {
                Ok(db) => {
                    let updated = db
                        .execute(
                            "UPDATE bot_memory SET content = ?, category = ?, updated_at = ? WHERE key = ? AND active = 1",
                            rusqlite::params![content, category, now, key],
                        )
                        .unwrap_or(0);
                    if updated == 0 {
                        let _ = db.execute(
                            "INSERT INTO bot_memory (key, content, category, created_at, updated_at) VALUES (?, ?, ?, ?, ?)",
                            rusqlite::params![key, content, category, now, now],
                        );
                        format!("Memory saved: {} = {}", key, content)
                    } else {
                        format!("Memory updated: {} = {}", key, content)
                    }
                }
                Err(e) => format!("ERROR: {}", e),
            }
        }
        "recall_memory" => {
            let query = args["query"].as_str().unwrap_or("");
            let category = args["category"].as_str();
            match open_db() {
                Ok(db) => {
                    let sql = if !query.is_empty() && category.is_some() {
                        "SELECT key, content, category, updated_at FROM bot_memory WHERE active = 1 AND (key LIKE ?1 OR content LIKE ?1) AND category = ?2 ORDER BY category, key"
                    } else if !query.is_empty() {
                        "SELECT key, content, category, updated_at FROM bot_memory WHERE active = 1 AND (key LIKE ?1 OR content LIKE ?1) ORDER BY category, key"
                    } else if category.is_some() {
                        "SELECT key, content, category, updated_at FROM bot_memory WHERE active = 1 AND category = ?2 ORDER BY category, key"
                    } else {
                        "SELECT key, content, category, updated_at FROM bot_memory WHERE active = 1 ORDER BY category, key"
                    };
                    let pat = format!("%{}%", query);
                    let cat = category.unwrap_or("");
                    match db
                        .prepare(sql)
                        .and_then(|mut s| {
                            s.query_map(rusqlite::params![pat, cat], |r| {
                                Ok((
                                    r.get::<_, String>(0)?,
                                    r.get::<_, String>(1)?,
                                    r.get::<_, String>(2)?,
                                    r.get::<_, String>(3)?,
                                ))
                            })
                            .map(|rows| rows.filter_map(|r| r.ok()).collect::<Vec<_>>())
                        }) {
                        Ok(rows) if rows.is_empty() => "No memories found.".into(),
                        Ok(rows) => rows
                            .iter()
                            .map(|(k, c, cat, u)| format!("[{}] {}: {} (updated {})", cat, k, c, &u[..u.len().min(10)]))
                            .collect::<Vec<_>>()
                            .join("\n"),
                        Err(e) => format!("ERROR: {}", e),
                    }
                }
                Err(e) => format!("ERROR: {}", e),
            }
        }
        _ => format!("unknown tool: {}", name),
    }
}

// ── Claude API call ─────────────────────────────────────

fn call_claude(
    api_key: &str,
    system: &str,
    messages: &[serde_json::Value],
    narrow: bool,
) -> Result<(Vec<String>, serde_json::Value), String> {
    let body = serde_json::json!({
        "model": CLAUDE_MODEL,
        "max_tokens": if narrow { 300u32 } else { MAX_TOKENS },
        "system": system,
        "tools": tools_json(),
        "messages": messages,
    });

    let body_str = serde_json::to_string(&body).map_err(|e| format!("JSON encode: {}", e))?;

    let mut resp = ureq::post(CLAUDE_API_URL)
        .header("Content-Type", "application/json")
        .header("x-api-key", api_key)
        .header("anthropic-version", "2023-06-01")
        .send(&body_str)
        .map_err(|e| format!("API error: {}", e))?;

    let resp_str = resp.body_mut().read_to_string().map_err(|e| format!("read error: {}", e))?;
    let json: serde_json::Value = serde_json::from_str(&resp_str).map_err(|e| format!("JSON parse: {}", e))?;

    let stop_reason = json["stop_reason"].as_str().unwrap_or("");
    let content = json["content"].clone();

    let mut texts = Vec::new();
    let mut tool_calls = Vec::new();

    if let Some(blocks) = content.as_array() {
        for block in blocks {
            match block["type"].as_str() {
                Some("text") => {
                    if let Some(t) = block["text"].as_str() {
                        let trimmed: &str = t.trim();
                        if !trimmed.is_empty() {
                            texts.push(trimmed.to_string());
                        }
                    }
                }
                Some("tool_use") => {
                    tool_calls.push(block.clone());
                }
                _ => {}
            }
        }
    }

    // If there are tool calls, execute them and continue
    if stop_reason == "tool_use" && !tool_calls.is_empty() {
        // Return the content block so caller can build the tool_result message
        return Ok((texts, json));
    }

    Ok((texts, serde_json::Value::Null))
}

/// Run the full Claude conversation loop with tool use.
fn chat_send(
    api_key: &str,
    db: &Connection,
    device_name: &str,
    user_text: &str,
    narrow: bool,
) -> String {
    // User message already saved by the caller.
    // Load history
    let history = load_chat_history(db, 40);
    let mut messages = history_to_claude(&history);
    let system = build_system_prompt(db, narrow);

    let mut all_texts = Vec::new();

    for _ in 0..MAX_TOOL_ROUNDS {
        match call_claude(api_key, &system, &messages, narrow) {
            Ok((texts, api_response)) => {
                all_texts.extend(texts);

                if api_response.is_null() {
                    // No tool calls, we're done
                    break;
                }

                // Build assistant message with full content
                let content = api_response["content"].clone();
                messages.push(serde_json::json!({"role": "assistant", "content": content}));

                // Execute tool calls and build results
                let mut tool_results = Vec::new();
                if let Some(blocks) = content.as_array() {
                    for block in blocks {
                        if block["type"].as_str() == Some("tool_use") {
                            let tool_name = block["name"].as_str().unwrap_or("");
                            let tool_id = block["id"].as_str().unwrap_or("");
                            let tool_input = &block["input"];
                            let result = execute_tool(tool_name, tool_input);
                            tool_results.push(serde_json::json!({
                                "type": "tool_result",
                                "tool_use_id": tool_id,
                                "content": result,
                            }));
                        }
                    }
                }

                if !tool_results.is_empty() {
                    messages.push(serde_json::json!({"role": "user", "content": tool_results}));
                } else {
                    break;
                }
            }
            Err(e) => {
                all_texts.push(format!("API error: {}", e));
                break;
            }
        }
    }

    let bot_text = if all_texts.is_empty() {
        "(no response)".to_string()
    } else {
        all_texts.join("\n")
    };

    // Save bot response
    let now = utc_now();
    let _ = db.execute(
        "INSERT INTO chat_messages (role, body, created_at) VALUES (?, ?, ?)",
        rusqlite::params!["m0usunet", bot_text, now],
    );

    bot_text
}

fn load_chat_history(db: &Connection, limit: i64) -> Vec<(String, String)> {
    db.prepare("SELECT role, body FROM chat_messages ORDER BY id DESC LIMIT ?")
        .and_then(|mut s| {
            s.query_map([limit], |r| Ok((r.get::<_, String>(0)?, r.get::<_, String>(1)?)))
                .map(|rows| {
                    let mut v: Vec<_> = rows.filter_map(|r| r.ok()).collect();
                    v.reverse();
                    v
                })
        })
        .unwrap_or_default()
}

fn history_to_claude(rows: &[(String, String)]) -> Vec<serde_json::Value> {
    let mut messages: Vec<serde_json::Value> = Vec::new();
    for (role, body) in rows {
        let (claude_role, content) = if role == "m0usunet" {
            ("assistant", body.clone())
        } else {
            ("user", format!("[{}] {}", role, body))
        };

        if let Some(last) = messages.last_mut() {
            if last["role"].as_str() == Some(claude_role) {
                let prev = last["content"].as_str().unwrap_or("");
                *last = serde_json::json!({"role": claude_role, "content": format!("{}\n{}", prev, content)});
                continue;
            }
        }
        messages.push(serde_json::json!({"role": claude_role, "content": content}));
    }

    // Claude requires first message to be "user"
    while messages.first().map(|m| m["role"].as_str()) == Some(Some("assistant")) {
        messages.remove(0);
    }
    messages
}

// ── TUI App ─────────────────────────────────────────────

enum UiMsg {
    Key(event::KeyEvent),
    Mouse(event::MouseEvent),
    DbUpdate(Vec<(i64, String, String)>), // (id, role, body)
    ClaudeResponse(String),
    Resize,
}

struct App {
    lines: Vec<(String, String)>, // (role, text_line)
    input: String,
    cursor_x: usize,
    scroll: usize,
    device_name: String,
    thinking: bool,
    seen_id: i64,
    visible_height: usize,
    term_width: usize,
}

impl App {
    fn new(device_name: &str) -> Self {
        let (w, h) = crossterm::terminal::size().unwrap_or((80, 24));
        Self {
            lines: Vec::new(),
            input: String::new(),
            cursor_x: 0,
            scroll: 0,
            device_name: device_name.to_string(),
            thinking: false,
            seen_id: 0,
            visible_height: h.saturating_sub(6) as usize, // borders + status + input
            term_width: w.saturating_sub(4) as usize,     // borders + scrollbar
        }
    }

    fn add_message(&mut self, role: &str, body: &str) {
        for line in body.lines() {
            if !line.trim().is_empty() {
                self.lines.push((role.to_string(), line.to_string()));
            }
        }
        // Auto-scroll to bottom
        self.scroll_to_bottom();
    }

    /// Estimate total wrapped visual lines.
    fn wrapped_line_count(&self) -> usize {
        let w = self.term_width.max(20);
        self.lines.iter().map(|(role, text)| {
            let full_len = role.len() + 2 + text.len(); // "role: text"
            if full_len <= w { 1 } else { (full_len + w - 1) / w }
        }).sum()
    }

    fn scroll_to_bottom(&mut self) {
        let total = self.wrapped_line_count();
        self.scroll = total.saturating_sub(self.visible_height);
    }

    fn max_scroll(&self) -> usize {
        let total = self.wrapped_line_count();
        total.saturating_sub(self.visible_height)
    }

    fn load_backlog(&mut self, db: &Connection) {
        let rows = db
            .prepare("SELECT id, role, body FROM chat_messages ORDER BY id DESC LIMIT 100")
            .and_then(|mut s| {
                s.query_map([], |r| {
                    Ok((
                        r.get::<_, i64>(0)?,
                        r.get::<_, String>(1)?,
                        r.get::<_, String>(2)?,
                    ))
                })
                .map(|rows| {
                    let mut v: Vec<_> = rows.filter_map(|r| r.ok()).collect();
                    v.reverse();
                    v
                })
            })
            .unwrap_or_default();

        for (id, role, body) in &rows {
            self.seen_id = self.seen_id.max(*id);
            self.add_message(role, body);
        }
    }
}

// ── TUI rendering ───────────────────────────────────────

fn render(f: &mut ratatui::Frame, app: &App) {
    let chunks = Layout::default()
        .direction(Direction::Vertical)
        .constraints([
            Constraint::Min(3),
            Constraint::Length(1),
            Constraint::Length(3),
        ])
        .split(f.area());

    // Messages
    let msg_lines: Vec<Line> = app
        .lines
        .iter()
        .map(|(role, text)| {
            let color = role_color(role);
            Line::from(vec![
                Span::styled(
                    format!("{}: ", role),
                    Style::default().fg(color).add_modifier(Modifier::BOLD),
                ),
                Span::styled(text.as_str(), Style::default().fg(color)),
            ])
        })
        .collect();

    // Add "thinking..." if waiting
    let mut display_lines = msg_lines;
    if app.thinking {
        display_lines.push(Line::from(Span::styled(
            "  thinking...",
            Style::default().fg(Color::DarkGray),
        )));
    }

    let scroll = app.scroll as u16;

    let messages_widget = Paragraph::new(display_lines)
        .block(Block::default().borders(Borders::ALL))
        .wrap(Wrap { trim: false })
        .scroll((scroll, 0));

    f.render_widget(messages_widget, chunks[0]);

    // Scrollbar
    let mut scrollbar_state = ScrollbarState::new(app.max_scroll()).position(scroll as usize);
    f.render_stateful_widget(
        Scrollbar::default().orientation(ScrollbarOrientation::VerticalRight),
        chunks[0],
        &mut scrollbar_state,
    );

    // Status bar
    let status = Line::from(vec![
        Span::styled(
            format!(
                " m0usunet chat \u{2502} {} \u{2502} /refresh \u{2502} ctrl-c to leave ",
                app.device_name
            ),
            Style::default()
                .fg(Color::Rgb(170, 170, 170))
                .bg(Color::Rgb(51, 51, 51)),
        ),
    ]);
    f.render_widget(
        Paragraph::new(status).style(Style::default().bg(Color::Rgb(51, 51, 51))),
        chunks[1],
    );

    // Input
    let input_widget = Paragraph::new(app.input.as_str())
        .block(
            Block::default()
                .borders(Borders::ALL)
                .title(format!(" {}> ", app.device_name))
                .title_style(
                    Style::default()
                        .fg(role_color(&app.device_name))
                        .add_modifier(Modifier::BOLD),
                ),
        );
    f.render_widget(input_widget, chunks[2]);

    // Cursor
    f.set_cursor_position((chunks[2].x + 1 + app.cursor_x as u16, chunks[2].y + 1));
}

// ── Main entry ──────────────────────────────────────────

pub fn run_chat() -> anyhow::Result<()> {
    let api_key = load_api_key();
    if api_key.is_empty() {
        eprintln!("No API key found. Set ANTHROPIC_API_KEY or create ~/.anthropic_api_key");
        return Ok(());
    }

    let device_name = detect_device().to_string();
    let db = open_db()?;

    // Ensure chat tables exist
    db.execute_batch(
        "CREATE TABLE IF NOT EXISTS chat_messages (
            id INTEGER PRIMARY KEY,
            role TEXT NOT NULL,
            body TEXT NOT NULL,
            created_at TEXT NOT NULL DEFAULT (datetime('now'))
        )"
    )?;

    let term_width = crossterm::terminal::size().map(|(w, _)| w).unwrap_or(80);
    let narrow = term_width < 60;

    let mut app = App::new(&device_name);
    app.load_backlog(&db);

    // Set up terminal
    enable_raw_mode()?;
    io::stdout().execute(EnterAlternateScreen)?;
    io::stdout().execute(crossterm::event::EnableMouseCapture)?;
    let mut terminal = Terminal::new(CrosstermBackend::new(io::stdout()))?;

    let (tx, rx) = mpsc::channel::<UiMsg>();

    // Event reader thread
    let tx_events = tx.clone();
    std::thread::spawn(move || loop {
        if event::poll(Duration::from_millis(100)).unwrap_or(false) {
            match event::read() {
                Ok(Event::Key(k)) if k.kind == KeyEventKind::Press => {
                    let _ = tx_events.send(UiMsg::Key(k));
                }
                Ok(Event::Mouse(m)) => {
                    let _ = tx_events.send(UiMsg::Mouse(m));
                }
                Ok(Event::Resize(..)) => {
                    let _ = tx_events.send(UiMsg::Resize);
                }
                _ => {}
            }
        }
    });

    // DB poller thread
    let tx_db = tx.clone();
    let seen_id = app.seen_id;
    std::thread::spawn(move || {
        let db = match open_db() {
            Ok(d) => d,
            Err(_) => return,
        };
        let mut last_seen = seen_id;
        loop {
            std::thread::sleep(Duration::from_millis(1500));
            if let Ok(mut stmt) = db.prepare(
                "SELECT id, role, body FROM chat_messages WHERE id > ? ORDER BY id",
            ) {
                if let Ok(rows) = stmt
                    .query_map([last_seen], |r| {
                        Ok((
                            r.get::<_, i64>(0)?,
                            r.get::<_, String>(1)?,
                            r.get::<_, String>(2)?,
                        ))
                    })
                    .map(|rows| rows.filter_map(|r| r.ok()).collect::<Vec<_>>())
                {
                    if !rows.is_empty() {
                        for (id, _, _) in &rows {
                            last_seen = last_seen.max(*id);
                        }
                        let _ = tx_db.send(UiMsg::DbUpdate(rows));
                    }
                }
            }
        }
    });

    // Main loop
    terminal.draw(|f| render(f, &app))?;

    loop {
        match rx.recv() {
            Ok(UiMsg::Key(key)) => {
                match (key.modifiers, key.code) {
                    (KeyModifiers::CONTROL, KeyCode::Char('c'))
                    | (KeyModifiers::CONTROL, KeyCode::Char('d'))
                    | (KeyModifiers::CONTROL, KeyCode::Char('q')) => break,
                    (_, KeyCode::Enter) => {
                        let text = app.input.trim().to_string();
                        if text.is_empty() {
                            continue;
                        }
                        app.input.clear();
                        app.cursor_x = 0;

                        if text == "/quit" || text == "/q" || text == "/back" {
                            break;
                        }

                        if text == "/refresh" || text == "/r" {
                            app.lines.clear();
                            app.load_backlog(&db);
                            terminal.draw(|f| render(f, &app))?;
                            continue;
                        }

                        // Save user message to DB now and bump seen_id
                        // so the poller doesn't double-display it
                        {
                            let now = utc_now();
                            let _ = db.execute(
                                "INSERT INTO chat_messages (role, body, created_at) VALUES (?, ?, ?)",
                                rusqlite::params![&device_name, &text, now],
                            );
                            if let Ok(max_id) = db.query_row(
                                "SELECT COALESCE(MAX(id), 0) FROM chat_messages",
                                [],
                                |r| r.get::<_, i64>(0),
                            ) {
                                app.seen_id = app.seen_id.max(max_id);
                            }
                        }

                        // Show user message + thinking
                        app.add_message(&device_name, &text);
                        app.thinking = true;
                        terminal.draw(|f| render(f, &app))?;

                        // Spawn Claude call in background (skip saving user msg, already done)
                        let tx_claude = tx.clone();
                        let api_key_clone = api_key.clone();
                        let device_clone = device_name.clone();
                        let text_clone = text.clone();
                        std::thread::spawn(move || {
                            let db = match open_db() {
                                Ok(d) => d,
                                Err(e) => {
                                    let _ = tx_claude
                                        .send(UiMsg::ClaudeResponse(format!("DB error: {}", e)));
                                    return;
                                }
                            };
                            let result =
                                chat_send(&api_key_clone, &db, &device_clone, &text_clone, narrow);
                            let _ = tx_claude.send(UiMsg::ClaudeResponse(result));
                        });
                    }
                    (_, KeyCode::Char(c)) => {
                        app.input.insert(app.cursor_x, c);
                        app.cursor_x += 1;
                    }
                    (_, KeyCode::Backspace) => {
                        if app.cursor_x > 0 {
                            app.cursor_x -= 1;
                            app.input.remove(app.cursor_x);
                        }
                    }
                    (_, KeyCode::Delete) => {
                        if app.cursor_x < app.input.len() {
                            app.input.remove(app.cursor_x);
                        }
                    }
                    (_, KeyCode::Left) => {
                        app.cursor_x = app.cursor_x.saturating_sub(1);
                    }
                    (_, KeyCode::Right) => {
                        app.cursor_x = (app.cursor_x + 1).min(app.input.len());
                    }
                    (_, KeyCode::Home) => {
                        app.cursor_x = 0;
                    }
                    (_, KeyCode::End) => {
                        app.cursor_x = app.input.len();
                    }
                    (_, KeyCode::PageUp) => {
                        app.scroll = app.scroll.saturating_sub(10);
                    }
                    (_, KeyCode::PageDown) => {
                        app.scroll = (app.scroll + 10).min(app.max_scroll());
                    }
                    _ => {}
                }
            }
            Ok(UiMsg::Mouse(mouse)) => match mouse.kind {
                MouseEventKind::ScrollUp => {
                    app.scroll = app.scroll.saturating_sub(3);
                }
                MouseEventKind::ScrollDown => {
                    app.scroll = (app.scroll + 3).min(app.max_scroll());
                }
                _ => {}
            },
            Ok(UiMsg::DbUpdate(rows)) => {
                for (id, role, body) in rows {
                    if id > app.seen_id {
                        app.seen_id = id;
                        app.add_message(&role, &body);
                    }
                }
            }
            Ok(UiMsg::ClaudeResponse(text)) => {
                app.thinking = false;
                // Bump seen_id past the messages we just wrote
                if let Ok(max_id) = db.query_row(
                    "SELECT COALESCE(MAX(id), 0) FROM chat_messages",
                    [],
                    |r| r.get::<_, i64>(0),
                ) {
                    app.seen_id = app.seen_id.max(max_id);
                }
                app.add_message("m0usunet", &text);
            }
            Ok(UiMsg::Resize) => {
                if let Ok((w, h)) = crossterm::terminal::size() {
                    app.visible_height = h.saturating_sub(6) as usize;
                    app.term_width = w.saturating_sub(4) as usize;
                }
            }
            Err(_) => break,
        }
        terminal.draw(|f| render(f, &app))?;
    }

    // Restore terminal
    io::stdout().execute(crossterm::event::DisableMouseCapture)?;
    io::stdout().execute(LeaveAlternateScreen)?;
    disable_raw_mode()?;
    Ok(())
}
