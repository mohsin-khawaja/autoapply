# autoapply — Specification

Build **autoapply** — a local-first job application agent that sources new-grad roles, filters
them against my background, auto-fills ATS applications in a real browser, and tracks everything
in SQLite. Zero recurring cost: all inference runs locally via Ollama, all automation runs via
Playwright on my machine. No paid APIs, no cloud.

## 1. Hard constraints

- **$0 marginal cost.** Local LLM only (Ollama). No Anthropic/OpenAI API calls. No paid proxies or CAPTCHA services.
- **Local compute.** Runs on my machine (macOS/Linux). Python 3.11+.
- **Human-in-the-loop by default.** The tool fills every field, uploads the resume, then PAUSES in a headed browser for me to review and click Submit. An `--auto-submit` flag exists but is off by default and only works for ATSs on an explicit allowlist in config.
- **Never bypass CAPTCHAs or bot checks.** If one appears, surface the browser window and wait for me. Log it.
- **Idempotent.** Never apply to the same (company, title, url) twice. Dedupe before filling anything.

## 2. Tech stack

- Python 3.11+, `uv` for env management
- **Playwright** (Python, Chromium) with a **persistent browser context** (`user_data_dir`) so cookies/sessions survive across runs
- **Ollama** for local inference. Default model: `qwen2.5:7b-instruct` (fallback `llama3.1:8b`). Talk to it via `http://localhost:11434/api/chat`. Detect if Ollama isn't running and fail gracefully with instructions.
- **SQLite** (stdlib `sqlite3` or SQLModel) for job + application tracking
- **Pydantic** for the profile schema and structured LLM outputs
- **Typer** for the CLI, **Rich** for terminal output
- `rapidfuzz` for fuzzy matching dropdown options

## 3. Job sourcing

Primary source: SimplifyJobs New-Grad-Positions repo. Do NOT parse the README markdown — pull the JSON feed directly:

```
https://raw.githubusercontent.com/SimplifyJobs/New-Grad-Positions/dev/.github/scripts/listings.json
```

(Verified at implementation time: path is correct; returns a JSON array of ~17.7k listing objects.)

Each listing has fields: `active`, `category`, `company_name`, `company_url`, `date_posted` (unix
epoch int), `date_updated`, `degrees`, `id`, `is_visible`, `locations` (list[str]), `source`,
`sponsorship`, `title`, `url`. (Note: the original spec mentioned a `terms` field; it does not
exist in the live feed.)

- `autoapply sync` — fetch listings.json, upsert into SQLite, mark new/changed/inactive rows
- Resolve redirect URLs (Simplify links often redirect to the actual ATS) and classify the final URL by ATS: `jobs.ashbyhq.com`, `boards.greenhouse.io` / `job-boards.greenhouse.io`, `jobs.lever.co`, `myworkdayjobs.com`, `smartrecruiters.com`, `icims.com`, `rippling.com`, `greenhouse embedded`, `other`

**Scoring/filtering.** Score each listing 0–100 against my profile using cheap heuristics first
(title keyword match, location, sponsorship, active), then optionally a local-LLM relevance pass on
the job description for the top candidates. My targeting:

- Titles (high priority): ML Engineer, AI Engineer, Machine Learning, Applied AI, AI Solutions Engineer, Forward Deployed Engineer, Solutions Engineer
- Titles (medium): Software Engineer, SWE New Grad, Backend Engineer, Data Scientist, Product Manager (AI)
- Locations: SF Bay Area, San Jose, Berkeley, Remote (US). Willing: NYC, Seattle, San Diego
- Exclude: roles requiring 3+ YOE, clearance-required, non-US
- I do NOT require sponsorship (US work authorized) — do not filter out "no sponsorship" listings

Commands: `autoapply list --min-score 70`, `autoapply queue add <id>`, `autoapply queue add --top 20`.

## 4. Profile (single source of truth)

Store as `profile.yaml`, validated by a Pydantic model. Seeded data lives in `profile.yaml` at repo
root. Never invent facts (no fake GPA, no fake references, no fabricated dates). Anything a form
requires that isn't covered: mark the field visually (red outline via injected CSS) and list it in
the terminal for me to fill manually before submit.

## 5. Form-filling engine

**Adapter pattern.** `BaseAdapter` with `detect(url)`, `extract_form()`, `fill()`, `review_pause()`,
`submit()`. Concrete adapters, in priority order:

1. **Ashby** (`jobs.ashbyhq.com`) — SPA; `data-testid`/label-based selectors; multi-step forms + combobox widgets
2. **Greenhouse** (`boards.greenhouse.io`, `job-boards.greenhouse.io`, embedded boards) — board API `https://boards-api.greenhouse.io/v1/boards/{token}/jobs/{job_id}?questions=true` returns the exact question schema
3. **Lever** (`jobs.lever.co`) — simple single-page form
4. **SmartRecruiters, Rippling** — heuristic adapters
5. **GenericAdapter** — fallback

**Out of scope for v1:** Workday and LinkedIn Easy Apply. Classify them, keep them `manual`, provide
`autoapply open <id>` to open the posting in the persistent browser with my profile printed to the
terminal for fast manual entry.

**Field mapping.** Deterministic first, LLM last:
1. Match `autocomplete` attrs, `input[name]`, `id`, `aria-label`, `<label for=…>` text against a synonym table
2. File inputs → upload `documents.resume`
3. Selects/comboboxes/radios → fuzzy-match canonical answer against visible options (rapidfuzz); below threshold → flag for manual
4. Unmapped free-text → local LLM, strict prompt: profile + job description + question → ≤120 words, first person, plain confident tone, no buzzwords, only profile facts. Cache per (question_hash, company) in SQLite; editable via `autoapply answers edit <id>`
5. Anything unresolved → red outline + terminal checklist, wait for me

**Review flow.** After filling: screenshot to `runs/<timestamp>/<company>.png`, print a diff-style
summary of every field → value, block on my keypress (or on-page Submit click). On submit, detect
the confirmation page/text and record status.

## 6. Tracking

SQLite tables: `jobs` (score + ats + status), `applications` (job_id, filled_at, submitted_at,
status ∈ {queued, filled, needs_input, submitted, failed, manual, skipped}, screenshot, notes),
`answers` (question cache). Commands: `autoapply status`, `autoapply export csv`, `autoapply retry <id>`.

## 7. CLI surface (Typer)

```
autoapply init          # create profile.yaml, dirs, DB; check ollama + playwright install
autoapply sync          # pull SimplifyJobs listings
autoapply list          # scored jobs table
autoapply queue add ... # enqueue
autoapply run           # process queue: fill → review pause → submit
autoapply run --dry-run # fill nothing, print planned field mappings per job
autoapply open <id>     # open manual-tier jobs in persistent browser
autoapply status / export / answers edit
```

## 8. Repo layout

```
autoapply/
  cli.py
  config.py            # settings, allowlists, thresholds
  profile.py           # pydantic schema + loader
  sources/simplify.py
  ats/ (base.py, ashby.py, greenhouse.py, lever.py, generic.py, ...)
  filling/ (mapper.py, synonyms.py, llm_answers.py)
  browser.py           # persistent context management
  db.py
  assets/              # resume pdf
  runs/                # screenshots + logs
  tests/
```

(Implemented under `src/autoapply/` for a clean installable package.)

## 9. Milestones — structured for PARALLEL development

Built by multiple Claude Code sessions running simultaneously in separate git worktrees, one branch
each, merging via PRs only. Milestone 0 lands on `main` first and defines every interface contract;
the four workstreams that follow are file-disjoint.

**Milestone 0 — Foundation (serial, must merge to main before anything else):**
- Repo scaffolding, `pyproject.toml`, CLAUDE.md (§11), CI (ruff + pytest via GitHub Actions)
- `profile.py` (Pydantic schema), `db.py` (all tables), `config.py`, `browser.py` (persistent context)
- `ats/base.py` — the FROZEN `BaseAdapter` ABC: `detect(url) -> bool`, `extract_form(page) -> list[FormField]`, `fill(page, plan) -> FillResult`, plus the `FormField`/`FillPlan`/`FillResult` dataclasses
- `filling/mapper.py` interface + synonym table, `sources/simplify.py` ingestion + scoring, CLI skeleton with all commands stubbed
- Acceptance: `sync` then `list --min-score 70` shows real scored listings with ATS classification

**Parallel workstreams (one worktree + branch + PR each). Strict file ownership:**

| Workstream | Branch | Owns | Acceptance |
|---|---|---|---|
| A | `feat/greenhouse` | `ats/greenhouse.py`, `tests/test_greenhouse.py` | `run --dry-run` prints correct field plan for 3 real Greenhouse postings (question API); `run` fills one end-to-end and pauses at review |
| B | `feat/ashby` | `ats/ashby.py`, `tests/test_ashby.py` | Same bar on 3 real Ashby postings, incl. multi-step forms and combobox widgets |
| C | `feat/lever-generic` | `ats/lever.py`, `ats/generic.py`, tests | Same bar on Lever; generic adapter degrades gracefully to `needs_input` |
| D | `feat/llm-answers` | `filling/llm_answers.py`, `answers` CLI subcommand, tests | Free-text answers via local Ollama, cached in SQLite, editable via `answers edit`; unit-testable with a mocked Ollama endpoint |

**Milestone 2 — Integration (serial, after all four PRs merge):** wire adapters into `run`,
needs_input flow, screenshots, export, rate limiting, polish.

Interface changes must NOT edit `ats/base.py` in a feature branch — flag in the PR; a separate
interface PR that all branches rebase on. Test against live postings in headed mode but DO NOT
submit during development — stop at the review pause.

## 10. Non-goals / rules

- No CAPTCHA solving, no proxy rotation, no headless stealth arms race. Hard-blocked site → `manual` tier.
- No scraping LinkedIn (auth-walled + ToS). LinkedIn URL is only a profile field value.
- No mass-blasting: default rate limit 1 application per 45–90s (jittered) and `--max-per-run` (default 15).
- Keep answers honest and grounded in profile.yaml only.

## 11. CLAUDE.md

See `CLAUDE.md` at repo root (carries caveman output style, safety, worktree discipline, conventions).

## 12. GitHub + review loop

- Milestone 0 includes `gh repo create` (private) and a GitHub Actions workflow for lint + tests on every PR.
- Claude Code GitHub integration (`/install-github-app`) so `@claude` reviews every PR. A `claude-code-review` workflow checks each PR against SPEC.md — file-ownership violations, interface drift from `ats/base.py`, invented profile facts, and any code path that could auto-submit without the review pause.
- PR template: workstream letter, acceptance criteria checklist from §9, screenshots of dry-run output.
