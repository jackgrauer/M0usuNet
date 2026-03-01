#!/system/bin/sh
# pixel-sms.sh - Send SMS from Pixel's cellular radio via Messages app UI
# Lives on Pixel at /data/local/tmp/pixel-sms.sh
# Called from Pi via: echo "message" | adb shell su -c "sh /data/local/tmp/pixel-sms.sh +1XXXXXXXXXX"
# Reads message from stdin to avoid quoting hell through adb+su layers
#
# Uses am intent + UI tap instead of service call isms (which silently fails on H2O/Android 12)

number="$1"
if [ -z "$number" ]; then
    echo "Usage: pixel-sms.sh <number>" >&2
    echo "Message is read from stdin" >&2
    exit 1
fi

message="$(cat)"
if [ -z "$message" ]; then
    echo "No message on stdin" >&2
    exit 1
fi

# Wake screen and dismiss keyguard
input keyevent KEYCODE_WAKEUP
sleep 0.5
wm dismiss-keyguard
sleep 1

# Open Messages compose with pre-filled body
am start -a android.intent.action.SENDTO -d "sms:${number}" --es sms_body "$message"
sleep 3

# Find the Send button via uiautomator and tap it
uiautomator dump /data/local/tmp/ui_dump.xml 2>/dev/null
send_bounds=$(grep -o 'content-desc="Send SMS"[^/]*bounds="\[[0-9]*,[0-9]*\]\[[0-9]*,[0-9]*\]"' /data/local/tmp/ui_dump.xml 2>/dev/null)

if [ -z "$send_bounds" ]; then
    echo "ERROR: Send button not found" >&2
    rm -f /data/local/tmp/ui_dump.xml
    exit 1
fi

# Extract center coordinates from bounds="[x1,y1][x2,y2]"
x1=$(echo "$send_bounds" | grep -o '\[[0-9]*,' | head -1 | tr -d '[,')
y1=$(echo "$send_bounds" | grep -o ',[0-9]*\]' | head -1 | tr -d ',]')
x2=$(echo "$send_bounds" | grep -o '\[[0-9]*,' | tail -1 | tr -d '[,')
y2=$(echo "$send_bounds" | grep -o ',[0-9]*\]' | tail -1 | tr -d ',]')

tap_x=$(( (x1 + x2) / 2 ))
tap_y=$(( (y1 + y2) / 2 ))

input tap $tap_x $tap_y
sleep 1

# Verify send by checking for "You said" in UI
uiautomator dump /data/local/tmp/ui_dump.xml 2>/dev/null
if grep -q "You said" /data/local/tmp/ui_dump.xml 2>/dev/null; then
    echo "OK: SMS sent to $number"
else
    echo "WARN: Tap sent but could not verify delivery" >&2
fi

rm -f /data/local/tmp/ui_dump.xml
