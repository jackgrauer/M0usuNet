#!/bin/bash
# dashboard.sh - MousuNet mesh network dashboard
# Runs on Pi, checks all devices and services

R='\033[31m'    # red
G='\033[32m'    # green
Y='\033[33m'    # yellow
C='\033[36m'    # cyan
B='\033[1m'     # bold
D='\033[2m'     # dim
N='\033[0m'     # reset

SOCKET="/tmp/mpv-socket"

up()   { printf "${G}●${N}"; }
down() { printf "${R}●${N}"; }
warn() { printf "${Y}●${N}"; }

# --- Gather data in parallel ---
tmpdir=$(mktemp -d)

# iPad check
(ssh -o ConnectTimeout=3 -o BatchMode=yes ipad "echo ok" &>/dev/null && echo "up" || echo "down") > "$tmpdir/ipad" &

# Mac check
(ssh -o ConnectTimeout=3 -o BatchMode=yes mac "echo ok" &>/dev/null && echo "up" || echo "down") > "$tmpdir/mac" &

# Pixel check
(adb devices 2>/dev/null | grep -q "device$" && echo "up" || echo "down") > "$tmpdir/pixel" &

# Watchdog statuses
(systemctl is-active ipad-watchdog 2>/dev/null) > "$tmpdir/ipad-wd" &
(systemctl is-active pixel-watchdog 2>/dev/null) > "$tmpdir/pixel-wd" &

# mpv status
(if [ -S "$SOCKET" ]; then
    title=$(echo '{"command": ["get_property", "media-title"]}' | socat - "$SOCKET" 2>/dev/null | grep -o '"data":"[^"]*"' | cut -d'"' -f4)
    pos=$(echo '{"command": ["get_property", "time-pos"]}' | socat - "$SOCKET" 2>/dev/null | grep -o '"data":[0-9.]*' | cut -d: -f2)
    dur=$(echo '{"command": ["get_property", "duration"]}' | socat - "$SOCKET" 2>/dev/null | grep -o '"data":[0-9.]*' | cut -d: -f2)
    paused=$(echo '{"command": ["get_property", "pause"]}' | socat - "$SOCKET" 2>/dev/null | grep -o '"data":[a-z]*' | cut -d: -f2)
    if [ -n "$title" ]; then
        state="▶"
        [ "$paused" = "true" ] && state="⏸"
        pm=$(printf "%d:%02d" $((${pos%.*}/60)) $((${pos%.*}%60)) 2>/dev/null)
        dm=$(printf "%d:%02d" $((${dur%.*}/60)) $((${dur%.*}%60)) 2>/dev/null)
        echo "$state $title ($pm/$dm)"
    else
        echo "off"
    fi
else
    echo "off"
fi) > "$tmpdir/mpv" &

# Uptime
(uptime -p 2>/dev/null || uptime | sed 's/.*up/up/;s/,.*//') > "$tmpdir/uptime" &

# Last message
(tail -1 ~/messages.log 2>/dev/null || echo "none") > "$tmpdir/lastmsg" &

wait

# --- Read results ---
ipad_st=$(cat "$tmpdir/ipad")
mac_st=$(cat "$tmpdir/mac")
pixel_st=$(cat "$tmpdir/pixel")
ipad_wd=$(cat "$tmpdir/ipad-wd")
pixel_wd=$(cat "$tmpdir/pixel-wd")
mpv_st=$(cat "$tmpdir/mpv")
pi_uptime=$(cat "$tmpdir/uptime")
last_msg=$(cat "$tmpdir/lastmsg")

rm -rf "$tmpdir"

# --- Display ---
clear
printf "${B}${C}"
cat << 'BANNER'
 __  __  ___  _   _ ___ _   _ _  _ ___ _____
|  \/  |/ _ \| | | / __| | | | \| | __|_   _|
| |\/| | (_) | |_| \__ \ |_| | .` | _|  | |
|_|  |_|\___/ \___/|___/\___/|_|\_|___| |_|
BANNER
printf "${N}\n"

# Devices
printf "${B} DEVICES${N}\n"
printf " ┌──────────┬──────────────┬─────────────┬───────────┐\n"
printf " │ ${B}%-8s${N} │ ${B}%-12s${N} │ ${B}%-11s${N} │ ${B}%-9s${N} │\n" "Device" "Address" "Status" "Role"
printf " ├──────────┼──────────────┼─────────────┼───────────┤\n"

printf " │ Pi       │ 192.168.0.19 │ "
up
printf " up          │ hub       │\n"

printf " │ iPad     │ 192.168.0.11 │ "
[ "$ipad_st" = "up" ] && up || down
[ "$ipad_st" = "up" ] && printf " up          │" || printf " ${R}down${N}        │"
printf " iMessage  │\n"

printf " │ Pixel    │ USB/ADB      │ "
[ "$pixel_st" = "up" ] && up || down
[ "$pixel_st" = "up" ] && printf " up          │" || printf " ${R}down${N}        │"
printf " dating    │\n"

printf " │ Mac      │ 100.82.246.99│ "
[ "$mac_st" = "up" ] && up || down
[ "$mac_st" = "up" ] && printf " up          │" || printf " ${R}down${N}        │"
printf " SMS relay │\n"

printf " └──────────┴──────────────┴─────────────┴───────────┘\n"

# Services
printf "\n${B} SERVICES${N}\n"
printf " ┌────────────────────┬─────────────┐\n"

printf " │ iPad watchdog      │ "
[ "$ipad_wd" = "active" ] && { up; printf " running     │\n"; } || { down; printf " ${R}stopped${N}     │\n"; }

printf " │ Pixel watchdog     │ "
[ "$pixel_wd" = "active" ] && { up; printf " running     │\n"; } || { down; printf " ${R}stopped${N}     │\n"; }

printf " │ Pi-hole            │ "
if systemctl is-active pihole-FTL &>/dev/null; then
    up; printf " running     │\n"
else
    warn; printf " ${Y}unknown${N}     │\n"
fi

printf " └────────────────────┴─────────────┘\n"

# Now Playing
printf "\n${B} NOW PLAYING${N}\n"
printf " ┌────────────────────────────────────────────────────┐\n"
if [ "$mpv_st" != "off" ]; then
    printf " │ ${G}%-50s${N}  │\n" "$mpv_st"
else
    printf " │ ${D}%-50s${N}  │\n" "Nothing playing"
fi
printf " └────────────────────────────────────────────────────┘\n"

# Last Message
printf "\n${B} LAST MESSAGE${N}\n"
printf " ┌────────────────────────────────────────────────────┐\n"
if [ "$last_msg" != "none" ]; then
    # Truncate to fit
    display_msg=$(echo "$last_msg" | cut -c1-50)
    printf " │ %-50s  │\n" "$display_msg"
else
    printf " │ ${D}%-50s${N}  │\n" "No messages"
fi
printf " └────────────────────────────────────────────────────┘\n"

# Pi uptime
printf "\n ${D}Pi uptime: %s${N}\n" "$pi_uptime"
printf " ${D}yt <url> | yt pause | yt stop | msg <name> <text>${N}\n"
