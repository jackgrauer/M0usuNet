# m0usunet — headless messaging daemon

Ingests messages from iMessage (via Mac Mini) and SMS (via Pixel) over MQTT,
stores them in SQLite, and sends scheduled replies via the relay script.

## Architecture

```
Mac Mini (MQTT broker)          Pixel (SMS gateway)
   mosquitto :8883                 bugle_watcher
        |                               |
        +---------- MQTT TLS ----------+
                      |
                   Pi (m0usunet daemon)
                      |
                 ~/m0usunet.db (SQLite WAL)
                      |
                   m0usubot (TUI client, separate package)
```

m0usunet is the **daemon only** — no UI. It runs via systemd and handles:

- **Ingest**: subscribes to MQTT topics for iMessage + SMS, writes messages to SQLite
- **Scheduler**: checks for due scheduled messages every 30s, sends them via relay
- **Relay**: shells out to the relay script on the Pi to send messages through Mini/Pixel
- **DB**: SQLite with WAL mode, shared with m0usubot for concurrent reads

## Packages

| Package | Role | Location |
|---------|------|----------|
| **m0usunet** | Headless daemon (this repo) | `~/m0usunet/` |
| **m0usubot** | TUI client (Textual) | `~/m0usubot/` |

Both are installed in the same venv (`~/m0usunet/.venv/`).
m0usubot depends on m0usunet for DB access, relay, and constants.

## Running

The daemon runs via systemd — you shouldn't need to start it manually:

```sh
sudo systemctl status m0usunet    # check status
sudo systemctl restart m0usunet   # restart
journalctl -u m0usunet -f         # follow logs
```

CLI subcommands (for admin tasks only):

```sh
m0usunet daemon              # start daemon (systemd does this)
m0usunet import-contacts     # import from ~/contacts.tsv
m0usunet seed                # create test conversations
m0usunet ingest              # run ingest only (no scheduler)
m0usunet link <name> <plat>  # link a platform identity to a contact
m0usunet sources <name>      # list platform sources for a contact
m0usunet merge <from> <into> # merge one contact into another
```

## Module layout

```
m0usunet/
  __init__.py       # version
  __main__.py       # python -m m0usunet entrypoint
  cli.py            # argparse CLI, cmd_daemon, admin subcommands
  constants.py      # paths (DB_PATH, CONTACTS_TSV), timezone, env vars
  db.py             # SQLite schema, models (Contact, Message, etc.), all queries
  exceptions.py     # M0usuNetError, RelayError
  ingest.py         # MQTT client, message handlers, attachment downloads
  relay.py          # send_message() — shells out to relay script
  scheduler.py      # background thread, sends due scheduled messages
```

## Config

| Env var | Default | Purpose |
|---------|---------|---------|
| `M0USUNET_DB` | `~/m0usunet.db` | SQLite database path |
| `M0USUNET_MQTT_HOST` | `mini` | MQTT broker hostname |
| `M0USUNET_MQTT_PORT` | `8883` | MQTT broker port (TLS) |
| `M0USUNET_RELAY` | `~/relay.sh` | Relay script for sending messages |

## systemd

Service file: `/etc/systemd/system/m0usunet.service`

```ini
[Unit]
Description=m0usunet messaging daemon
After=network-online.target

[Service]
Type=simple
User=jackpi5
ExecStart=/home/jackpi5/m0usunet/.venv/bin/python3 -m m0usunet daemon
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
```
