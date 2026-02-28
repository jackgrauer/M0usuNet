#!/usr/bin/env python3
"""Wait for a UI element on Pixel via uiautomator2.

Replaces pixel-wait.sh (uiautomator dump + grep) with native element waits.
Much faster — keeps a persistent connection instead of cold-starting JVM each poll.

Usage:
    pixel-wait <text> [timeout_seconds]
    pixel-wait --desc <content-description> [timeout_seconds]

Returns: 0 if found, 1 if timeout, 2 if device unreachable
"""

import sys
import uiautomator2 as u2

def main():
    if len(sys.argv) < 2:
        print("Usage: pixel-wait <text> [timeout] | pixel-wait --desc <desc> [timeout]", file=sys.stderr)
        sys.exit(2)

    args = sys.argv[1:]
    by_desc = False

    if args[0] == "--desc":
        by_desc = True
        args = args[1:]

    if not args:
        print("Missing search text", file=sys.stderr)
        sys.exit(2)

    target = args[0]
    timeout = float(args[1]) if len(args) > 1 else 10.0

    try:
        d = u2.connect()
    except Exception as e:
        print(f"Device unreachable: {e}", file=sys.stderr)
        sys.exit(2)

    if by_desc:
        found = d(description=target).exists(timeout=timeout)
    else:
        found = d(text=target).exists(timeout=timeout)

    sys.exit(0 if found else 1)

if __name__ == "__main__":
    main()
