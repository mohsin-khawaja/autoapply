# autoapply

Autonomous job-application agent. Discovers new-grad roles across Greenhouse,
Lever, Ashby and YC, scores them against your profile, fills ATS forms in a real
Playwright browser, and auto-submits the ones that resolve completely. Anything
short of complete lands on a local dashboard for you to finish.

State lives in SQLite at `.autoapply/autoapply.db`. Nothing leaves your machine
except job-board fetches and short answer-generation calls.

## Quickstart

```bash
uv sync
uv run playwright install chromium

cp .env.example .env          # add ANTHROPIC_API_KEY (and workspace id if needed)
cp /path/to/resume.pdf assets/your_resume.pdf
$EDITOR profile.yaml          # the only source of truth for your facts

uv run autoapply init         # verifies profile, resume, LLM provider, browser
uv run autoapply discover     # pull and score postings
uv run autoapply away         # discover -> queue -> apply, on a loop
```

## Unattended mode

```bash
cd ~/autoapply && caffeinate -i nohup uv run autoapply away --interval 45m --max 40 > runs/away.log 2>&1 &
pkill -f "autoapply away"; pkill -f caffeinate; pkill -f "Chrome for Testing"
```

`away` loops until killed: an empty queue sleeps and retries rather than
exiting. Each cycle prints one line:

```
cycle 3 | new 52 | queued 12 | applied 9 (2 submitted, 5 need you, 2 manual) | next wake 15:17
```

Discovery can also run on its own schedule, decoupled from applying — it is pure
HTTP and needs no browser or terminal:

```bash
./scripts/install_discovery.sh      # launchd agent, every 6h + on load
uv run autoapply discover --status  # last run per source, staleness
```

## Safety model

- **Submitting is irreversible.** Auto-submit is off unless
  `AUTOAPPLY_AUTO_SUBMIT` names an ATS, and a form submits only when *zero*
  required fields are unresolved. A form short of complete goes to the manual
  queue rather than out the door half-filled.
- **No fabricated facts.** `profile.yaml` is the only source. GPA, test scores,
  class rank and clearance are never estimated — they stay flagged for you.
- **No inverted answers.** An option that would flip a negation ("not a
  protected veteran" → "I am a veteran") or change a degree level (B.S. → MBA)
  is rejected before matching. Both were real wrong answers caught on live
  forms.
- **CAPTCHAs are never answered.** They are a human check by design.
- **No duplicate applications**, including boards reposting one req under
  several ids.

## How a field gets filled

Three tiers, cheapest first — roughly 90% never reach a model:

1. **Profile mapping** — a synonym table resolves labels to `profile.yaml` keys,
   with alias chains for the ways forms word the same fact ("B.S." vs
   "Bachelor's degree (BA/BS)" vs "Undergraduate Degree").
2. **Answer cache** — a question answered once is reused per company.
3. **LLM** — free text and unmapped dropdowns only. Provider sits behind a
   protocol (`src/autoapply/llm.py`): Anthropic by default, with automatic
   local-Ollama failover so a bad key degrades quality instead of failing the
   whole run.

## Targeting

A title must name one of these families or it scores zero: software
engineering, AI/ML, data/analytics, GTM/solutions, product. Levels above new
grad, non-US roles, language-gated gig work, and postings restricted to another
school's students are hard excludes.

## CLI

```
autoapply init                 # setup + health checks
autoapply discover             # all sources; --status, --force, --only
autoapply away                 # autonomous loop; --interval, --max, --dry-run
autoapply sync | yc | referrals
autoapply list                 # scored jobs   (--min-score N)
autoapply queue add --top N    # enqueue fillable, untried postings
autoapply run                  # one pass      (--dry-run, --auto-submit, --unattended)
autoapply skip <pattern>       # retire queued apps mid-run
autoapply dashboard            # http://127.0.0.1:8787
autoapply status | export csv | retry <id> | open <id> | answers edit <id>
```

## Outcomes

| status | meaning |
|---|---|
| `submitted` | confirmation text/URL seen, or the form cleared |
| `filled` | submit clicked, unconfirmed |
| `needs_input` | partly filled, a human answer is needed |
| `manual` | no fillable form, or an account-walled portal |
| `skipped` | retired, or a duplicate of a role already applied to |

## Config

`.env` (gitignored): `ANTHROPIC_API_KEY`, `ANTHROPIC_WORKSPACE_ID`,
`AUTOAPPLY_LLM` (`anthropic` | `ollama`), `AUTOAPPLY_ANTHROPIC_MODEL`,
`AUTOAPPLY_AUTO_SUBMIT` (comma-separated ATS allowlist).

## Development

```bash
uv run pytest && uv run ruff check
```

Check exit codes, not piped output — `pytest | tail` returns tail's status and
hides failures. `SPEC.md` has the full specification, `CLAUDE.md` the
contributor rules, `CODEX_PLAN.md` the guide for an external agent.
