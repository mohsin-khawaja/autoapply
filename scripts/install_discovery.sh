#!/bin/bash
# Install (or reinstall) the scheduled discovery agent.
#
#   ./scripts/install_discovery.sh            # install + start
#   ./scripts/install_discovery.sh --uninstall
#
# Replaces the old daily 08:00 sync agent, which used StartCalendarInterval and
# silently lost any day the Mac was powered off through the window.
set -euo pipefail

REPO="/Users/mohsinkhawaja/autoapply"
LABEL="com.mohsin.autoapply.discovery"
OLD_LABEL="com.mohsin.autoapply.dailysync"
AGENTS="$HOME/Library/LaunchAgents"
PLIST="$AGENTS/$LABEL.plist"

if [ "${1:-}" = "--uninstall" ]; then
  launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
  rm -f "$PLIST"
  echo "uninstalled $LABEL"
  exit 0
fi

mkdir -p "$AGENTS" "$REPO/runs"
chmod +x "$REPO/scripts/discover.sh"
cp "$REPO/scripts/$LABEL.plist" "$PLIST"

# Retire the old daily-sync agent so the two do not both write the jobs table.
if launchctl list | grep -q "$OLD_LABEL"; then
  launchctl bootout "gui/$(id -u)/$OLD_LABEL" 2>/dev/null || true
  echo "retired $OLD_LABEL"
fi

launchctl bootout "gui/$(id -u)/$LABEL" 2>/dev/null || true
launchctl bootstrap "gui/$(id -u)" "$PLIST"
launchctl enable "gui/$(id -u)/$LABEL"

echo "installed $LABEL — every 6h + on load"
launchctl list | grep "$LABEL" || echo "(not listed — check $REPO/runs/discovery.err)"
