#!/usr/bin/env bash
# Remove the launchd agent installed by install_schedule.sh.
set -euo pipefail

LABEL="com.nitya.canvas-forum-agent"
PLIST="$HOME/Library/LaunchAgents/$LABEL.plist"

launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
rm -f "$PLIST"
echo "removed $LABEL"
