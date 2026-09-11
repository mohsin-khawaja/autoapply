"""Build a :class:`FillPlan` from extracted form fields (SPEC.md §5).

FROZEN public entry point: :func:`build_plan`. Adapters call it after
:meth:`extract_form`; workstream D's ``llm_answers`` populates the answer cache
that free-text fields read from.

Mapping order (deterministic first, LLM last):
1. Synonym / autocomplete / name match against profile keys.
2. File inputs -> resume path.
3. Select / radio / combobox -> fuzzy-match canonical value to a visible option.
4. Free-text with no profile mapping -> mark ``source="llm"`` if a cached answer
   exists, else ``source="unmapped"`` / ``needs_input=True``.
5. Anything unresolved -> ``needs_input=True`` (red outline + terminal checklist).

This function is pure and synchronous — it never calls the network or the LLM.
It only *decides* that a field needs an LLM answer; generation happens elsewhere
so the mapper stays unit-testable.
"""

from __future__ import annotations

import hashlib
from pathlib import Path
from typing import Protocol

from rapidfuzz import fuzz, process

from autoapply.ats.base import (
    ATSKind,
    FieldPlan,
    FillPlan,
    FormField,
)
from autoapply.filling import synonyms
from autoapply.profile import Profile


class AnswerCache(Protocol):
    """Read-only view of the cached free-text answers (see :mod:`autoapply.db`)."""

    def get(self, question_hash: str, company: str) -> str | None: ...


class JobContext(Protocol):
    """Minimal job info the mapper needs. Satisfied by :class:`autoapply.db.JobRow`."""

    id: str
    company_name: str
    title: str

    # ``final_url``/``url`` — one of these is used as the plan's job_url.


#: Field keys/names that belong to bot-detection widgets, not the applicant.
#: They are invisible, unfillable, and a CAPTCHA by definition needs a human —
#: so they must never be routed to the LLM and reported as resolved.
_BOT_INFRA = ("recaptcha", "captcha", "hcaptcha", "turnstile", "honeypot", "csrf")


def is_bot_infra_field(f: FormField) -> bool:
    """True for CAPTCHA/anti-bot plumbing that no answer can legitimately fill."""
    blob = " ".join(
        t for t in (f.key, f.name or "", f.label or "", f.attrs.get("id", "")) if t
    ).lower()
    return any(marker in blob for marker in _BOT_INFRA)


def question_hash(question: str) -> str:
    """Stable hash of a free-text question, for the answer cache key."""
    return hashlib.sha256(question.strip().lower().encode()).hexdigest()[:16]


def resolve_profile_value(profile: Profile, dotted_key: str) -> str | None:
    """Resolve a dotted key (e.g. ``identity.links.github``) to a string value.

    List indices are supported (``education.0.school``). Returns None for a
    missing/empty value so the caller flags it for manual entry rather than
    fabricating (SPEC.md §4).
    """
    cur: object = profile
    for part in dotted_key.split("."):
        if part.isdigit():
            if not isinstance(cur, (list, tuple)) or int(part) >= len(cur):
                return None
            cur = cur[int(part)]
        else:
            cur = getattr(cur, part, None)
        if cur is None:
            return None
    if isinstance(cur, str):
        return cur or None
    return str(cur) if cur is not None else None


def _yes_no_option(value: str, options: list[str]) -> str | None:
    """Match a bare ``Yes``/``No`` answer to the option that begins with it.

    ATS screeners spell the choice out ("Yes, I am legally authorized to work
    in the United States for any employer") while profile.yaml stores the bare
    word. Fuzzy ratio scores that pair around 60 — under any sane threshold —
    so a correctly-mapped answer was being dropped to needs_input and blocking
    the whole application. Matching on the leading token is exact, not fuzzy:
    an option starting with "Yes," *is* the yes branch. Requires the first
    token to equal yes/no, so "Not applicable" and "None" never match "No".
    """
    want = value.strip().lower()
    if want not in ("yes", "no"):
        return None
    for opt in options:
        head = opt.strip().lower().replace(",", " ").split()
        if head and head[0].strip(".:;") == want:
            return opt
    return None


#: Words that flip an answer's meaning. Fuzzy similarity ignores them, so they
#: are checked separately.
_NEGATIONS = (" not ", "n't", " no ", "decline", "prefer not", "never")


def _is_negative(text: str) -> bool:
    return any(n in f" {text.lower().strip()} " for n in _NEGATIONS)


def polarity_ok(value: str, option: str) -> bool:
    """False when matching ``option`` would invert a negative profile answer.

    Real case: profile "I am not a protected veteran" fuzzy-matched the option
    "I am a veteran" at 85.5 — above the 82 threshold — and would have filed a
    false veteran claim on a live application. Similarity treats "not" as one
    small token; the meaning is the opposite.

    Only constrains negative profile values. A positive value ("Asian") may
    legitimately match an option containing "not" ("Asian (Not Hispanic or
    Latino)"), so that direction is left alone.
    """
    return not (_is_negative(value) and not _is_negative(option))


def _fuzzy_option(value: str, options: list[str], threshold: int) -> tuple[str | None, float]:
    """Best option match for ``value`` via rapidfuzz. Returns (option, score 0..100).

    Candidates that would invert the answer's meaning are removed before
    scoring, so a safe lower-scoring option can still win.
    """
    if not options:
        return None, 0.0
    safe = [o for o in options if polarity_ok(value, o)]
    if not safe:
        return None, 0.0
    match = process.extractOne(value, safe, scorer=fuzz.WRatio)
    if match is None:
        return None, 0.0
    option, score, _ = match
    return (option if score >= threshold else None), float(score)


#: Verifiable credentials the LLM must never invent — a made-up value here is a
#: factual misrepresentation on a real application. These stay needs_input until
#: the user supplies the real value in profile.yaml.
_UNFABRICABLE = (
    "gpa", "grade point", "sat score", "act score", "gre score", "gmat",
    "lsat", "test score", "class rank", "security clearance level",
)


def is_unfabricable_fact(f: FormField) -> bool:
    """True for fields asking a specific credential no answer can be estimated."""
    blob = " ".join(t for t in (f.label or "", f.name or "", f.key) if t).lower()
    return any(marker in blob for marker in _UNFABRICABLE)



def _is_optional_skippable(f: FormField) -> bool:
    """True when an unmapped optional field is safely left untouched.

    Only for fields where "blank" is a real answer: an unticked checkbox or an
    empty optional text box. Optional dropdowns still go through mapping, since
    some render with a pre-selected value that matters.
    """
    # Radios are included: some forms emit each choice as its own field, so a
    # single yes/no question arrives as two fields labelled "YES" and "NO" with
    # the question text lost. Those were counted as two blockers apiece.
    if f.field_type in ("checkbox", "radio") and not f.options:
        return synonyms.match_key(
            label=f.label, name=f.name or "", field_id=f.attrs.get("id", ""),
            aria=f.attrs.get("aria-label", ""), autocomplete=f.autocomplete or "",
        ) is None
    return False

def build_plan(
    fields: list[FormField],
    profile: Profile,
    answers: AnswerCache,
    job: JobContext,
    *,
    job_url: str,
    ats: ATSKind,
    resume_path: Path | None,
    fuzzy_threshold: int = 82,
) -> FillPlan:
    """Map every :class:`FormField` to a :class:`FieldPlan` and return a :class:`FillPlan`.

    ``fuzzy_threshold`` is a rapidfuzz WRatio score (0..100); option matches below
    it are downgraded to ``needs_input`` rather than guessed (SPEC.md §5.3).
    """
    planned: list[FieldPlan] = []
    for f in fields:
        planned.append(
            _plan_field(f, profile, answers, job, resume_path, fuzzy_threshold)
        )
    return FillPlan(
        job_id=job.id,
        job_url=job_url,
        ats=ats,
        resume_path=resume_path,
        fields=planned,
    )


def _plan_field(
    f: FormField,
    profile: Profile,
    answers: AnswerCache,
    job: JobContext,
    resume_path: Path | None,
    threshold: int,
) -> FieldPlan:
    # 2. File inputs -> resume.
    if f.field_type == "file":
        if resume_path is not None:
            return FieldPlan(f, resume_path, "file", 1.0, False, note="resume upload")
        return FieldPlan(f, None, "unmapped", 0.0, True, note="no resume configured")

    # 0a. An OPTIONAL field we cannot map is not a blocker — leaving it alone is
    # the correct answer. Forms carry piles of these (a 26-language checkbox
    # matrix, "Other", "Not Applicable"), and counting each as unresolved kept
    # the submit gate shut on applications that were otherwise complete.
    # Marked resolved with no value so adapters skip it and it never reaches
    # FillPlan.unresolved.
    if not f.required and _is_optional_skippable(f):
        return FieldPlan(f, None, "optional_skip", 1.0, False, note="optional — left blank")

    # 0b. Verifiable credentials are checked BEFORE any mapping. Otherwise a
    # label like "Please state the GPA and degree obtained" matches the bare
    # "state" synonym and fills the applicant's home state ("CA") into a GPA
    # box — a wrong answer on a live application, scored profile/100%, which
    # would then pass the submit gate as fully resolved.
    if is_unfabricable_fact(f):
        return FieldPlan(f, None, "unmapped", 0.0, True, note="verifiable credential — needs you")

    # 1. Deterministic profile mapping.
    key = synonyms.match_key(
        label=f.label,
        name=f.name or "",
        field_id=f.attrs.get("id", ""),
        aria=f.attrs.get("aria-label", ""),
        autocomplete=f.autocomplete or "",
    )
    if key:
        value = resolve_profile_value(profile, key)
        if value is None:
            return FieldPlan(f, None, "unmapped", 0.0, True, note=f"{key} is empty in profile")

        # 3. Constrained fields: fuzzy-match the canonical value to an option.
        if f.field_type in ("select", "radio", "combobox", "multiselect") and f.options:
            option, score = _fuzzy_option(value, f.options, threshold)
            if option is None:
                for alias in synonyms.VALUE_ALIASES.get(value.strip().lower(), ()):
                    option, score = _fuzzy_option(alias, f.options, threshold)
                    if option is not None:
                        break
            if option is None:  # bare Yes/No vs a spelled-out option
                option = _yes_no_option(value, f.options)
                if option is not None:
                    score = 100.0
            if option is None:
                # Profile has a value but no option matched — let the LLM pick
                # from the visible options (unless it's a hard credential).
                if not is_unfabricable_fact(f):
                    return FieldPlan(
                        f, None, "llm", 0.0, True,
                        note=f"{key}={value!r}: llm to choose option",
                    )
                return FieldPlan(
                    f, None, "unmapped", score / 100.0, True,
                    note=f"{key}={value!r} no option >= {threshold} (best {score:.0f})",
                )
            return FieldPlan(f, option, "profile", score / 100.0, False, note=f"{key} -> option")

        # Constrained widget whose options load asynchronously (extract saw
        # none): plan the canonical value anyway — adapters fill comboboxes by
        # type-ahead, so the widget itself constrains the final choice.
        if f.field_type in ("combobox", "select") and not f.options:
            # Raw value; the adapter expands VALUE_ALIASES variants itself.
            return FieldPlan(f, value, "profile", 0.6, False, note=f"{key} (type-ahead)")

        return FieldPlan(f, value, "profile", 1.0, False, note=key)

    # CAPTCHA plumbing: always a human, never an LLM answer.
    if is_bot_infra_field(f):
        return FieldPlan(f, None, "unmapped", 0.0, True, note="captcha — needs a human")

    # A specific verifiable credential (GPA, test score, clearance) that isn't in
    # the profile — never estimate it; a wrong number is a lie on the application.
    if is_unfabricable_fact(f):
        return FieldPlan(f, None, "unmapped", 0.0, True, note="credential not in profile")

    # 4. Free-text with no profile mapping -> LLM. Cached answer wins; otherwise
    #    the runner generates one. Both single-line and paragraph fields qualify,
    #    so best-effort estimated answers complete the form rather than blocking it.
    if f.field_type in ("textarea", "text"):
        qh = question_hash(f.label or f.key)
        cached = answers.get(qh, job.company_name)
        if cached:
            return FieldPlan(f, cached, "answer_cache", 1.0, False, note="cached answer")
        return FieldPlan(f, None, "llm", 0.0, True, note="free-text: llm to answer")

    # 5. Unmapped constrained field (no profile key matched) -> LLM picks the
    #    best option from what's visible, grounded in the profile.
    if f.field_type in ("select", "radio", "combobox", "multiselect") and f.options:
        return FieldPlan(f, None, "llm", 0.0, True, note="unmapped choice: llm to pick")

    # 6. Everything else (unknown/date/checkbox with no mapping) -> manual.
    return FieldPlan(f, None, "unmapped", 0.0, True, note="no deterministic mapping")
