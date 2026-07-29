#!/bin/bash
# Daily autoapply job sync — pulls fresh SimplifyJobs listings, scores them,
# and appends a dated summary to runs/daily_sync.log. Runs locally via launchd
# so the local SQLite DB (and the dashboard that reads it) stays current.
set -euo pipefail

REPO="/Users/mohsinkhawaja/autoapply"
UV="/Users/mohsinkhawaja/.local/bin/uv"
LOG="$REPO/runs/daily_sync.log"
cd "$REPO"
mkdir -p "$REPO/runs"

echo "===== $(date '+%Y-%m-%d %H:%M:%S') =====" >>"$LOG"
# Count high-fit jobs before, sync, count after → report new ones.
BEFORE=$("$UV" run python -c "
from autoapply.config import load_settings
from autoapply import db
c = db.connect(load_settings().db_path)
print(c.execute('SELECT COUNT(*) FROM jobs WHERE active=1 AND is_visible=1 AND score>=70').fetchone()[0])
" 2>>"$LOG" || echo 0)

"$UV" run autoapply sync >>"$LOG" 2>&1 || { echo "sync failed" >>"$LOG"; exit 1; }

AFTER=$("$UV" run python -c "
from autoapply.config import load_settings
from autoapply import db
c = db.connect(load_settings().db_path)
print(c.execute('SELECT COUNT(*) FROM jobs WHERE active=1 AND is_visible=1 AND score>=70').fetchone()[0])
" 2>>"$LOG" || echo 0)

echo "score>=70 active jobs: $BEFORE -> $AFTER" >>"$LOG"
echo "" >>"$LOG"
