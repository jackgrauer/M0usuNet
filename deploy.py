"""
Mesh network deployment via pyinfra.

Usage:
    pyinfra inventory.py deploy.py          # deploy everything
    pyinfra @pi deploy.py                   # deploy to Pi only
    pyinfra inventory.py deploy.py --dry    # dry run

Replaces: mesh-sync pi
"""

from io import StringIO
from pyinfra.operations import files, systemd, server

PI_HOME = "/home/jackpi5"

# --- Pi scripts ---

scripts = [
    "relay.sh",
    "imcheck.sh",
    "ipad-watchdog.sh",
    "pixel-watchdog.sh",
    "pixel.sh",
    "pixel-wait.sh",
    "pixel-wait.py",
    "pixel-sms.sh",
    "yt.sh",
    "dashboard.sh",
]

script_results = {}
for script in scripts:
    script_results[script] = files.put(
        name=f"Deploy {script}",
        src=f"./{script}",
        dest=f"{PI_HOME}/{script}",
        mode="755",
    )

# --- Pixel on-device script (pushed via ADB) ---
# pixel-sms.sh lives on the Pixel at /data/local/tmp/ for SMS sending

server.shell(
    name="Push pixel-sms.sh to Pixel via ADB",
    commands=[
        f"adb push {PI_HOME}/pixel-sms.sh /data/local/tmp/pixel-sms.sh",
        "adb shell su -c 'chmod 755 /data/local/tmp/pixel-sms.sh'",
    ],
)

# --- Contacts ---

contacts = files.put(
    name="Deploy contacts.tsv",
    src="./contacts.tsv",
    dest=f"{PI_HOME}/contacts.tsv",
    mode="644",
)

# --- systemd unit files ---

units = [
    "ipad-watchdog.service",
    "pixel-watchdog.service",
    "watchdog-alert@.service",
]

unit_results = {}
for unit in units:
    unit_results[unit] = files.put(
        name=f"Deploy {unit}",
        src=f"./{unit}",
        dest=f"/etc/systemd/system/{unit}",
        mode="644",
        _sudo=True,
    )

# --- journald config ---

journald = files.put(
    name="Deploy journald mesh config",
    src=StringIO("[Journal]\nStorage=volatile\nRuntimeMaxUse=20M\nCompress=yes\n"),
    dest="/etc/systemd/journald.conf.d/mesh.conf",
    mode="644",
    _sudo=True,
)

# --- logrotate for messages.log ---

logrotate_content = (
    f"{PI_HOME}/messages.log {{\n"
    "    monthly\n"
    "    rotate 3\n"
    "    compress\n"
    "    delaycompress\n"
    "    missingok\n"
    "    notifempty\n"
    "    create 0644 jackpi5 jackpi5\n"
    "}\n"
)

logrotate = files.put(
    name="Deploy logrotate config",
    src=StringIO(logrotate_content),
    dest="/etc/logrotate.d/mesh-relay",
    mode="644",
    _sudo=True,
)

# --- Conditional restarts ---

any_unit_changed = any(r.changed for r in unit_results.values())

if any_unit_changed:
    systemd.daemon_reload(
        name="Reload systemd daemon",
        _sudo=True,
    )

if journald.changed:
    server.shell(
        name="Restart journald",
        commands=["systemctl restart systemd-journald"],
        _sudo=True,
    )

watchdog_scripts = {"ipad-watchdog.sh", "pixel-watchdog.sh"}
watchdog_units = {"ipad-watchdog.service", "pixel-watchdog.service"}

script_change = any(
    script_results[s].changed for s in watchdog_scripts if s in script_results
)
unit_change = any(
    unit_results[u].changed for u in watchdog_units if u in unit_results
)

if script_change or unit_change:
    for svc in ["ipad-watchdog", "pixel-watchdog"]:
        systemd.service(
            name=f"Restart {svc}",
            service=svc,
            restarted=True,
            _sudo=True,
        )
