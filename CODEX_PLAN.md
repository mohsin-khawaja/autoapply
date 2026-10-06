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

## Accounts and browser sessions

The browser profile at `.autoapply/browser_data/` is a real, persistent Chrome
profile. Whatever you sign into there stays signed in across runs.

Account-walled portals (Workday, iCIMS, SmartRecruiters, Rippling) show no form
to a signed-out visitor. The runner checks `_has_portal_session` for a session
cookie on that host: signed out it records manual without wasting a page load;
signed in it fills the form like any other ATS. To enable a portal, sign in once
yourself in that profile — `uv run autoapply open <job-id>` opens it — then
later runs pick it up.

YC Work at a Startup stays manual by design even when signed in: applying there
is a message to a founder, which needs a human.

**Never automate a sign-in, store credentials in the repo, or create accounts.**
Sessions are established by the user, by hand, once.

Profile links live under `identity.links` in `profile.yaml` (linkedin, website,
github, twitter, handshake, simplify, devpost, kaggle, scholar, stackoverflow,
other_link). Empty means not provided — the field is flagged for the user, never
guessed.

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

## Startup discovery and account access

`autoapply source-access` lists all nine requested networks, the configured
profile links, and the persistent browser location. Discovery uses public GET
requests only: no browser session, applicant profile, paid API, subscriptions,
messages, endorsements, or applications are sent.

| Source | Implemented path |
|---|---|
| YC job board | Existing public new-grad feed; applying remains manual |
| Lenny's Jobs | Public Lenny 100 company data → company careers links → ATS APIs |
| Breakout List | Public company directory → careers links → ATS APIs |
| HN Who is hiring | Latest official `whoishiring` thread; top-level comments → linked ATS boards |
| Founders You Should Know | Public newsletter company links → careers links → ATS APIs |
| Ramp vendor reports | Research-only access link; rankings do not establish an open role |
| a16z Build | Public newsletter and portfolio-board ATS links |
| Next Play | Public curated company lists → careers links → ATS APIs |
| Cosign | Public jobs-page ATS links, including embedded page data |

These are bounded discovery passes, not complete copies of the networks. Each
pass rotates through up to 20 company links (30 for HN), follows at most two
same-host career pages per company, then reads current Greenhouse, Lever, or
Ashby postings. HN identifies employers; the imported vacancies come from their
current public boards, not guessed titles in comments. Reports under
`.autoapply/startup-sources/<source>.json` retain unresolved company URLs,
request failures, the next batch offset, and counts. A source with no successfully
fetched board fails visibly rather than claiming an empty successful run.

```bash
uv run autoapply source-access
uv run autoapply discover --only hackernews,lennys,breakout,foundersysk,a16zbuild,nextplay,cosign --force --no-notify
```

The normal `discover` / `away` discovery registry includes these seven sources.
Running discovery does not queue or submit applications. New posts retain the
existing title targeting, require positive US location evidence, and reject
explicit 3+ year experience requirements. Unknown/bare-Remote locations stay
out. Canonical ATS URLs dedupe against existing jobs across feeds, and reposts
of submitted company/title pairs are skipped. Only a successfully refreshed
board can retire its missing startup postings. Simplify cannot deactivate this
separate source namespace.

Profile links are not login sessions. LinkedIn, GitHub, website, and X remain
whatever `profile.yaml` actually contains. Handshake, Simplify, Devpost, Kaggle,
Scholar, Stack Overflow, and other links stay empty until the user supplies real
URLs. Do not invent them from a username. Public sources do not need accounts;
personalization, invite-only events, and membership features may.

Portal sign-in remains `uv run autoapply open <job-id>` in the tool's persistent
browser. Workday accounts can be employer-specific. Cookie presence (the PR #13
routing heuristic) is not proof of authentication or that a form is supported.
Never copy a personal Chrome cookie database into the tool. Creating credentials,
security challenges, and accepting account terms require the user where the
computer-use policy calls for handoff or confirmation.

To reuse an existing tool session from another checkout, explicitly set
`AUTOAPPLY_HOME` to that tool's `.autoapply` directory before `open`; this also
selects its database. Do not launch a second process against an in-use browser
profile. Discovery and validation for this change used a separate database copy.

## Preferred roles and explicit submission launcher

Queue priority: GTM/sales engineering first, FDE/AI/ML second, remaining eligible
software/data/product roles next. Ranking does not override seniority, location,
credential, or title-family exclusions. The explicit launcher uses score floor 1
so other eligible families remain in scope. Existing queued rows are revalidated
before filling; retry selection respects the score floor. Freshness and ATS
ordering remain tie-breakers within each priority.

`python -m autoapply.unattended --home <existing-data-dir> --profile <profile.yaml>
--env-file <existing.env> --auto-submit` runs batches of 40 with 45-minute pauses.
Omit `--auto-submit` for review-only. Add `--check` for a provider/profile/resume
preflight that does not open a browser or process applications. An exclusive data
lock prevents two instances; legacy away processes cause a startup error. Keep
an old launchd away service unloaded when using this launcher.

Lever and Ashby submission uses the integration helper in `submission.py`: exactly
one visible enabled Submit control, the existing allowlist/unresolved gate, and
native required-control validity. A visible confirmation or changed confirmation
URL establishes success; hidden HTML and form disappearance alone do not. An
uncertain click is recorded as `filled` for manual verification, never an automatic
retry. Reposts of uncertain company/title pairs are also blocked. Frozen base and
DB interfaces remain untouched. Greenhouse retains its existing submit path.

`autoapply board-open yc|handshake|simplify` opens the selected board in the tool's
persistent profile and waits for user sign-in. Stop away before using it. Optional
bookmarks in `<AUTOAPPLY_HOME>/board_access.json` override access URLs. These are
not credentials or public applicant profile links. YC founder messages and
Handshake applications remain manual; supplying their URLs does not authenticate
them or add automatic submission support. Simplify's GitHub feed already works
without authentication.
