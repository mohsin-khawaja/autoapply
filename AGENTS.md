# autoapply — agent rules

## Output style (caveman)
Respond terse. Fragments fine. Drop articles, filler, pleasantries, preamble,
postamble, tool-call narration. No "Great!", no restating the task, no summary
of what you just did unless asked. Code, commands, paths, error messages stay
byte-exact and complete. Drop caveman ONLY for: security warnings, destructive
or irreversible operations, interface-change proposals.

## Safety
- This tool touches LIVE job applications. Never trigger a real Submit —
  always stop at the review pause. Treat every headed-browser test as prod.
- Never invent profile facts. profile.yaml is the only source of truth.

## Worktree discipline
- You may be one of several parallel sessions in separate git worktrees.
  Run `git branch --show-current` and `git worktree list` before assuming
  anything about repo state. Stay inside your workstream's owned files (§9
  of SPEC.md). Never edit ats/base.py or db.py from a feature branch.
- Merges happen via PR only. Never push to main. Never merge locally.
- Conventional Commits, subject ≤ 50 chars.

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
