#!/bin/bash
# yt.sh - YouTube TV remote control on Pi
# Manages mpv playback via IPC socket
# Usage: yt.sh <url>              Play a video
#        yt.sh pause              Toggle pause
#        yt.sh stop               Stop playback
#        yt.sh vol <0-100>        Set volume
#        yt.sh seek <+/-seconds>  Seek forward/back
#        yt.sh info               Show what's playing

SOCKET="/tmp/mpv-socket"
MPV_OPTS="--really-quiet --hwdec=v4l2m2m-copy --vo=drm --ao=alsa"
YTDL_FMT="bestvideo[height<=720][vcodec^=avc]+bestaudio/best[height<=720]"
COOKIES="$HOME/yt-cookies.txt"

cmd() {
    if [ ! -S "$SOCKET" ]; then
        echo "Nothing playing" >&2
        return 1
    fi
    echo "$1" | socat - "$SOCKET" 2>/dev/null
}

case "${1:-}" in
    pause)
        cmd '{"command": ["cycle", "pause"]}'
        ;;
    stop)
        cmd '{"command": ["quit"]}' 2>/dev/null
        pkill -f "mpv.*mpv-socket" 2>/dev/null
        rm -f "$SOCKET"
        echo "Stopped"
        ;;
    vol|volume)
        if [ -z "$2" ]; then
            cmd '{"command": ["get_property", "volume"]}' | grep -o '"data":[0-9.]*' | cut -d: -f2
        else
            cmd "{\"command\": [\"set_property\", \"volume\", $2]}"
            echo "Volume: $2"
        fi
        ;;
    seek)
        cmd "{\"command\": [\"seek\", $2]}"
        ;;
    info)
        title=$(cmd '{"command": ["get_property", "media-title"]}' 2>/dev/null | grep -o '"data":"[^"]*"' | cut -d'"' -f4)
        pos=$(cmd '{"command": ["get_property", "time-pos"]}' 2>/dev/null | grep -o '"data":[0-9.]*' | cut -d: -f2)
        dur=$(cmd '{"command": ["get_property", "duration"]}' 2>/dev/null | grep -o '"data":[0-9.]*' | cut -d: -f2)
        paused=$(cmd '{"command": ["get_property", "pause"]}' 2>/dev/null | grep -o '"data":[a-z]*' | cut -d: -f2)
        if [ -n "$title" ]; then
            printf "%s\n" "$title"
            if [ -n "$pos" ] && [ -n "$dur" ]; then
                pm=$(printf "%d:%02d" $((${pos%.*}/60)) $((${pos%.*}%60)))
                dm=$(printf "%d:%02d" $((${dur%.*}/60)) $((${dur%.*}%60)))
                state="playing"
                [ "$paused" = "true" ] && state="paused"
                printf "%s  %s / %s\n" "$state" "$pm" "$dm"
            fi
        else
            echo "Nothing playing"
        fi
        ;;
    "")
        echo "Usage: yt <url|pause|stop|vol|seek|info>"
        ;;
    *)
        # Anything else is treated as a URL
        url="$1"

        # Kill existing playback
        cmd '{"command": ["quit"]}' 2>/dev/null
        pkill -f "mpv.*mpv-socket" 2>/dev/null
        sleep 0.5
        rm -f "$SOCKET"

        YTDL_RAW="js-runtimes=node"
        [ -f "$COOKIES" ] && YTDL_RAW="$YTDL_RAW,cookies=$COOKIES"

        setsid mpv $MPV_OPTS \
            --input-ipc-server="$SOCKET" \
            --ytdl-format="$YTDL_FMT" \
            --ytdl-raw-options="$YTDL_RAW" \
            "$url" </dev/null >/dev/null 2>&1 &

        # Wait for socket to appear
        for i in $(seq 1 10); do
            [ -S "$SOCKET" ] && break
            sleep 0.5
        done

        # Get title
        sleep 2
        title=$(cmd '{"command": ["get_property", "media-title"]}' 2>/dev/null | grep -o '"data":"[^"]*"' | cut -d'"' -f4)
        echo "Playing: ${title:-$url}"
        ;;
esac
