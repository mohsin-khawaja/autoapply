# autoapply — handoff

Context for a new agentic coding tool picking up this project. Read `CLAUDE.md`
(agent rules), `SPEC.md` (design), and this file. As of 2026-07 the tool works
end to end; the notes below are what a fresh session most needs.

## What it is

A local-first job-application agent. It pulls new-grad/early-career listings,
scores them for fit against `profile.yaml`, fills the application forms in a
real browser, and — when a form fills 100% from the profile — clicks Submit.
Anything it can't complete is cached to a dashboard for the user to finish.

**100% local. No Claude/OpenAI API anywhere in the run path.** Stack:
- SQLite (`.autoapply/autoapply.db`) — jobs, applications, cached answers
- Playwright Chromium (headed) — form filling
- Ollama (`qwen2.5:7b-instruct`, falls back to any installed model) — free-text
  answers and constrained-option selection

Python 3.11+, `uv`, `ruff`, `pytest`. Type hints everywhere. 118 tests pass
(`uv run pytest`). Lint clean (`uv run ruff check`).

## Running it

```bash
uv sync
uv run playwright install chromium
uv run autoapply init        # checks profile, ollama, playwright

# hands-off, unattended, submits complete forms, skips the rest:
cd ~/autoapply && caffeinate -i nohup ./scripts/away_run.sh 180 > /dev/null 2>&1 &
tail -f runs/away_run.log     # watch
pkill -f 'autoapply run'      # stop

uv run autoapply dashboard    # http://127.0.0.1:8787
uv run autoapply skip <text>  # retire queued apps mid-run (no restart)
```

CLI: `init sync scout referrals list queue run open status export retry skip
answers dashboard`. `run` flags: `--auto-submit --unattended --max-per-run N`.
Auto-submit is gated by env `AUTOAPPLY_AUTO_SUBMIT=greenhouse,lever,ashby,generic`.

## Architecture (`src/autoapply/`)

- `sources/simplify.py` — pulls the SimplifyJobs feed, classifies ATS by URL,
  `heuristic_score()` 0–100. `sources/referral.py`, `sources/bigtech.py` (on
  branch `feat/sources-bigtech`) — Workday CXS + IBM career APIs.
- `ats/base.py` — FROZEN `BaseAdapter` ABC + `FormField`/`FillPlan`/`FillResult`
  dataclasses. `detect/extract_form/fill` per adapter; `review_pause`/`submit`
  are shared. `submit()` clicks the button and proves the outcome (confirmation
  text/url or the form clearing).
- `ats/{greenhouse,ashby,lever,generic}.py` — one adapter per ATS family.
  Registration order = priority; generic (matches any http) is last.
- `filling/synonyms.py` — form-label → dotted profile-key table + `VALUE_ALIASES`
  (degree/school spelling variants) + `_yes_no_option`.
- `filling/mapper.py` — `build_plan()`: deterministic mapping first, then LLM for
  unmapped free-text and unmatched constrained fields. `is_unfabricable_fact()`
  and `is_bot_infra_field()` are hard guards (see Guardrails).
- `filling/llm_answers.py` — `generate_answer()` (free-text), `choose_option()`
  (picks a visible option or returns None), grounded prompts.
- `runner.py` — `run_queue()` / `process_one()`: the pipeline. Browser-crash
  relaunch, per-job status re-read (for live `skip`), account-walled fast-skip,
  pacing only real attempts.
- `ollama.py`, `config.py`, `db.py` (FROZEN schema), `profile.py`, `browser.py`.

## Guardrails — do NOT weaken these to raise the submit count

1. **Two-part submit gate.** A form submits only when the ATS is in
   `Settings.auto_submit_allowlist` AND `plan.unresolved` is empty (every
   required field resolved). A half-filled application burns the posting.
2. **Never fabricate verifiable credentials.** `is_unfabricable_fact()` blocks
   GPA, SAT/ACT/GRE/GMAT/LSAT scores, class rank, clearance level from being
   LLM-estimated — a wrong number is misrepresentation on a real application.
   They stay `needs_input` until the user puts the real value in profile.yaml.
3. **Never answer CAPTCHA fields.** `is_bot_infra_field()` flags
   recaptcha/hcaptcha/turnstile/honeypot for a human.
4. **profile.yaml is the only source of truth.** The LLM prompt forbids
   inventing employers, dates, degrees, visa status, skills. `choose_option`
   replies UNKNOWN rather than assert an unsupported credential.

## Current run state

Applications: submitted 5, filled 7, needs_input 147, manual 300, skipped 90,
queued 84. The submitted count is low because the pool is dominated by
CAPTCHA-gated Ashby/Lever forms and login-walled portals (Workday/iCIMS) that
no automation can complete. Greenhouse (which auto-submits cleanly) is largely
exhausted.

**Two profile gaps block many otherwise-complete forms** (`profile.yaml`):
`gpa: ""` and `github: ""`. Filling these unblocks the SpaceX family and
several others — highest-leverage change available.

## Branches / PRs (github.com/mohsin-khawaja/autoapply, private)

Built across parallel git worktrees (see `git worktree list`), merged via PR.
Open PRs, none merged yet:
- **#8 `feat/integration`** — the run pipeline + all fill refinements. This is
  the main line; everything above lives here. HEAD `954af0f`.
- **#9 `feat/dashboard`** — dashboard + Finish-manually card.
- **#10 `feat/sources-bigtech`** — Workday/IBM scout.
- #1/#2 — WaaS (Work-at-a-Startup) workstream, separate feature.

CI runs ruff + pytest on every PR. The `@claude` review workflow fails until
the user sets the `ANTHROPIC_API_KEY` repo secret (non-blocking).

## Known issues / good next tasks

- **Ashby/Lever reCAPTCHA** can't be auto-submitted (correct behavior); they
  land in needs_input. Most of the remaining submit ceiling is here.
- **Confirmation detection** on Greenhouse job-boards is imperfect — some real
  submissions record as `filled` (submit clicked, unconfirmed) rather than
  `submitted`. See `_CONFIRMATION_NEEDLES` in `ats/base.py`.
- **More sources** would help most: wiring Lever/generic submit for more
  companies, or adding LinkedIn-referral-friendly company career pages.
- Merge PRs #8/#9/#10 to main; set the `ANTHROPIC_API_KEY` secret.
