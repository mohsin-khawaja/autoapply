#!/bin/bash
# "Going to the gym" mode — start it, walk away, come back to results.
#
#   ./scripts/away_run.sh            # 100 applications
#   ./scripts/away_run.sh 40         # 40 applications
#
# Survives the terminal closing (nohup), keeps the Mac awake for the whole run
# (caffeinate), never stops for input, and submits every form that fills
# completely from profile.yaml. Anything needing a human is recorded and
# skipped to the dashboard's "Finish manually" card.
#
# Pacing stays at the SPEC §10 rate limit (45-90s jittered between apps), so
# 100 applications takes roughly 75-150 minutes. That pacing is deliberate —
# it is what keeps this a paced job search rather than a blast.
#
# Watch progress:   tail -f ~/autoapply/runs/away_run.log
# Stop early:       pkill -f 'autoapply run'
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO"

case "${1:-}" in
  -h|--help) sed -n '2,18p' "$0"; exit 0 ;;
  ""|*[!0-9]*) [ -z "${1:-}" ] || { echo "batch size must be a number (got: $1)"; exit 1; } ;;
esac
BATCH="${1:-100}"
MIN_SCORE="${MIN_SCORE:-60}"
SUBMIT_ATS="${SUBMIT_ATS:-greenhouse,lever,ashby,generic}"
LOG="$REPO/runs/away_run.log"
mkdir -p "$REPO/runs"

{
  echo "================================================================"
  echo "away run started $(date '+%Y-%m-%d %H:%M:%S')  —  batch=$BATCH  min_score=$MIN_SCORE"
  echo "================================================================"

  echo "[1/3] checking discovery freshness"
  # Discovery belongs to the scheduled agent (scripts/discover.sh), not to the
  # apply run. This only tops up when the data is already stale, so a run never
  # blocks on an 18k-listing sync and a discovery outage stays visible instead
  # of being silently papered over here.
  uv run autoapply discover || echo "  (discovery unavailable — using existing listings)"
  uv run autoapply discover --status || true

  echo "[2/3] queueing up to $BATCH NEW jobs (score >= $MIN_SCORE)"
  # queue add is INSERT OR IGNORE on job_id, so anything already attempted
  # (submitted, needs_input, manual, skipped) is never re-queued. Retrying a
  # form that needs a human just burns the batch on jobs that cannot succeed.
  uv run autoapply queue add --top "$BATCH" --min-score "$MIN_SCORE"

  echo "[3/3] applying — unattended, submitting every complete form"
  AUTOAPPLY_AUTO_SUBMIT="$SUBMIT_ATS" uv run autoapply run \
    --auto-submit --unattended --max-per-run "$BATCH"

  echo
  echo "---- submitted this run ----"
  uv run autoapply status
  echo "away run finished $(date '+%Y-%m-%d %H:%M:%S')"
} 2>&1 | tee -a "$LOG"
