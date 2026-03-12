#!/bin/bash
# Stop the LaunchAgent, clear the log, and restart.
# Log file is truncated (not deleted) so editors don't need to reopen it.
PLIST="$HOME/Library/LaunchAgents/com.alexacart.plist"
LOG="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)/data/alexacart.log"

launchctl unload "$PLIST" 2>/dev/null || true
> "$LOG"
launchctl load "$PLIST"
echo "Restarted. Tailing $LOG"
tail -f "$LOG"
