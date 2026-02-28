#!/bin/bash
# pixel.sh - Run command on Pixel via ADB USB
# Usage: pixel.sh <command>
# Example: pixel.sh screencap -p /sdcard/screen.png

if [ $# -eq 0 ]; then
    adb shell
else
    adb shell su -c "$*"
fi
