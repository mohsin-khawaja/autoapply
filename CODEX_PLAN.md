# Running autoapply with Codex

Plan for an external coding agent (Codex, or any agent with shell access) to
operate and extend this repo. Read this before touching anything.

## What this tool is

A local-first agent that discovers job postings, scores them, fills ATS forms in
a real browser, and auto-submits the ones that resolve completely. State lives
in SQLite at `.autoapply/autoapply.db`. ~8.9K LOC, 202 tests.

Pipeline: **discover → score → queue → fill → submit**, looped by `autoapply away`.

## Where an LLM is actually used — read this before replacing anything

The LLM does **not** drive the browser. Playwright does, deterministically.
There are exactly three LLM call sites, all in
`src/autoapply/filling/llm_answers.py`:

| Call | Input | Output |
|---|---|---|
| `generate_answer` | one application question + profile facts | 2–3 sentences |
| `choose_option` | one question + the visible dropdown options | one option string |
| `choose_option` retry | same, forcing a pick | one option string |

That is the entire surface: short text in, short text out. Roughly 90% of form
fields never reach it — they resolve from `profile.yaml` or the 1.3K-entry
answer cache first.

**Therefore: computer-use is the wrong tool here.** Driving the browser by
screenshots and clicks would replace a deterministic Playwright adapter that
already knows each ATS's DOM with something slower, costlier, and far less
reliable, to do a job Playwright already does correctly. Recommended: keep
Playwright, and treat the model only as a text answerer.

## Swapping the model provider (the supported extension point)

Providers sit behind one protocol in `src/autoapply/llm.py`:

```python
class ChatClient(Protocol):
    def chat(self, messages: list[dict[str, str]], *, temperature: float = ...,
             model: str | None = ...) -> str: ...
    def health(self) -> tuple[bool, str]: ...
    def ensure_available(self) -> None: ...
```

`messages` arrives Ollama-shaped — a flat list with `system` entries inline. A
provider that wants system prompts separately must split them out itself; see
`_split_system` in `anthropic_client.py`.

To add a provider:

1. New module `src/autoapply/<name>_client.py` implementing the three methods.
2. Register it in `llm.make_client()` under a new `settings.llm_provider` value.
3. Add the env wiring in `config.load_settings()`.
4. Mirror `tests/test_anthropic_client.py` — message shaping, error mapping,
   and a test that a dead provider falls back instead of failing the run.

**Current state:** provider is `anthropic` / `claude-haiku-4-5`, working.
Ollama is already only the *fallback*, not the active provider — so "replace
Ollama" is not a change that gains anything today. Any new provider must keep a
working fallback: a dead provider used to fail every field of every application
for a whole unattended batch.

## Invariants — do not break these

These exist because each one was a real bug that put wrong data on live
applications. Every one has a regression test; run the suite before claiming
anything works.

- **Never fabricate verifiable credentials.** GPA, SAT/ACT/GRE, class rank and
  clearance never reach the LLM (`mapper.is_unfabricable_fact`).
- **Never invert an answer.** A negative profile value may not match a positive
  option — "I am not a protected veteran" matched "I am a veteran" at 85.5
  fuzzy score (`mapper.polarity_ok`).
- **Never change degree level.** "Bachelor of Science" matched "Master of
  Business Administration" at 85.5 (`mapper.level_ok`).
- **Never answer CAPTCHA fields** (`mapper.is_bot_infra_field`).
- **Submit gate:** a form submits only when the ATS is on
  `settings.auto_submit_allowlist` AND zero required fields are unresolved.
  Widening this to raise the submit count produces half-filled applications
  that burn the posting.
- **`profile.yaml` is the only source of truth** for applicant facts.
- **Never edit `ats/base.py` or `db.py` from a feature branch** (frozen
  interfaces, SPEC.md §9).

## Operating it

```bash
uv sync && uv run playwright install chromium
uv run autoapply init                      # verifies provider + browser + profile
cd ~/autoapply && caffeinate -i nohup uv run autoapply away --interval 45m --max 40 > runs/away.log 2>&1 &
pkill -f "autoapply away"; pkill -f caffeinate; pkill -f "Chrome for Testing"
```

Config is `.env` (gitignored): `ANTHROPIC_API_KEY`, `ANTHROPIC_WORKSPACE_ID`,
`AUTOAPPLY_LLM`, `AUTOAPPLY_ANTHROPIC_MODEL`, `AUTOAPPLY_AUTO_SUBMIT`.

Useful commands: `discover --status`, `status`, `queue list`, `skip <pattern>`,
`retry <job>`, `answers list`, `export`.

## Verifying a change

```bash
uv run pytest && uv run ruff check
```

Check the real exit code, not piped output — `pytest | tail -1` returns tail's
status and will hide a failure. Beyond the suite, read `runs/away.log` after a
live cycle: every correctness bug listed above was found by reading filled
values there, not by a failing test.

## Known-good failure handling

Already handled; do not re-implement: per-application 2-minute cap, browser
crash relaunch, stale Chrome profile-lock release, discovery retry with
backoff, empty-cycle survival, duplicate-req skip at run time.
