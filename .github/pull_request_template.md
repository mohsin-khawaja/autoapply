<!-- SPEC.md §12 PR template -->

## Workstream
<!-- A (greenhouse) | B (ashby) | C (lever-generic) | D (llm-answers) | interface | other -->
Workstream:

## Acceptance criteria (SPEC.md §9)
<!-- Check off your workstream's row. -->
- [ ] Only my owned files changed (no edits to `ats/base.py` or `db.py`)
- [ ] `run --dry-run` prints a correct field plan for 3 real postings
- [ ] `run` fills one posting end-to-end and **pauses at the review step** (never submits)
- [ ] Tests added and green (`uv run pytest`); `uv run ruff check` clean
- [ ] No invented profile facts — values sourced from `profile.yaml` only

## Dry-run output
<!-- Paste the `run --dry-run` field-mapping output / screenshots here. -->

## Interface changes
<!-- If you need an ats/base.py change, DO NOT edit it here. Describe it so it
     goes through a separate interface PR that all branches rebase on. -->
None.
