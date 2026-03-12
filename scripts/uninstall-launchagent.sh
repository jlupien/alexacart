#!/bin/bash
# Removes the AlexaCart LaunchAgent.
set -e

PLIST_PATH="$HOME/Library/LaunchAgents/com.alexacart.plist"

if [ ! -f "$PLIST_PATH" ]; then
    echo "LaunchAgent not found at $PLIST_PATH"
    exit 0
fi

launchctl unload "$PLIST_PATH"
rm "$PLIST_PATH"
echo "AlexaCart LaunchAgent uninstalled."
