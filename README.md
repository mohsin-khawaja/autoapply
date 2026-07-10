# autoapply

Local-first, $0-marginal-cost job-application agent. Sources new-grad roles from the
SimplifyJobs feed, scores them against your profile, auto-fills ATS forms in a **headed**
Playwright browser, and **pauses for human review before any Submit**. All inference runs
locally via Ollama; all automation runs on your machine. No paid APIs, no cloud.

## Quickstart

```bash
# 1. deps (uv manages a pinned Python 3.12)
uv sync
uv run playwright install chromium

# 2. local model (default: qwen2.5:7b-instruct)
ollama pull qwen2.5:7b-instruct

# 3. init: creates DB + dirs, validates ollama + playwright, seeds profile.yaml
uv run autoapply init

# 4. drop your resume where profile.yaml points
cp /path/to/resume.pdf assets/Mohsin_Khawaja_Resume.pdf

# 5. sync the feed and see scored jobs
uv run autoapply sync
uv run autoapply list --min-score 70
```

## Safety model
- Every field is filled, resume uploaded, then the tool **stops** in a visible browser
  for you to review and click Submit. `--auto-submit` is off by default and only works
  for ATSs on an explicit config allowlist.
- Never bypasses CAPTCHAs / bot checks — it surfaces the window and waits for you.
- Never applies to the same job twice (idempotent dedupe).
- Answers are grounded in `profile.yaml` only — no fabricated facts.

## CLI
```
autoapply init          # setup: dirs, DB, profile, health checks
autoapply sync          # pull SimplifyJobs listings
autoapply list          # scored jobs table  (--min-score N)
autoapply queue add ... # enqueue jobs        (--top N)
autoapply run           # fill -> review pause -> submit  (--dry-run, --auto-submit)
autoapply open <id>     # open manual-tier jobs in the persistent browser
autoapply status        # application tracking
autoapply export csv
autoapply answers edit <id>
autoapply retry <id>
```

See `SPEC.md` for the full specification and `CLAUDE.md` for contributor rules.

## Architecture
Milestone 0 (this) freezes every interface contract. ATS adapters and the LLM-answers
module are built in parallel worktrees against those frozen contracts. See `SPEC.md` §9.
