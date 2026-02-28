# MousuNet

A unified messaging TUI that strips away dating app dark patterns and gives you a clean, honest interface to all your conversations.

Runs on the Pi. SSH in from Mac or iPhone (Echo). One inbox for everything.

## Core Concept

Dating apps are designed to waste your time and manipulate your behavior. MousuNet removes all of that. You see messages, profiles, and conversations — nothing else. Every dating app, iMessage, and SMS looks the same: just text from a person.

## Architecture

```
┌─────────────────────────────────────────────────────┐
│                    MousuNet TUI                     │
│              (Python / Textual, on Pi)              │
│                                                     │
│  ┌─────────────┐  ┌──────────┐  ┌───────────────┐  │
│  │ Conversations│  │  Chat    │  │ Profile View  │  │
│  │ (all sources)│  │  History │  │ (on demand)   │  │
│  └─────────────┘  └──────────┘  └───────────────┘  │
│                                                     │
│  ┌─────────────────────────────────────────────┐    │
│  │ Reply box  [Tab for Claude suggestions]     │    │
│  └─────────────────────────────────────────────┘    │
└──────────────────┬──────────────────────────────────┘
                   │
          ┌────────┼────────┐
          │        │        │
          ▼        ▼        ▼
       Pixel     iPad    iPhone
      (dating)  (iMsg)   (SMS)
```

## Devices & Roles

| Device | Role | Mechanism |
|--------|------|-----------|
| Pi | Hub, TUI host, SQLite DB, Claude bot | Always-on daemon |
| Pixel | Dating apps (Bumble, Hinge, etc.) | Xposed notification hook + uiautomator2 |
| iPad | iMessage gateway | Existing `imessage` binary via IMCore |
| iPhone | SMS gateway | Existing AppleScript relay via Mac |
| Mac | SSH client to Pi | Direct access |
| iPhone (Echo) | SSH client to Pi | Mobile access |

## Inbound Message Flow

### Dating Apps (Pixel)
1. Xposed module hooks Android `NotificationListenerService`
2. On new notification from Bumble/Hinge/etc: extract sender, message preview, app name
3. Forward to Pi via ADB stdout or a simple socket/file drop
4. Pi ingests into SQLite, triggers ntfy alert if user not in TUI

### iMessage (iPad)
- Existing relay infrastructure
- Hook into `imcheck.sh` or watch iPad's `SMS.db` for new rows
- Forward to Pi, ingest into SQLite

### SMS (iPhone)
- Existing relay via Mac AppleScript
- Capture inbound SMS (may need new listener on Mac)
- Forward to Pi, ingest into SQLite

## Outbound Message Flow

### Dating App Reply
1. User types reply in MousuNet, hits Enter
2. Pi sends command to Pixel via ADB
3. uiautomator2 script: open app → navigate to conversation → type message → send
4. Confirm delivery, update SQLite

### iMessage Reply
1. MousuNet calls existing `im` command on iPad via SSH
2. Update SQLite

### SMS Reply
1. MousuNet calls existing `sms` command on Mac via SSH
2. Update SQLite

## Identity Linking

When a dating app match transitions to SMS:
- MousuNet detects the new SMS number
- User can link it to an existing dating app conversation
- Or auto-link if the match shares their number in-app and MousuNet can parse it
- From then on, both threads appear as one unified conversation

## Claude Bot

- **Activation**: Press `Tab` in reply box to request a suggestion
- **Tone**: Warm but impartial, boundary-sensitive
- **Flow**: Claude suggests → editable draft appears → user tweaks → Enter to send
- **Context**: Claude sees the full conversation history for that person
- **Not auto-suggest**: only fires when asked

## Notifications

- ntfy.sh push to `jack-mesh-alerts` (or dedicated MousuNet topic) on new inbound message
- Only fires when user is NOT actively viewing MousuNet
- Notification includes: sender name, app source, message preview

## TUI Design

- **Framework**: Python + Textual
- **Conversation list**: All conversations, all sources, sorted by most recent message
- **Source tags**: `[bumble]` `[hinge]` `[imsg]` `[sms]` — all look the same, just labeled
- **Chat view**: Full conversation history, scrollable
- **Profile view**: Accessible per-conversation, shows name + profile info (bio, basics, prompts)
- **Reply box**: Bottom of chat view, Tab for Claude, Enter to send
- **Consistent UI**: No visual distinction between app sources — a person is a person

## Storage

**SQLite on Pi** (`~/mousunet.db`)

### Tables (draft)

```sql
-- People (one row per identity, even across platforms)
CREATE TABLE contacts (
    id INTEGER PRIMARY KEY,
    display_name TEXT NOT NULL,
    linked_phone TEXT,          -- if they moved to SMS
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Platform accounts linked to a contact
CREATE TABLE contact_sources (
    id INTEGER PRIMARY KEY,
    contact_id INTEGER REFERENCES contacts(id),
    platform TEXT NOT NULL,     -- 'bumble', 'hinge', 'imessage', 'sms'
    platform_id TEXT,           -- username/ID on that platform
    profile_data JSON,          -- bio, prompts, basics
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);

-- Messages
CREATE TABLE messages (
    id INTEGER PRIMARY KEY,
    contact_id INTEGER REFERENCES contacts(id),
    platform TEXT NOT NULL,
    direction TEXT NOT NULL,    -- 'in' or 'out'
    body TEXT NOT NULL,
    sent_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP,
    delivered BOOLEAN DEFAULT FALSE
);

-- Claude suggestions (for learning/review)
CREATE TABLE suggestions (
    id INTEGER PRIMARY KEY,
    message_id INTEGER REFERENCES messages(id),
    suggested_text TEXT,
    used BOOLEAN DEFAULT FALSE,
    edited_text TEXT,           -- what user actually sent (if modified)
    created_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP
);
```

## Tech Stack

| Component | Tech |
|-----------|------|
| TUI | Python + Textual |
| Database | SQLite |
| Pixel automation | uiautomator2 (Python) |
| Pixel notifications | Xposed module (Java/Kotlin) |
| iMessage | Existing Obj-C binary on iPad |
| SMS | Existing AppleScript on Mac |
| Claude | Anthropic API (Python SDK) |
| Alerts | ntfy.sh |
| Access | SSH (Mac direct, iPhone via Echo) |

## Build Order

### Phase 1: Foundation
- [ ] SQLite schema + Python data layer
- [ ] Basic Textual TUI (conversation list + chat view + reply box)
- [ ] Wire up outbound SMS (easiest, uses existing `msg` command)
- [ ] Wire up outbound iMessage (uses existing `im` command)

### Phase 2: Pixel Integration
- [ ] Build Xposed notification listener module for Pixel
- [ ] Forward notifications to Pi (ADB or socket)
- [ ] Ingest dating app messages into SQLite
- [ ] Build uiautomator2 reply script (open app → find chat → type → send)

### Phase 3: Intelligence
- [ ] Claude suggestion engine (Tab to invoke, editable draft)
- [ ] Profile scraping from dating apps (uiautomator2)
- [ ] Profile view in TUI

### Phase 4: Identity & Polish
- [ ] Auto-link dating app → SMS identity transitions
- [ ] ntfy notifications for new messages
- [ ] Inbound SMS/iMessage capture and ingestion
- [ ] Conversation search

### Phase 5+: Future
- [ ] (room for more features)

## Open Questions

- Best transport for Pixel → Pi notifications (ADB pipe vs TCP socket vs file watch)
- How to reliably detect "same person" across Bumble → SMS transition
- Rate limiting on uiautomator2 actions to avoid app bans
- Whether to run TUI as a persistent tmux session or launch fresh each time
