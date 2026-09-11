"""Referral-company source: fetchers against mocked HTTP, broad-role scoring."""

from __future__ import annotations

import httpx
import pytest

from autoapply.profile import Profile
from autoapply.sources import referral
from autoapply.sources.referral import ReferralJob

AMAZON_RESPONSE = {
    "hits": 2,
    "jobs": [
        {
            "title": "Software Development Engineer I",
            "job_path": "/en/jobs/100001/sde-i",
            "normalized_location": "Seattle, Washington, USA",
            "job_category": "Software Development",
            "posted_date": "July 24, 2026",
            "id_icims": "100001",
        },
        {
            "title": "Senior Software Engineer",
            "job_path": "/en/jobs/100002/senior-swe",
            "normalized_location": "Seattle, Washington, USA",
            "job_category": "Software Development",
            "posted_date": "July 20, 2026",
            "id_icims": "100002",
        },
    ],
}

ODOO_HTML = """
<a href="/jobs/junior-software-engineer-1234"></a>
<h3 class="text-900 mb-0">Junior Software Engineer</h3>
<a href="/jobs/line-cook-buffalo-ny-1995"></a>
<h3 class="text-900 mb-0">Line Cook - Buffalo, NY</h3>
"""


def _handler(request: httpx.Request) -> httpx.Response:
    if request.url.host == "www.amazon.jobs":
        return httpx.Response(200, json=AMAZON_RESPONSE)
    if request.url.host == "www.odoo.com":
        return httpx.Response(200, text=ODOO_HTML)
    raise AssertionError(f"unexpected host {request.url.host}")


_REAL_CLIENT = httpx.Client  # captured before any monkeypatching


def _mock_client(**_kw) -> httpx.Client:
    return _REAL_CLIENT(transport=httpx.MockTransport(_handler))


@pytest.fixture
def client() -> httpx.Client:
    return _mock_client()


@pytest.fixture
def profile() -> Profile:
    return Profile.model_validate(
        {
            "identity": {
                "first_name": "Test",
                "last_name": "User",
                "email": "t@example.com",
                "phone": "555",
                "location": {"city": "San Diego", "state": "CA", "country": "United States"},
            },
            "skills": ["Python", "SQL", "Machine Learning"],
        }
    )


def _job(title: str, location: str = "Seattle, Washington, USA") -> ReferralJob:
    return ReferralJob("x", "Amazon", title, "https://u", location, None, None)


def test_fetch_amazon_normalizes(client):
    jobs = referral.fetch_amazon(client=client, searches=("x",))
    assert {j.id for j in jobs} == {"ref-amazon-100001", "ref-amazon-100002"}
    sde = next(j for j in jobs if j.id == "ref-amazon-100001")
    assert sde.company == "Amazon"
    assert sde.url == "https://www.amazon.jobs/en/jobs/100001/sde-i"
    assert sde.location == "Seattle, Washington, USA"
    assert sde.posted == "2026-07-24"  # "July 24, 2026" parsed


def test_fetch_amazon_dedupes_across_searches(client):
    jobs = referral.fetch_amazon(client=client, searches=("a", "b"))
    assert len(jobs) == 2


def test_fetch_odoo_parses_cards(client):
    jobs = referral.fetch_odoo(client=client, pages=1)
    titles = {j.title for j in jobs}
    assert "Junior Software Engineer" in titles
    assert all(j.company == "Odoo" for j in jobs)
    assert all(j.url.startswith("https://www.odoo.com/jobs/") for j in jobs)


def test_scoring_excludes_senior_and_off_profile(profile):
    assert referral.fit_score(_job("Senior Software Engineer"), profile) == 0
    assert referral.fit_score(_job("Principal Data Scientist"), profile) == 0
    assert referral.fit_score(_job("Engineering Manager"), profile) == 0
    assert referral.fit_score(_job("Line Cook - Buffalo, NY", "Buffalo"), profile) == 0
    assert referral.fit_score(_job("Account Executive"), profile) == 0


def test_scoring_excludes_non_us(profile):
    assert referral.fit_score(_job("Data Analyst", "Luxembourg"), profile) == 0


def test_scoring_includes_pm_and_analyst_roles(profile):
    """The user is open to any role — PM/analyst titles must score, not be dropped."""
    for title in (
        "Associate Product Manager",
        "Technical Program Manager",
        "Business Analyst",
        "Data Analyst",
        "Project Manager",
    ):
        assert referral.fit_score(_job(title), profile) > 0, title


def test_scoring_ranks_engineering_above_pm(profile):
    """Best-fit-first: engineering beats PM at the same seniority signal."""
    swe = referral.fit_score(_job("Software Engineer"), profile)
    pm = referral.fit_score(_job("Product Manager"), profile)
    assert swe > pm > 0


def test_entry_level_signal_boosts(profile):
    plain = referral.fit_score(_job("Software Engineer"), profile)
    grad = referral.fit_score(_job("Software Engineer, New Grad"), profile)
    assert grad > plain


def test_scout_upserts(tmp_path, monkeypatch, profile):
    from autoapply.config import Settings
    from autoapply.db import connect

    settings = Settings(home=tmp_path)
    monkeypatch.setattr(referral, "load_profile", lambda _: profile)
    monkeypatch.setattr(httpx, "Client", _mock_client)

    result = referral.scout(settings)
    assert result.errors == []
    assert result.upserted > 0

    conn = connect(settings.db_path)
    rows = conn.execute("SELECT id, score FROM jobs WHERE id LIKE 'ref-%'").fetchall()
    conn.close()
    ids = {r["id"] for r in rows}
    assert "ref-amazon-100001" in ids  # entry SDE kept
    assert "ref-amazon-100002" not in ids  # senior filtered out
    assert all(r["score"] > 0 for r in rows)
    # idempotent re-run
    assert referral.scout(settings).upserted == result.upserted
