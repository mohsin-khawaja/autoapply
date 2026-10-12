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

**Scoring/filtering (implemented).** Deterministic heuristics, integer scores 0–100;
no LLM relevance pass runs. Simplify and YC use fixed title/location taxonomies,
not resume embeddings or inferred experience. Only referral scoring reads
`profile.skills` (case-insensitive title substrings). Applicant facts still come
only from `profile.yaml`; targeting configuration does not supply form answers.

### Level exceptions and hard gates

`AUTOAPPLY_LEVEL_BYPASS_PATTERNS` is a JSON array of case-insensitive Python regexes
matched against the job title. Default word-bounded patterns: `forward deployed`,
`fde`, `solutions architect`, `sales engineer`, `solutions engineer`,
`customer engineer`, `ai engineer`, `ml engineer`, `machine learning engineer`.
An empty array restores the strict level gate. A match bypasses seniority title
markers and title years-of-experience limits (and YC's structured experience bar).
It gives **no score bonus** and bypasses no other gate.

Without an exception, senior/staff/principal/lead/manager (except product manager),
director, architect, II/III/IV, leadership titles and 3–19 years in the title remain
excluded by Simplify's existing marker rules. YC also requires `Any (new grads ok)`
or `1+ years`; referral retains its additional seniority regex.

All sources enforce the shared title-family gate: software, AI/ML, data/analytics,
GTM/solutions (including standalone FDE), or product. Language/gig, school-restricted,
clearance, internship, hardware/support and research-scientist exclusions remain.
Simplify additionally checks active/visible. YC requires full-time and positive US
location evidence; referral retains its off-profile and US checks. The existing
location heuristics are not a geographic classifier: unknown Simplify locations
are not automatically rejected, and unspecified Simplify/referral locations remain
eligible. Explicit foreign locations do not become US merely by including `Remote`.
Sponsorship is neither a feature nor an exclusion.

### Features and weights

Before this change, the same numeric weights below were literals. Simplify hard
excluded all above-level titles; YC and referral had independent narrower gates.
Referral previously admitted program/project managers and business analysts outside
the shared title-family list; they now obey that list too.

Each applicable feature contributes once; alternatives within a tier are exclusive.
Keyword tables live in each source module and use case-insensitive substrings.

| Source | Config weight keys | Default contribution / trigger |
|---|---|---|
| Simplify | `title_high`, `title_medium`, `title_other` | 55 / 35 / 8; first matching HIGH_TITLES, MEDIUM_TITLES, otherwise eligible family |
| Simplify | `entry_level` | 30; any NEW_GRAD_TITLES marker |
| Simplify | `location_high`, `location_medium`, `location_us` | 35 / 20 / 8; high location (or blank), medium location, otherwise US hint |
| Simplify | `fresh_7_days`, `fresh_30_days` | 10 / 5; age ≤7 days, else ≤30 days; missing/older date adds 0 |
| YC | `yc_base`, `yc_new_grad` | 50 for eligible post; 20 for `Any (new grads ok)` |
| YC | `yc_ml_data`, `yc_software` | 25 for machine learning/data science role type; else 15 for backend/full stack/frontend |
| YC | `yc_title` | 5 for title containing machine learning, `ml `, `ai `, new grad, junior |
| Referral | `referral_base` | 12 for eligible post |
| Referral | `referral_ml`, `referral_software`, `referral_data_solutions`, `referral_product` | 55 / 50 / 42 / 38; first matching _ROLE_TIERS group |
| Referral | `referral_entry` | 18 for any _ENTRY_HINTS marker |
| Referral | `referral_skill`, `referral_skill_cap` | 5 per matching profile skill, capped at 10 total |

Referral tier groups, in order: ML/AI/applied scientist/data scientist/research
engineer; software/SDE/data engineer/full stack/backend/frontend; data/BI/analytics/
solutions; product/program/project/operations analyst. Shared hard exclusions run
before these tier matches. Simplify high locations: SF/Bay Area/San Jose/Berkeley,
Peninsula/South Bay/Oakland/remote. Medium: NYC/Seattle/San Diego. Matching behavior
is otherwise unchanged (including future dates receiving the ≤7-day freshness bonus).

`AUTOAPPLY_SCORE_WEIGHTS` is a JSON object overriding any of the keys above;
unspecified keys retain defaults. Unknown keys, non-integers, negatives and values
above 100 fail configuration loading. Sum contributions, then clamp to 0–100.
A signed `clamp` contribution records any adjustment so the stored contributions
sum exactly to the stored job score. Excluded jobs score 0 with an exclusion reason
and an empty contribution map.

### Thresholds and explanations

`AUTOAPPLY_MIN_SCORE` (integer 1–100, default **1**) controls the default for
`list`, `queue add --top`, `away`, and referral ingestion. Explicit `--min-score`
overrides it; `list --min-score 0 --explain` can inspect rejected Simplify rows.
Automatic queue selection always requires score >0 even with an explicit zero
threshold. Previously defaults differed: list/away 0, queue-top 70, referral 1.
Simplify stores all feed rows; YC stores positive-scored rows; referrals store
positive rows meeting the selected ingestion threshold. Previously stored YC/referral
rows that are now rejected still have their scores and explanations refreshed. Summary counters ≥70 and
≥50 are descriptive statistics, not eligibility gates.

Each ingested job stores its explanation transactionally alongside the job upsert
in `job_score_breakdowns` (`job_id` primary/foreign key, `source`, JSON
`contributions`, nullable `excluded`, `total`, `scored_at`). This additive table
leaves frozen `db.py` and its job row API unchanged. `autoapply list --explain`
shows stored contributions or exclusion reason. Legacy rows, or explanations whose
total differs from the current job score, say unavailable; rediscovery records new
breakdowns. Changing config does not retroactively rescore the DB or requeue any
application. No production applications are submitted during development.

Example `.env` (preferences only):

```dotenv
AUTOAPPLY_LEVEL_BYPASS_PATTERNS='["forward deployed", "\\bfde\\b", "solutions architect", "sales engineer", "solutions engineer", "customer engineer", "ai engineer", "ml engineer"]'
AUTOAPPLY_SCORE_WEIGHTS='{"title_high":55,"entry_level":20,"location_high":25}'
AUTOAPPLY_MIN_SCORE=60
```

Commands: `autoapply sync`, `autoapply discover`, `autoapply list --explain`,
`autoapply list --min-score 70 --explain`, `autoapply queue add --top 20`.

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
