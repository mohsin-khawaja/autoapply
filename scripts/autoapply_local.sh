#!/bin/bash
# Full autoapply pipeline, 100% local — no Claude API, no tokens, no network
# calls except the job boards themselves and your local Ollama.
#
#   ./scripts/autoapply_local.sh            # find jobs, queue, fill (review pause)
#   ./scripts/autoapply_local.sh --auto     # hands-off: submit clean forms, skip the rest
#   ./scripts/autoapply_local.sh --find     # just refresh job listings, don't apply
#
# --auto never stops for input. A form that needs a human (CAPTCHA, essay,
# GPA, visa question) is recorded and skipped, and shows up in the dashboard's
# "Finish manually" card. A crashed posting is recorded failed and skipped too.
#
# Everything runs on your machine: SQLite DB, local Playwright browser, and
# Ollama for free-text answers. Ctrl-C is safe at any point.
set -euo pipefail

REPO="$(cd "$(dirname "${BASH_SOURCE[0]}")/.." && pwd)"
cd "$REPO"

TOP_N="${TOP_N:-10}"          # how many jobs to queue per run
MIN_SCORE="${MIN_SCORE:-60}"  # fit-score floor for queueing
# ATS families allowed to auto-submit. A form still only submits when EVERY
# required field filled from your profile — this just says which sites qualify.
SUBMIT_ATS="${SUBMIT_ATS:-greenhouse,lever,ashby,generic}"
SUBMIT=0
FIND_ONLY=0
for arg in "$@"; do
  case "$arg" in
    --auto|--submit) SUBMIT=1 ;;
    --find)   FIND_ONLY=1 ;;
    -h|--help) sed -n '2,12p' "$0"; exit 0 ;;
    *) echo "unknown flag: $arg (try --help)"; exit 1 ;;
  esac
done

echo "==> 1/4  Refreshing job listings (SimplifyJobs feed)"
uv run autoapply sync

echo
echo "==> 2/4  Searching referral companies (Amazon, Odoo)"
uv run autoapply referrals || echo "    (referral sources unavailable — continuing)"

if [ "$FIND_ONLY" -eq 1 ]; then
  echo
  echo "==> Done (find-only). Top matches:"
  uv run autoapply list --min-score "$MIN_SCORE" --limit 20
  exit 0
fi

echo
echo "==> 3/4  Queueing top $TOP_N jobs (score >= $MIN_SCORE)"
uv run autoapply queue add --top "$TOP_N" --min-score "$MIN_SCORE"
uv run autoapply queue list

echo
if [ "$SUBMIT" -eq 1 ]; then
  echo "==> 4/4  Hands-off: submitting clean forms, skipping any that need you"
  echo "    Real applications will be sent. Ctrl-C now to back out."
  sleep 3
  echo "    auto-submit enabled for: $SUBMIT_ATS"
  AUTOAPPLY_AUTO_SUBMIT="$SUBMIT_ATS" uv run autoapply run \
    --auto-submit --unattended --max-per-run "$TOP_N"
else
  echo "==> 4/4  Filling forms (review pause — you click Submit)"
  uv run autoapply run --max-per-run "$TOP_N"
fi

echo
echo "==> Status"
uv run autoapply status
echo
echo "Dashboard:  uv run autoapply dashboard   ->  http://127.0.0.1:8787"
