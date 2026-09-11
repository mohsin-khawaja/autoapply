"""YC source: payload parsing, new-grad filtering, scoring."""

from __future__ import annotations

import html
import json

import pytest

from autoapply.sources.ycombinator import (
    Posting,
    is_new_grad,
    is_us,
    parse_postings,
    score_posting,
)


def _posting(**kw) -> Posting:
    base = {
        "id": "yc-1", "company_name": "Acme", "title": "Software Engineer",
        "url": "https://www.ycombinator.com/companies/acme/jobs/x",
        "location": "San Francisco, CA", "min_experience": "Any (new grads ok)",
        "role_type": "Backend", "job_type": "Full-time", "batch": "W22",
    }
    return Posting(**{**base, **kw})


def _page(postings: list[dict]) -> str:
    payload = json.dumps({"props": {"jobPostings": postings}})
    return f'<div id="app" data-page="{html.escape(payload, quote=True)}"></div>'


def test_parses_inertia_payload():
    posts = parse_postings(_page([
        {"id": 94934, "title": "Junior Software Engineer", "companyName": "MixRank",
         "url": "/companies/mixrank/jobs/abc", "location": "SF", "type": "Full-time",
         "minExperience": "Any (new grads ok)", "roleSpecificType": "Backend",
         "companyBatchName": "S11"},
    ]))
    assert len(posts) == 1
    p = posts[0]
    assert p.id == "yc-94934"
    assert p.company_name == "MixRank"
    # Relative listing URLs must be absolutized or the dashboard link breaks.
    assert p.url == "https://www.ycombinator.com/companies/mixrank/jobs/abc"


def test_missing_payload_raises_rather_than_silently_returning_nothing():
    """A YC redesign must be loud, not look like 'no jobs today'."""
    with pytest.raises(ValueError):
        parse_postings("<html><body>no inertia here</body></html>")


def test_new_grad_filter_uses_the_boards_own_field():
    assert is_new_grad(_posting(min_experience="Any (new grads ok)"))
    assert is_new_grad(_posting(min_experience="1+ years"))
    for bar in ("3+ years", "5+ years", "6+ years"):
        assert not is_new_grad(_posting(min_experience=bar)), bar


def test_out_of_reach_and_non_us_postings_score_zero():
    assert score_posting(_posting(min_experience="5+ years")) == 0
    assert score_posting(_posting(location="Bangalore, India")) == 0
    assert score_posting(_posting(job_type="Contract")) == 0
    assert score_posting(_posting(location="")) == 0  # unspecified is not assumed US


def test_new_grad_ml_role_outscores_a_bare_qualifying_one():
    ml = _posting(role_type="Machine learning", title="ML Engineer, New Grad")
    plain = _posting(role_type="Devops", min_experience="1+ years")
    assert score_posting(ml) > score_posting(plain) > 0


def test_us_detection():
    assert is_us(_posting(location="US / Remote"))
    assert is_us(_posting(location="New York, NY"))
    assert not is_us(_posting(location="Berlin, Germany"))
    assert not is_us(_posting(location="CO / Remote (CO)"))
