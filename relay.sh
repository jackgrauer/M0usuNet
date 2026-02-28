#!/bin/bash
# relay.sh - Central message relay on Pi
# Auto-detects iMessage vs SMS from chat history, caches results
# SMS goes via Pixel cellular (ADB), Mac AppleScript is fallback
# Usage: relay.sh <recipient> <message>

LOG="/home/jackpi5/messages.log"
CONTACTS="/home/jackpi5/contacts.tsv"
CACHE="/home/jackpi5/service-cache.tsv"
PIXEL_SMS="/data/local/tmp/pixel-sms.sh"

if [ $# -lt 2 ]; then
    echo "Usage: relay.sh <recipient> <message>" >&2
    exit 1
fi

recipient="$1"
shift
message="$*"

# Resolve contact name to number
number="$recipient"
name=""
if ! echo "$recipient" | grep -qE "^\+?[0-9]{7,}$"; then
    match=$(grep -i "$recipient" "$CONTACTS" 2>/dev/null | head -1)
    if [ -z "$match" ]; then
        echo "No contact found for: $recipient" >&2
        exit 1
    fi
    name=$(echo "$match" | cut -f1)
    number=$(echo "$match" | cut -f2)
fi

if echo "$number" | grep -qE "^[0-9]{10}$"; then
    number="+1$number"
elif echo "$number" | grep -qE "^1[0-9]{10}$"; then
    number="+$number"
fi

label="${name:+$name ($number)}"
label="${label:-$number}"

ts() { date "+%Y-%m-%d %H:%M:%S"; }

# Send SMS via Pixel cellular (primary) or Mac AppleScript (fallback)
send_sms() {
    local via=""

    # Try Pixel first — always on, no Mac needed
    if adb devices 2>/dev/null | grep -q "device$"; then
        if echo "$message" | adb shell su -c "sh $PIXEL_SMS '$number'" &>/dev/null; then
            via="Pixel"
        fi
    fi

    # Fall back to Mac AppleScript → iPhone
    if [ -z "$via" ]; then
        if ssh -o ConnectTimeout=5 -o BatchMode=yes mac "echo ok" &>/dev/null; then
            if ssh mac "/Users/jack/bin/sms $number $message" 2>&1; then
                via="Mac"
            fi
        fi
    fi

    if [ -n "$via" ]; then
        echo "$(ts) | SMS ($via) | $label | $message" >> "$LOG"
        echo "SMS ($via) -> $label: $message"
        return 0
    fi
    return 1
}

# Check cache first, then query iPad
service=$(grep "^$number	" "$CACHE" 2>/dev/null | cut -f2)
if [ -z "$service" ]; then
    service=$(/home/jackpi5/imcheck.sh "$number")
    if [ "$service" = "imessage" ] || [ "$service" = "sms" ]; then
        echo -e "$number\t$service" >> "$CACHE"
    fi
fi

# Route based on service type
case "$service" in
    sms)
        if send_sms; then
            exit 0
        fi
        echo "$(ts) | FAILED | $label | $message" >> "$LOG"
        echo "FAILED (Pixel and Mac both unreachable)" >&2
        exit 1
        ;;
    imessage)
        if ssh -o ConnectTimeout=5 -o BatchMode=yes ipad "echo ok" &>/dev/null; then
            ssh ipad "/var/jb/usr/local/bin/imessage $number $message" 2>&1
            if [ $? -eq 0 ]; then
                echo "$(ts) | iMessage | $label | $message" >> "$LOG"
                echo "iMessage -> $label: $message"
                exit 0
            fi
        fi
        # iPad down, fall back to SMS
        if send_sms; then
            echo "(iPad down, sent as SMS)" >&2
            exit 0
        fi
        echo "$(ts) | FAILED | $label | $message" >> "$LOG"
        echo "FAILED (all paths down)" >&2
        exit 1
        ;;
    *)
        # Unknown recipient — try iMessage first, fall back to SMS
        if ssh -o ConnectTimeout=5 -o BatchMode=yes ipad "echo ok" &>/dev/null; then
            ssh ipad "/var/jb/usr/local/bin/imessage $number $message" 2>&1
            if [ $? -eq 0 ]; then
                echo "$(ts) | iMessage (new) | $label | $message" >> "$LOG"
                echo "iMessage (new contact) -> $label: $message"
                exit 0
            fi
        fi
        if send_sms; then
            exit 0
        fi
        echo "$(ts) | FAILED | $label | $message" >> "$LOG"
        echo "FAILED" >&2
        exit 1
        ;;
esac
