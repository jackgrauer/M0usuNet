#!/bin/bash
# pixel-wait.sh - Wait for a UI element to appear on Pixel screen
# Replaces hardcoded sleep calls with actual UI state checks
# Usage: pixel-wait <text|content-desc> [timeout_seconds]
# Returns: 0 if found, 1 if timeout

if [ $# -lt 1 ]; then
    echo "Usage: pixel-wait <text> [timeout_seconds]" >&2
    exit 1
fi

TARGET="$1"
TIMEOUT="${2:-10}"
ELAPSED=0
INTERVAL=0.5
XML="/sdcard/ui.xml"

while (( $(echo "$ELAPSED < $TIMEOUT" | bc -l) )); do
    adb shell su -c "uiautomator dump $XML" &>/dev/null
    if adb shell su -c "cat $XML" 2>/dev/null | grep -qi "$TARGET"; then
        exit 0
    fi
    sleep $INTERVAL
    ELAPSED=$(echo "$ELAPSED + $INTERVAL" | bc -l)
done

exit 1
