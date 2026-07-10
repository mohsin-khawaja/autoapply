"""WaaS message validation + generation (waas-addendum.md E2).

The banned-phrase / structure rules are hard requirements — a generic template
MUST fail validation. These tests are the enforcement the reviewer checks for.
"""

from __future__ import annotations

from pathlib import Path

from autoapply.filling import waas_messages as wm
from autoapply.profile import load_profile
from autoapply.sources.waas import WaasRole

REPO = Path(__file__).resolve().parents[1]
PROFILE = load_profile(REPO / "profile.yaml")

ROLE = WaasRole(
    role_id="vellum-1",
    url="https://www.workatastartup.com/jobs/vellum-1",
    company="Vellum",
    title="Founding ML Engineer",
    one_liner="Developer platform for LLM apps.",
    tech_stack="Python, PyTorch",
    description="Evaluation harness for LLM applications and prompt-routing quality.",
)

GOOD = (
    "Hi Vellum, your eval harness for LLM apps maps to what I built at Aviz. "
    "I built autonomous NetOps agents that resolved 100+ Zendesk tickets with RAG and "
    "guardrails, and shipped them into customer workflows. In month one, I'd add regression "
    "evals for your prompt-routing paths so quality changes surface before release. "
    "US work auth, SF Bay Area in-person or remote, available immediately.\n"
    "Best, Mohsin\n"
    "https://www.linkedin.com/in/mohsin-khawaja\n"
    "https://mohsinkhawaja.com"
)


def test_good_message_passes():
    assert wm.validate_message(GOOD, PROFILE, ROLE) == []


def test_banned_phrase_rejected():
    msg = GOOD.replace(
        "your eval harness for LLM apps maps to what I built at Aviz",
        "I came across your posting and I'm excited about your mission",
    )
    problems = wm.validate_message(msg, PROFILE, ROLE)
    assert any("banned generic phrase" in p for p in problems)


def test_generic_template_fails():
    # Works verbatim for any company -> no company name, no month-one, no logistics.
    generic = (
        "Hello, I'm a great fit for this role and would love to chat.\n"
        "https://www.linkedin.com/in/mohsin-khawaja"
    )
    problems = wm.validate_message(generic, PROFILE, ROLE)
    assert "hook does not name the company" in problems
    assert "missing month-one sentence" in problems
    assert "missing fixed logistics line" in problems


def test_buzzword_chain_rejected():
    msg = GOOD.replace(
        "I built autonomous NetOps agents that resolved 100+ Zendesk tickets with RAG and "
        "guardrails, and shipped them into customer workflows.",
        "I use Python PyTorch LangChain daily.",
    )
    assert any("buzzword chain" in p for p in wm.validate_message(msg, PROFILE, ROLE))


def test_em_dash_pileup_rejected():
    msg = GOOD.replace("maps to what", "maps — really — to what")
    assert any("em-dash pileup" in p for p in wm.validate_message(msg, PROFILE, ROLE))


def test_word_cap_enforced():
    filler = " ".join(["word"] * 120)
    msg = GOOD.replace("maps to what I built at Aviz", filler)
    assert any("words" in p for p in wm.validate_message(msg, PROFILE, ROLE))


def test_multiple_lists_rejected():
    msg = GOOD.replace(
        "In month one, I'd add regression evals for your prompt-routing paths so quality "
        "changes surface before release.",
        "I know backend, frontend, infra. In month one, I'd ship evals, docs, dashboards.",
    )
    problems = wm.validate_message(msg, PROFILE, ROLE)
    assert any("comma-lists" in p or "> 3 items" in p for p in problems)


# --- generation with a mocked Ollama ---------------------------------------


class _MockClient:
    def __init__(self, replies):
        self._replies = list(replies)

    def chat(self, messages, temperature=0.2, model=None):
        return self._replies.pop(0)


def test_generate_returns_valid_first_try():
    client = _MockClient([GOOD])
    msg, problems = wm.generate_message(PROFILE, ROLE, client)
    assert problems == []
    assert msg == GOOD


def test_generate_retries_until_valid():
    bad = "Hello, I came across your posting.\nhttps://www.linkedin.com/in/mohsin-khawaja"
    client = _MockClient([bad, GOOD])
    msg, problems = wm.generate_message(PROFILE, ROLE, client, max_attempts=3)
    assert problems == []
    assert msg == GOOD


def test_generate_returns_best_effort_when_all_invalid():
    client = _MockClient(["nope"] * 3)
    msg, problems = wm.generate_message(PROFILE, ROLE, client, max_attempts=3)
    assert problems  # residual violations surfaced for the review pause
