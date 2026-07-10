"""profile.yaml loads and validates; frozen shape spot-checks (SPEC.md §4)."""

from __future__ import annotations

from pathlib import Path

from autoapply.filling.mapper import resolve_profile_value
from autoapply.profile import load_profile

REPO = Path(__file__).resolve().parents[1]


def test_profile_yaml_loads():
    p = load_profile(REPO / "profile.yaml")
    assert p.identity.first_name == "Mohsin"
    assert p.identity.last_name == "Khawaja"
    assert p.identity.email == "mkhawaja@ucsd.edu"
    assert len(p.experience) == 4
    assert p.answers.work_authorization_us == "Yes"
    assert p.answers.require_sponsorship == "No"
    assert p.answers.eeo.gender == "decline"


def test_dates_are_strings():
    p = load_profile(REPO / "profile.yaml")
    # YAML 2021-08 must not become a date object.
    assert isinstance(p.education[0].start, str)


def test_resolve_dotted_keys():
    p = load_profile(REPO / "profile.yaml")
    assert resolve_profile_value(p, "identity.first_name") == "Mohsin"
    assert resolve_profile_value(p, "answers.require_sponsorship") == "No"
    assert resolve_profile_value(p, "education.0.school").startswith("University of California")
    # empty optional -> None (never fabricate)
    assert resolve_profile_value(p, "identity.links.github") is None
    assert resolve_profile_value(p, "education.0.gpa") is None
