# autoapply — agent rules

## Output style (caveman)
Respond terse. Fragments fine. Drop articles, filler, pleasantries, preamble,
postamble, tool-call narration. No "Great!", no restating the task, no summary
of what you just did unless asked. Code, commands, paths, error messages stay
byte-exact and complete. Drop caveman ONLY for: security warnings, destructive
or irreversible operations, interface-change proposals.

## Safety
- This tool sends LIVE job applications under the user's name. Submitting is
  irreversible: a half-filled application burns the posting.
- Auto-submit is OFF by default and gated twice — the ATS must be in
  `Settings.auto_submit_allowlist` (env `AUTOAPPLY_AUTO_SUBMIT`, empty unless
  set) AND the plan must have zero unresolved required fields. Never widen or
  bypass that gate to raise the submit count; a form short of complete belongs
  in the manual queue.
- CAPTCHA fields are never answered — a CAPTCHA is a human check by design.
  Never fill, generate, or work around one.
- Never invent profile facts. profile.yaml is the only source of truth. If a
  field is not in the profile (GPA, citizenship, salary), flag it for the user
  rather than inferring a plausible value.
- Treat every headed-browser run as prod.

## Worktree discipline
- You may be one of several parallel sessions in separate git worktrees.
  Run `git branch --show-current` and `git worktree list` before assuming
  anything about repo state. Stay inside your workstream's owned files (§9
  of SPEC.md). Never edit ats/base.py or db.py from a feature branch.
- Merges happen via PR only. Never push to main. Never merge locally.
- Conventional Commits, subject ≤ 50 chars.

## Running it (local, no Claude API)
The whole pipeline is local: SQLite, Playwright, and Ollama for free-text
answers. Nothing here calls the Claude API.

- `./scripts/autoapply_local.sh --auto` — sync, queue, fill, submit complete
  forms, skip the rest. Stays in the terminal.
- `caffeinate -i nohup ./scripts/away_run.sh 180 &` — same, unattended: keeps
  the Mac awake and survives a closed terminal. Log: `runs/away_run.log`.
- `uv run autoapply skip <pattern>` — retire queued apps mid-run; the runner
  re-reads status before each job, so no restart needed.
- `uv run autoapply dashboard` — http://127.0.0.1:8787, includes the
  "Finish manually" card for anything needing a human.

Outcomes: `submitted` (proven — confirmation text/url or the form cleared),
`filled` (submit clicked, unconfirmed), `needs_input` (part filled, human
needed), `manual` (no fillable form / account-walled portal), `skipped`.

## Conventions
- Python 3.11+, uv, ruff, pytest. Type hints everywhere. No f-string logging
  in hot paths. Small functions, no speculative abstraction.

## Workstream file ownership (SPEC.md §9)
| Workstream | Branch | Owns (create/modify only these + own tests) |
|---|---|---|
| A | feat/greenhouse | ats/greenhouse.py, tests/test_greenhouse.py |
| B | feat/ashby | ats/ashby.py, tests/test_ashby.py |
| C | feat/lever-generic | ats/lever.py, ats/generic.py, tests |
| D | feat/llm-answers | filling/llm_answers.py, `answers` CLI body, tests |

Frozen interfaces live in `src/autoapply/ats/base.py` and `src/autoapply/db.py`.
Register an adapter from your own file via `ats.base.register(YourAdapter)` — no
edit to base.py needed. Interface changes go through a separate interface PR that
all branches rebase on.

## Commands
- `uv sync` — install deps
- `uv run playwright install chromium`
- `uv run pytest` / `uv run ruff check`
- `uv run autoapply <cmd>`
