"""LLM free-text answer generation via local Ollama (workstream D, SPEC.md §5).

Answers are grounded STRICTLY in profile.yaml — the prompt forbids inventing
employers, dates, degrees, visa status, or skills (SPEC.md §4, §10). Generated
answers are cached in the ``answers`` table keyed by
(:func:`autoapply.filling.mapper.question_hash`, company) and are editable via
``autoapply answers edit``.

No network happens at import time; the Ollama call is inside
:func:`generate_answer` only.
"""

from __future__ import annotations

import sqlite3
from datetime import UTC, datetime
from typing import Protocol

from autoapply import db
from autoapply.filling.mapper import question_hash
from autoapply.llm import ChatClient
from autoapply.profile import Profile

MAX_WORDS_DEFAULT = 120

_SYSTEM_PROMPT = (
    "You write short first-person answers to job application questions on "
    "behalf of the candidate described below.\n"
    "Rules — follow every one strictly:\n"
    "- Ground every statement in the CANDIDATE PROFILE facts. Never invent "
    "employers, job titles, dates, degrees, schools, visa or work-authorization "
    "status, certifications, or skills that are not in the profile.\n"
    "- If the profile lacks the information needed, say so briefly rather than "
    "guessing or fabricating.\n"
    f"- Be concise: at most {MAX_WORDS_DEFAULT} words unless the question "
    "explicitly asks for more.\n"
    "- Write in the first person, plain professional prose. No markdown, no "
    "bullet lists, no preamble — output the answer text only."
)


class JobContext(Protocol):
    """Minimal job info needed for prompting. Satisfied by :class:`autoapply.db.JobRow`."""

    company_name: str
    title: str


_OPTION_SYSTEM_PROMPT = (
    "You pick the single best answer to a multiple-choice job application "
    "question on behalf of the candidate described below.\n"
    "Rules — follow every one strictly:\n"
    "- Choose exactly ONE option from the numbered list. Reply with only that "
    "option's exact text, copied verbatim. No number, no punctuation, no "
    "explanation.\n"
    "- Base the choice on the CANDIDATE PROFILE. For a question the profile "
    "answers directly (work authorization, sponsorship, relocation, employment "
    "history), pick the option that matches the profile.\n"
    "- For a reasonable-judgment question the profile does not settle, pick the "
    "option a typical applicant in the candidate's position would choose — "
    "prefer the neutral, non-disqualifying, honest option.\n"
    "- Never pick an option that asserts a specific credential the profile does "
    "not support (a GPA, a test score, a clearance, a degree not held). If every "
    "option would require inventing such a fact, reply with exactly: UNKNOWN"
)


def profile_facts(profile: Profile) -> str:
    """Render the profile as a compact plain-text fact sheet for the prompt."""
    ident = profile.identity
    lines = [f"Name: {ident.first_name} {ident.last_name}"]
    loc = ident.location
    if loc.city or loc.state or loc.country:
        lines.append("Location: " + ", ".join(p for p in (loc.city, loc.state, loc.country) if p))
    for e in profile.education:
        deg = " ".join(p for p in (e.degree, e.major) if p)
        span = f" ({e.start}–{e.end})" if e.start or e.end else ""
        lines.append(f"Education: {deg or 'studied'} at {e.school}{span}")
    for x in profile.experience:
        span = f" ({x.start}–{x.end})" if x.start or x.end else ""
        summary = f" — {x.summary}" if x.summary else ""
        lines.append(f"Experience: {x.title or 'worked'} at {x.company}{span}{summary}")
    for p in profile.projects:
        blurb = f" — {p.blurb}" if p.blurb else ""
        lines.append(f"Project: {p.name}{blurb}")
    if profile.skills:
        lines.append("Skills: " + ", ".join(profile.skills))
    a = profile.answers
    lines.append(f"Authorized to work in the US: {a.work_authorization_us}")
    lines.append(f"Requires visa sponsorship: {a.require_sponsorship}")
    return "\n".join(lines)


def build_messages(question: str, profile: Profile, job: JobContext) -> list[dict[str, str]]:
    """Build the chat messages for one application question."""
    user = (
        f"CANDIDATE PROFILE:\n{profile_facts(profile)}\n\n"
        f"JOB: {job.title} at {job.company_name}\n\n"
        f"APPLICATION QUESTION:\n{question}\n\n"
        "Answer as the candidate."
    )
    return [
        {"role": "system", "content": _SYSTEM_PROMPT},
        {"role": "user", "content": user},
    ]


def generate_answer(
    question: str,
    profile: Profile,
    job: JobContext,
    client: ChatClient,
    *,
    temperature: float = 0.2,
) -> str:
    """Generate a grounded first-person answer via the local Ollama model."""
    text = client.chat(build_messages(question, profile, job), temperature=temperature)
    return text.strip()


def choose_option(
    question: str,
    options: list[str],
    profile: Profile,
    job: JobContext,
    client: ChatClient,
    *,
    temperature: float = 0.0,
) -> str | None:
    """Pick the best option for a constrained question, or None if genuinely unknown.

    Returns an option string only when the model's reply matches one of
    ``options`` (case-insensitive, whitespace-normalized). A reply of UNKNOWN,
    or anything that does not match an option, returns None so the field stays
    needs_input rather than being filled with an invented answer.
    """
    if not options:
        return None
    numbered = "\n".join(f"{i + 1}. {o}" for i, o in enumerate(options))
    user = (
        f"CANDIDATE PROFILE:\n{profile_facts(profile)}\n\n"
        f"JOB: {job.title} at {job.company_name}\n\n"
        f"QUESTION:\n{question}\n\nOPTIONS:\n{numbered}\n\n"
        "Reply with the exact text of the single best option."
    )
    reply = client.chat(
        [
            {"role": "system", "content": _OPTION_SYSTEM_PROMPT},
            {"role": "user", "content": user},
        ],
        temperature=temperature,
    ).strip()
    if not reply or reply.upper() == "UNKNOWN":
        return None
    norm = " ".join(reply.lower().split())
    for opt in options:
        if " ".join(opt.lower().split()) == norm:
            return opt
    # Model sometimes returns "2" or "2. Yes" — recover a leading index.
    head = reply.split(".")[0].split(")")[0].strip()
    if head.isdigit() and 1 <= int(head) <= len(options):
        return options[int(head) - 1]
    return None


def get_or_generate(
    conn: sqlite3.Connection,
    question: str,
    company: str,
    *,
    profile: Profile,
    job: JobContext,
    client: ChatClient,
) -> str:
    """Return the cached answer for (question, company), generating on a miss.

    Cache key is :func:`question_hash` + company; fresh answers are stored via
    :func:`autoapply.db.put_answer` before returning.
    """
    qh = question_hash(question)
    cached = db.get_answer(conn, qh, company)
    if cached is not None:
        return cached
    answer = generate_answer(question, profile, job, client)
    with db.transaction(conn):
        db.put_answer(
            conn,
            question_hash=qh,
            company=company,
            question=question,
            answer=answer,
            now_iso=datetime.now(UTC).isoformat(),
        )
    return answer
