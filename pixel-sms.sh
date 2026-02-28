#!/system/bin/sh
# pixel-sms.sh - Send SMS from Pixel's cellular radio
# Lives on Pixel at /data/local/tmp/pixel-sms.sh
# Called from Pi via: echo "message" | adb shell su -c "sh /data/local/tmp/pixel-sms.sh +1XXXXXXXXXX"
# Reads message from stdin to avoid quoting hell through adb+su layers

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

# sendTextForSubscriber via Android telephony service (Android 12, Pixel 3a)
# Args: subId, callingPkg, callingAttr, destAddr, scAddr, text, sentIntent, deliveryIntent, persistMessage
service call isms 7 i32 0 s16 "com.android.mms" s16 "null" s16 "$number" s16 "null" s16 "$message" s16 "null" s16 "null" i32 0
