#!/bin/bash
# Scheduled job DISCOVERY — decoupled from apply/fill.
#
# Pure HTTP: fetches boards, scores, writes SQLite. No browser, no LLM, no
# terminal. Safe to run from launchd with no GUI session.
#
# Wired up by: scripts/install_discovery.sh  (every 6h + on load)
# Watch:       tail -f ~/autoapply/runs/discovery.log
# Status:      uv run autoapply discover --status
#
# launchd gives a job almost no environment, so PATH is set explicitly here —
# a bare "uv" resolves interactively and fails under the scheduler.
set -uo pipefail

REPO="/Users/mohsinkhawaja/autoapply"
UV="/Users/mohsinkhawaja/.local/bin/uv"
LOG="$REPO/runs/discovery.log"

export PATH="/Users/mohsinkhawaja/.local/bin:/opt/homebrew/bin:/usr/bin:/bin:/usr/sbin:/sbin"
cd "$REPO" || exit 1
mkdir -p "$REPO/runs"

echo "===== $(date '+%Y-%m-%d %H:%M:%S') discovery =====" >>"$LOG"
"$UV" run autoapply discover >>"$LOG" 2>&1
rc=$?
echo "exit=$rc" >>"$LOG"
exit $rc
