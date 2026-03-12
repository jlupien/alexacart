#!/bin/bash
# Installs AlexaCart as a macOS LaunchAgent so it starts automatically at login.
set -e

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
PLIST_PATH="$HOME/Library/LaunchAgents/com.alexacart.plist"
LOG_FILE="$REPO_DIR/data/alexacart.log"

# Find uv
UV_PATH=""
for candidate in "$HOME/.local/bin/uv" "/opt/homebrew/bin/uv" "/usr/local/bin/uv" "$(which uv 2>/dev/null)"; do
    if [ -x "$candidate" ]; then
        UV_PATH="$candidate"
        break
    fi
done

if [ -z "$UV_PATH" ]; then
    echo "Error: uv not found. Install it: brew install uv"
    exit 1
fi

echo "uv:      $UV_PATH"
echo "repo:    $REPO_DIR"
echo "log:     $LOG_FILE"

mkdir -p "$REPO_DIR/data"

cat > "$PLIST_PATH" << EOF
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN" "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
    <key>Label</key>
    <string>com.alexacart</string>
    <key>ProgramArguments</key>
    <array>
        <string>$UV_PATH</string>
        <string>run</string>
        <string>python</string>
        <string>run.py</string>
    </array>
    <key>WorkingDirectory</key>
    <string>$REPO_DIR</string>
    <key>RunAtLoad</key>
    <true/>
    <key>KeepAlive</key>
    <true/>
    <key>StandardOutPath</key>
    <string>$LOG_FILE</string>
    <key>StandardErrorPath</key>
    <string>$LOG_FILE</string>
    <key>EnvironmentVariables</key>
    <dict>
        <key>PATH</key>
        <string>/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:$HOME/.local/bin</string>
    </dict>
</dict>
</plist>
EOF

# Unload first if already installed, then load
launchctl unload "$PLIST_PATH" 2>/dev/null || true
launchctl load "$PLIST_PATH"

# Set up log rotation via newsyslog (requires sudo)
NEWSYSLOG_CONF="/etc/newsyslog.d/alexacart.conf"
CURRENT_USER="$(whoami)"
CURRENT_GROUP="$(id -gn)"
if sudo tee "$NEWSYSLOG_CONF" > /dev/null << EOF
# logfile                      mode  owner            group            size(KB)  when  flags
$LOG_FILE  644   $CURRENT_USER  $CURRENT_GROUP  5120      *     J
EOF
then
    echo "Log rotation configured (5 MB max, via newsyslog)."
else
    echo "Skipped log rotation setup (sudo declined). Logs at $LOG_FILE will grow unbounded."
    echo "  To set it up later: sudo bash scripts/install-launchagent.sh"
fi

echo ""
echo "AlexaCart LaunchAgent installed and started."
echo "  http://127.0.0.1:8000"
echo "  logs: tail -f $LOG_FILE"
echo ""
echo "Stop:    launchctl unload $PLIST_PATH"
echo "Start:   launchctl load $PLIST_PATH"
echo "Restart: launchctl unload $PLIST_PATH && launchctl load $PLIST_PATH"
echo "Remove:  bash scripts/uninstall-launchagent.sh"
