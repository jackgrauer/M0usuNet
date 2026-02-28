#!/bin/bash
# iPad Jailbreak Watchdog
# Monitors iPad SSH and attempts re-jailbreak if it reboots
# Logs to stdout (captured by journald)
# Alerts via ntfy.sh
# Heartbeats to systemd WatchdogSec

IPAD_IP="192.168.0.11"
IPAD_UDID="4534b945e50aab7a2ed2d57c798245a3c1403fa5"
CHECK_INTERVAL=60
NTFY_TOPIC="jack-mesh-alerts"
ALERTED=0
RECOVER_COUNT=0
RECOVER_THRESHOLD=3

log() {
    echo "$(date '+%Y-%m-%d %H:%M:%S') $1"
}

alert() {
    curl -s -d "$1" "ntfy.sh/$NTFY_TOPIC" >/dev/null 2>&1
}

check_ssh() {
    ssh -o ConnectTimeout=5 -o BatchMode=yes ipad "echo ok" 2>/dev/null
}

attempt_rejailbreak() {
    log "iPad SSH is down. Checking USB..."

    if ! idevice_id -l 2>/dev/null | grep -q "$IPAD_UDID"; then
        log "iPad not on USB. Cannot re-jailbreak."
        return 1
    fi

    log "iPad on USB. Entering recovery mode..."
    ideviceenterrecovery "$IPAD_UDID" 2>&1
    sleep 10

    log "Attempting DFU via irecovery reset..."
    irecovery -r 2>&1
    sleep 5

    MODE=$(irecovery -m 2>&1)
    log "Device mode: $MODE"

    if echo "$MODE" | grep -qi "DFU"; then
        log "In DFU! Running palera1n -l ..."
        palera1n -l 2>&1
        log "Waiting for boot..."
        sleep 60
        for i in $(seq 1 12); do
            if check_ssh | grep -q "ok"; then
                log "SUCCESS: iPad re-jailbroken!"
                return 0
            fi
            sleep 10
        done
        log "palera1n ran but SSH not back yet"
        return 1
    else
        log "FAILED to enter DFU automatically."
        log "Manual button press needed: Home+Power 8s, release Power, hold Home 5s"
        return 1
    fi
}

systemd-notify --ready 2>/dev/null
log "=== iPad Watchdog started ==="
FAILS=0

while true; do
    systemd-notify WATCHDOG=1 2>/dev/null

    if check_ssh | grep -q "ok"; then
        if [ $FAILS -gt 0 ]; then
            RECOVER_COUNT=$((RECOVER_COUNT + 1))
            if [ $RECOVER_COUNT -ge $RECOVER_THRESHOLD ]; then
                log "iPad back after $FAILS failures ($RECOVER_COUNT consecutive OK)"
                if [ $ALERTED -eq 1 ]; then
                    alert "iPad is back UP"
                fi
                ALERTED=0
                FAILS=0
                RECOVER_COUNT=0
            fi
        fi
    else
        RECOVER_COUNT=0
        FAILS=$((FAILS + 1))
        log "SSH fail #$FAILS"
        if [ $FAILS -eq 3 ]; then
            if [ $ALERTED -eq 0 ]; then
                log "Sending alert: iPad is down"
                alert "ALERT: iPad is down (3 consecutive failures)"
                ALERTED=1
            fi
            attempt_rejailbreak
        elif [ $FAILS -eq 10 ]; then
            attempt_rejailbreak
        fi
    fi
    sleep $CHECK_INTERVAL
done
