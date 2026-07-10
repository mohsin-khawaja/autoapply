"""WaaS founder-message generation + validation (waas-addendum.md E2).

Personalization-first: each message names something specific about THEIR product,
gives ONE proof point with a number from profile.yaml, proposes a concrete
month-one deliverable, and ends with a fixed logistics line + sign-off. Quality is
enforced by :func:`validate_message`, which the generator loops on and the tests
assert — a generic template must FAIL validation.

Facts come only from ``profile.yaml`` (SPEC.md §4; never invented).
"""

from __future__ import annotations

import re

from autoapply.ollama import OllamaClient, OllamaError
from autoapply.profile import Profile
from autoapply.sources.waas import WaasRole

# --- hard-rule constants (asserted in tests) -------------------------------

WORD_CAP = 100  # words before the sign-off (E2)

#: Generic openers/phrases that would work verbatim for any company (E2, hard rule).
BANNED_PHRASES = (
    "i'm excited about your mission",
    "i am excited about your mission",
    "i came across your posting",
    "i came across your job",
    "i saw your posting",
    "i'm a great fit",
    "i am a great fit",
    "to whom it may concern",
    "i'm passionate about",
    "i am passionate about",
    "your innovative company",
    "cutting-edge technology",
    "make a difference",
    "hit the ground running",
)

#: Technical nouns used to detect buzzword chains (3+ in a row → reject).
TECH_NOUNS = frozenset(
    {
        "python", "typescript", "javascript", "sql", "c++", "rust", "go", "golang",
        "pytorch", "tensorflow", "langchain", "rag", "mcp", "react", "docker", "git",
        "aws", "ec2", "lambda", "nlp", "llm", "llms", "ml", "ai", "cv", "kubernetes",
        "postgres", "redis", "kafka", "spark", "api", "apis", "backend", "frontend",
        "microservices", "pipeline", "pipelines", "embeddings", "vector", "transformer",
        "transformers", "inference", "fine-tuning", "guardrails", "orchestration",
    }
)

LOGISTICS = "US work auth, SF Bay Area in-person or remote, available immediately."

_CONNECTORS = {",", "/", "&", "and", "+"}


def cache_key(role: WaasRole) -> str:
    """Answer-cache key for a role's message (waas-addendum.md E2)."""
    return f"waas:{role.role_id}"


# --- validation ------------------------------------------------------------


def _split_signoff(msg: str, profile: Profile) -> tuple[str, str]:
    """Split into (body, signoff). Sign-off begins at the first link line."""
    links = [
        profile.identity.links.linkedin,
        profile.identity.links.website,
        "linkedin.com",
    ]
    lines = msg.splitlines()
    for i, line in enumerate(lines):
        if any(lnk and lnk in line for lnk in links):
            return "\n".join(lines[:i]).strip(), "\n".join(lines[i:]).strip()
    return msg.strip(), ""


def _word_count(text: str) -> int:
    return len(re.findall(r"\S+", text))


def _enumerations(text: str) -> list[int]:
    """Return item-counts of each comma-list of 3+ items, scanned per sentence.

    Sentence-scoped so commas in separate sentences don't merge into one giant
    (false) list.
    """
    counts = []
    for sentence in re.split(r"[.!?\n]", text):
        parts = [p.strip() for p in sentence.split(",") if p.strip()]
        if len(parts) >= 3:
            counts.append(len(parts))
    return counts


def _buzzword_chain(text: str) -> bool:
    """True if any sentence has 3+ technical nouns in a row (connectors ignored)."""
    for sentence in re.split(r"[.!?\n]", text):
        run = 0
        for tok in re.findall(r"[A-Za-z0-9+#./&-]+", sentence.lower()):
            if tok in _CONNECTORS:
                continue
            if tok in TECH_NOUNS:
                run += 1
                if run >= 3:
                    return True
            else:
                run = 0
    return False


def validate_message(msg: str, profile: Profile, role: WaasRole) -> list[str]:
    """Return a list of hard-rule violations. Empty list == valid (E2)."""
    problems: list[str] = []
    body, signoff = _split_signoff(msg, profile)
    low = msg.lower()

    if _word_count(body) > WORD_CAP:
        problems.append(f"body is {_word_count(body)} words (> {WORD_CAP})")

    for phrase in BANNED_PHRASES:
        if phrase in low:
            problems.append(f"banned generic phrase: {phrase!r}")

    enums = _enumerations(body)
    if any(n > 3 for n in enums):
        problems.append("a comma list has > 3 items")
    if len(enums) > 1:
        problems.append(f"{len(enums)} comma-lists (max 1)")

    if _buzzword_chain(body):
        problems.append("buzzword chain: 3+ technical nouns in a row")

    if msg.count("—") > 1:
        problems.append("em-dash pileup (> 1)")

    # structure
    if not re.match(r"\s*(hi|hey|hello)\b", low):
        problems.append("missing greeting hook")
    if "in month one" not in low:
        problems.append("missing month-one sentence")
    if "available immediately" not in low:
        problems.append("missing fixed logistics line")
    if not signoff or "linkedin.com" not in signoff.lower():
        problems.append("missing sign-off with LinkedIn link")

    # personalization: the hook must name the company
    if role.company and role.company.lower() not in low:
        problems.append("hook does not name the company")

    return problems


# --- generation ------------------------------------------------------------

_SYSTEM = (
    "You write short, specific outreach messages from a job applicant to a startup "
    "founder. Plain, confident tone. Use ONLY facts from the provided profile — never "
    "invent metrics, focus, or experience. If the role wants something not in the "
    "profile, use the nearest real adjacency instead of pretending."
)


def _prompt(profile: Profile, role: WaasRole) -> str:
    facts = _profile_facts(profile)
    return (
        f"Company: {role.company}\nRole: {role.title}\n"
        f"One-liner: {role.one_liner}\nTech stack: {role.tech_stack}\n"
        f"Job description:\n{role.description[:1500]}\n\n"
        f"My profile facts (use only these):\n{facts}\n\n"
        "Write the message with EXACTLY this structure, no headers, no bullet points:\n"
        f"1. 'Hi {role.company}, ' then ONE sentence naming something specific about "
        "their product/problem from the job description, connected to my background.\n"
        "2. One or two sentences: ONE relevant experience/project with a number, matched "
        "to their stack/problem. Not a skills list.\n"
        "3. One sentence starting 'In month one, I'd ' with a concrete first deliverable "
        "for THIS role.\n"
        f"4. This exact sentence: '{LOGISTICS}'\n"
        f"5. Sign-off with my LinkedIn ({profile.identity.links.linkedin}) and site "
        f"({profile.identity.links.website}).\n\n"
        "Rules: <= 100 words before the sign-off. At most one comma-separated list of "
        "<= 3 items. No sentence with 3+ technical nouns in a row. No em-dashes. Do not "
        "use phrases like 'excited about your mission' or 'came across your posting'."
    )


def _profile_facts(profile: Profile) -> str:
    lines = [f"Name: {profile.identity.first_name} {profile.identity.last_name}"]
    for e in profile.experience:
        lines.append(f"- {e.title} @ {e.company}: {e.summary}")
    for p in profile.projects:
        lines.append(f"- Project {p.name}: {p.blurb}")
    return "\n".join(lines)


def generate_message(
    profile: Profile,
    role: WaasRole,
    client: OllamaClient,
    *,
    max_attempts: int = 3,
) -> tuple[str, list[str]]:
    """Generate a validated message. Returns (message, remaining_violations).

    Regenerates up to ``max_attempts`` times when validation fails, feeding the
    violations back to the model. Returns the best attempt and any residual
    problems (caller flags a non-empty list at the review pause).
    """
    prompt = _prompt(profile, role)
    messages = [{"role": "system", "content": _SYSTEM}, {"role": "user", "content": prompt}]
    best: tuple[str, list[str]] | None = None
    for _ in range(max_attempts):
        try:
            out = client.chat(messages, temperature=0.4)
        except OllamaError:
            raise
        problems = validate_message(out, profile, role)
        if not problems:
            return out, []
        if best is None or len(problems) < len(best[1]):
            best = (out, problems)
        messages.append({"role": "assistant", "content": out})
        messages.append(
            {"role": "user", "content": "Fix these problems and rewrite: " + "; ".join(problems)}
        )
    return best if best else ("", ["generation produced no output"])
