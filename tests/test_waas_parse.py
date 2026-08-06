"""WaaS role-page parsing against saved HTML fixtures (waas-addendum.md E1)."""

from __future__ import annotations

from pathlib import Path

from autoapply.sources import waas

FIXTURES = Path(__file__).parent / "fixtures"


def _load(name: str) -> str:
    return (FIXTURES / name).read_text()


def test_parse_synthetic_role():
    role = waas.parse_role_page(
        _load("synthetic_role.html"),
        url="https://www.workatastartup.com/jobs/vellum-founding-ml",
    )
    assert role.company == "Vellum"
    assert role.title == "Founding ML Engineer"
    assert role.batch == "W23"
    assert "developer platform" in role.one_liner.lower()
    assert "San Francisco" in role.location
    assert "0.5%" in role.compensation
    assert "Python" in role.tech_stack
    assert "evaluation harness for LLM applications" in role.description
    assert role.role_id == "vellum-founding-ml"
    assert role.locations == ["San Francisco, CA (in-person)"]


def test_parse_falls_back_to_headings():
    html = "<h1>Acme</h1><h2>Backend Engineer</h2><body>text</body>"
    role = waas.parse_role_page(html, url="https://www.workatastartup.com/jobs/acme-be")
    assert role.company == "Acme"
    assert role.title == "Backend Engineer"


def test_score_role_reuses_simplify():
    role = waas.parse_role_page(
        _load("synthetic_role.html"),
        url="https://www.workatastartup.com/jobs/vellum-founding-ml",
    )
    # ML Engineer + SF -> high score via shared simplify heuristic.
    assert waas.score_role(role) >= 70
