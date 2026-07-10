# SPEC ADDENDUM — Workstream E: YC Work at a Startup (paste into Claude Code)

Extend autoapply with a **WaaS module**: source roles from workatastartup.com and send personalized interest messages to founders, using the same review-pause pattern as form fills. This is a new parallel workstream.

## Why this is different from ATS adapters

WaaS "applications" are free-text messages read by actual founders, not forms parsed by an ATS. Quality per message matters more than volume; identical mass messages get ignored and can get the account flagged. So this module is personalization-first with hard volume caps.

## E1. Auth + sourcing (`sources/waas.py`)

- Reuse the persistent Playwright context. I log into workatastartup.com manually ONCE in the persistent browser (`autoapply login waas` opens it); never store or type my password programmatically. On any logged-out detection mid-run, pause, surface the browser, wait for me.
- `autoapply sync --source waas`: browse my WaaS search feed (respect my saved filters), scrape role cards + full job pages into the same `jobs` table with `ats=waas`. Capture: company, one-liner, batch, role title, tech stack, salary/equity if shown, location/remote, and the full job description text (needed for personalization).
- Same scoring pipeline as SimplifyJobs listings. Dedupe against companies I've already messaged (any role at that company counts — founders see all messages).

## E2. Message engine (`filling/waas_messages.py`)

Generate each message with local Ollama from profile.yaml + the scraped job page. **Structure (hard requirements):**

1. **Hook (1 sentence):** names something specific about THEIR product/problem from the job description and connects it to my background. Banned: "I'm excited about your mission", "I came across your posting", any sentence that would work verbatim for a different company.
2. **Proof (1–2 sentences):** ONE relevant experience or project with a number, chosen to match their stack/problem — not a skills list. Pull from profile.yaml only.
3. **Month-one (1 sentence):** "In month one, I'd..." — a concrete, plausible first deliverable for THIS role.
4. **Logistics (1 sentence, fixed):** "US work auth, SF Bay Area in-person or remote, available immediately."
5. Sign-off with LinkedIn + site links.

**Constraints:** ≤100 words before the sign-off. Plain confident tone. Max one comma-separated list of ≤3 items in the whole message. No buzzword chains (reject any sentence containing 3+ technical nouns in a row). No em-dash pileups. Never invent facts, metrics, or claims of focus/experience not in profile.yaml — if the role wants something I don't have, the hook uses the nearest real adjacency instead of pretending.

Style reference (structure to keep, density to avoid — mine reads too keyword-stuffed):

> Hi {Company}, {hook tied to their actual product}. {One proof point with a metric}. In month one, I'd {concrete deliverable}. US work auth, SF in-person, available immediately. Would love to talk to someone on the team.

Cache generated messages in the `answers` table keyed by (company, role_id); `autoapply messages edit <id>` opens for editing before send. Regeneration allowed with `--regen`.

## E3. Send flow (`ats/waas.py`)

- `autoapply run --source waas`: for each queued role — open role page, generate (or load cached/edited) message, fill the interest textbox, screenshot, print the message to terminal, **pause for my review**. I approve/edit/skip per message. `--auto-submit` is DISABLED for waas regardless of config — founders read these; every send is human-approved. This is a hard rule, refuse to implement otherwise.
- Volume caps: max 10 sends/day (configurable, ceiling 15), 60–180s jittered between sends, stop immediately on any CAPTCHA/rate-limit signal and mark remaining queue `deferred`.
- Record sends in `applications` with status `messaged`, plus a `followups` view: companies messaged >7 days ago with no reply flag (reply tracking manual via `autoapply mark <id> replied`).

## E4. Workstream rules

- Branch `feat/waas`, owns `sources/waas.py`, `filling/waas_messages.py`, `ats/waas.py`, `messages`/`login` CLI subcommands, tests (mock Ollama + saved HTML fixtures of WaaS pages — snapshot 2–3 real pages into `tests/fixtures/`).
- Frozen interfaces from `ats/base.py` apply; if the message flow doesn't fit `BaseAdapter`, propose a `MessageAdapter` sibling ABC in the PR description — do not modify base.py on this branch.
- Same PR + @claude review flow. Reviewer should specifically check: no credential storage, auto-submit hard-disabled, banned-phrase list enforced in tests.

Acceptance: `sync --source waas` populates ≥20 real scored roles; `run --source waas --dry-run` prints generated messages for 3 roles that each name something company-specific a generic template couldn't; live run sends 1 message end-to-end after my approval.
