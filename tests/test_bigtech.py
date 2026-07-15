"""Big-tech source: fetchers against mocked HTTP, fit scoring, upsert."""

from __future__ import annotations

import httpx
import pytest

from autoapply.profile import Profile
from autoapply.sources import bigtech

WD_RESPONSE = {
    "total": 2,
    "jobPostings": [
        {
            "title": "Machine Learning Engineer, New Grad",
            "externalPath": "/job/US-CA-Santa-Clara/ML-Engineer_JR100",
            "locationsText": "US, CA, Santa Clara",
            "bulletFields": ["JR100"],
        },
        {
            "title": "Senior Staff Engineer",
            "externalPath": "/job/US-CA-Santa-Clara/Senior_JR200",
            "locationsText": "US, CA, Santa Clara",
            "bulletFields": ["JR200"],
        },
    ],
}

IBM_RESPONSE = {
    "hits": {
        "hits": [
            {
                "_id": "abcdef0123456789deadbeef",
                "_source": {
                    "title": "AI Engineer",
                    "url": "https://careers.ibm.com/careers/JobDetail?jobId=1",
                    "dcdate": "2026-07-01",
                    "field_keyword_05": "United States",
                    "field_keyword_18": "Entry Level",
                    "field_keyword_19": "San Jose, CA",
                },
            }
        ]
    }
}


def _handler(request: httpx.Request) -> httpx.Response:
    if "myworkdayjobs.com" in request.url.host:
        return httpx.Response(200, json=WD_RESPONSE)
    if request.url.host == "www-api.ibm.com":
        return httpx.Response(200, json=IBM_RESPONSE)
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
            "skills": ["Python", "PyTorch", "Machine Learning"],
        }
    )


def test_fetch_workday_normalizes(client):
    jobs = bigtech.fetch_workday("NVIDIA", client=client, searches=("x",))
    assert {j.id for j in jobs} == {"bt-nvidia-JR100", "bt-nvidia-JR200"}
    ml = next(j for j in jobs if j.id == "bt-nvidia-JR100")
    assert ml.company == "NVIDIA"
    assert ml.url.startswith("https://nvidia.wd5.myworkdayjobs.com/en-US/NVIDIAExternalCareerSite/job/")
    assert ml.locations == ["US, CA, Santa Clara"]


def test_fetch_workday_dedupes_across_searches(client):
    jobs = bigtech.fetch_workday("NVIDIA", client=client, searches=("a", "b"))
    assert len(jobs) == 2  # same two postings from both searches


def test_fetch_ibm_normalizes(client):
    jobs = bigtech.fetch_ibm(client=client, searches=("x",))
    assert len(jobs) == 1
    j = jobs[0]
    assert j.id == "bt-ibm-abcdef0123456789"
    assert j.company == "IBM"
    assert j.level == "Entry Level"
    assert j.locations == ["San Jose, CA", "United States"]
    assert j.posted == "2026-07-01"


def test_fit_score_prefers_entry_level_skill_match(client, profile):
    wd = bigtech.fetch_workday("NVIDIA", client=client, searches=("x",))
    ml = next(j for j in wd if "New Grad" in j.title)
    senior = next(j for j in wd if "Senior" in j.title)
    assert bigtech.fit_score(senior, profile) == 0  # hard exclude
    ml_score = bigtech.fit_score(ml, profile)
    assert ml_score >= 70

    ibm = bigtech.fetch_ibm(client=client, searches=("x",))[0]
    ibm_score = bigtech.fit_score(ibm, profile)
    assert 0 < ibm_score <= 100
    # entry-level metadata counts even without title hints
    ibm_no_level = bigtech.BigTechJob(
        id=ibm.id, company=ibm.company, title=ibm.title, url=ibm.url,
        locations=ibm.locations, level=None, posted=ibm.posted,
    )
    assert ibm_score >= bigtech.fit_score(ibm_no_level, profile)


def test_scout_upserts(tmp_path, monkeypatch, client, profile):
    from autoapply.config import Settings

    settings = Settings(home=tmp_path)
    monkeypatch.setattr(bigtech, "load_profile", lambda _: profile)
    monkeypatch.setattr(httpx, "Client", _mock_client)

    result = bigtech.scout(settings)
    assert result.errors == []
    assert result.upserted > 0

    from autoapply.db import connect

    conn = connect(settings.db_path)
    rows = conn.execute("SELECT id, ats, score FROM jobs WHERE id LIKE 'bt-%'").fetchall()
    conn.close()
    ids = {r["id"] for r in rows}
    assert "bt-ibm-abcdef0123456789" in ids
    assert any(r["ats"] == "workday" for r in rows)
    # idempotent re-run
    assert bigtech.scout(settings).upserted == result.upserted
