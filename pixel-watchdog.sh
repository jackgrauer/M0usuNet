#!/bin/bash
# Pixel ADB Watchdog
# Monitors Pixel USB/ADB connection, logs status, attempts recovery
# Logs to stdout (captured by journald)
# Alerts via ntfy.sh
# Heartbeats to systemd WatchdogSec
# Post-reboot: gates on root, unlocks screen, reflashes if root missing

PIXEL_SERIAL="9AHAY1DQS5"
CHECK_INTERVAL=60
NTFY_TOPIC="jack-mesh-alerts"
PATCHED_BOOT="/home/jackpi5/magisk_patched_known_good.img"
ALERTED=0
RECOVER_COUNT=0
RECOVER_THRESHOLD=3
ROOT_OK=0

log() {
    echo "$(date '+%Y-%m-%d %H:%M:%S') $1"
}

alert() {
    curl -s -d "$1" "ntfy.sh/$NTFY_TOPIC" >/dev/null 2>&1
}

check_adb() {
    adb devices 2>/dev/null | grep -q "$PIXEL_SERIAL"
}

check_shell() {
    timeout 10 adb shell echo ok 2>/dev/null | grep -q "ok"
}

check_root() {
    adb shell su -c "whoami" 2>/dev/null | grep -q "root"
}

wait_for_boot() {
    local tries=0
    while [ $tries -lt 30 ]; do
        if [ "$(adb shell getprop sys.boot_completed 2>/dev/null)" = "1" ]; then
            return 0
        fi
        sleep 5
        tries=$((tries + 1))
        systemd-notify WATCHDOG=1 2>/dev/null
    done
    return 1
}

unlock_screen() {
    adb shell input keyevent KEYCODE_WAKEUP 2>/dev/null
    sleep 1
    adb shell input swipe 540 1800 540 800 2>/dev/null
    sleep 2
}

attempt_reflash() {
    if [ ! -f "$PATCHED_BOOT" ]; then
        log "No known-good patched boot image at $PATCHED_BOOT"
        alert "CRITICAL: Pixel root missing, no patched boot image to reflash"
        return 1
    fi

    log "Reflashing known-good Magisk boot image..."
    alert "Pixel root missing after reboot — reflashing boot image"

    adb reboot bootloader 2>&1
    sleep 15
    systemd-notify WATCHDOG=1 2>/dev/null

    if ! fastboot devices 2>/dev/null | grep -q "$PIXEL_SERIAL"; then
        log "Device not in fastboot mode"
        return 1
    fi

    fastboot flash boot "$PATCHED_BOOT" 2>&1
    fastboot reboot 2>&1
    sleep 10

    adb wait-for-device
    wait_for_boot
    sleep 10
    unlock_screen

    if check_root; then
        log "Reflash succeeded — root restored"
        alert "Pixel root restored after reflash"
        return 0
    else
        log "Reflash failed — root still missing"
        alert "CRITICAL: Pixel reflash failed, root still missing"
        return 1
    fi
}

post_reboot_check() {
    log "Device back — waiting for full boot..."
    systemd-notify WATCHDOG=1 2>/dev/null
    wait_for_boot
    sleep 10
    unlock_screen

    if check_root; then
        log "Root OK after reboot"
        ROOT_OK=1
    else
        log "Root MISSING after reboot"
        ROOT_OK=0
        attempt_reflash
        if check_root; then
            ROOT_OK=1
        fi
    fi
}

attempt_recovery() {
    log "Attempting ADB recovery..."

    adb kill-server 2>&1
    sleep 2
    adb start-server 2>&1
    sleep 5

    if check_adb && check_shell; then
        log "Recovery succeeded - ADB reconnected"
        return 0
    fi

    log "Recovery failed - Pixel may need physical attention (USB replug or reboot)"
    return 1
}

systemd-notify --ready 2>/dev/null
log "=== Pixel Watchdog started ==="
FAILS=0
WAS_DOWN=0

while true; do
    systemd-notify WATCHDOG=1 2>/dev/null

    if check_adb && check_shell; then
        if [ $FAILS -gt 0 ]; then
            RECOVER_COUNT=$((RECOVER_COUNT + 1))
            if [ $RECOVER_COUNT -ge $RECOVER_THRESHOLD ]; then
                log "Pixel back after $FAILS failures ($RECOVER_COUNT consecutive OK)"

                # Post-reboot root gate
                if [ $WAS_DOWN -eq 1 ]; then
                    post_reboot_check
                    WAS_DOWN=0
                fi

                if [ $ALERTED -eq 1 ]; then
                    alert "Pixel is back UP"
                fi
                ALERTED=0
                FAILS=0
                RECOVER_COUNT=0
            fi
        fi
    else
        RECOVER_COUNT=0
        FAILS=$((FAILS + 1))
        WAS_DOWN=1
        log "ADB fail #$FAILS"
        if [ $FAILS -eq 3 ]; then
            if [ $ALERTED -eq 0 ]; then
                log "Sending alert: Pixel is down"
                alert "ALERT: Pixel is down (3 consecutive failures)"
                ALERTED=1
            fi
            attempt_recovery
        elif [ $((FAILS % 10)) -eq 0 ]; then
            attempt_recovery
        fi
    fi
    sleep $CHECK_INTERVAL
done
