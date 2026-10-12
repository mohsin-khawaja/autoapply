"""Configurable level exceptions and durable, additive score explanations."""
from __future__ import annotations

import datetime as dt
import json
from dataclasses import replace

import pytest
from typer.testing import CliRunner

from autoapply import cli, db, scoring
from autoapply.config import Settings, load_settings
from autoapply.profile import Profile
from autoapply.sources import referral, simplify, ycombinator


def listing(title: str, **changes: object) -> simplify.Listing:
    base = simplify.Listing(
        id="test", company_name="Example", title=title, url="https://example.com/job",
        locations=["San Francisco, CA"], sponsorship="", category="", active=True,
        is_visible=True, date_posted=None,
    )
    return replace(base, **changes)


@pytest.mark.parametrize("title", [
    "Senior Forward Deployed Engineer", "FDE II", "Senior Solutions Architect",
    "Sales Engineer III", "Staff Solutions Engineer", "Principal Customer Engineer",
    "AI Engineer (5+ years)", "Staff ML Engineer", "Senior Machine Learning Engineer",
])
def test_level_exception_only(title: str) -> None:
    assert simplify.heuristic_score(listing(title)) > 0
    assert simplify.heuristic_score(listing(title), Settings(level_bypass_patterns=())) == 0


@pytest.mark.parametrize("title,changes", [
    ("Senior AI Engineer", {"locations": ["London, UK"]}),
    ("Senior AI Engineer", {"locations": ["Remote - Canada"]}),
    ("Senior AI Engineer - French", {}),
    ("Senior AI Engineer - Freelance", {}),
    ("Senior AI Engineer - UF Only", {}),
    ("Senior AI Engineer - Clearance required", {}),
    ("Senior AI Engineer Intern", {}),
    ("Senior AI Engineer", {"active": False}),
    ("Senior AI Engineer", {"is_visible": False}),
])
def test_other_exclusions_survive(title: str, changes: dict) -> None:
    result = simplify.score_breakdown(listing(title, **changes))
    assert result.total == 0
    assert result.excluded


def test_custom_allowlist_cannot_bypass_family() -> None:
    settings = Settings(level_bypass_patterns=(".*",))
    assert simplify.heuristic_score(listing("Senior Accountant"), settings) == 0
    assert not Settings().bypass_level("FDEs unrelated")


def test_config_override_and_validation(monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr("autoapply.config._load_dotenv", lambda _: None)
    monkeypatch.setenv("AUTOAPPLY_LEVEL_BYPASS_PATTERNS", '["\\\\bcustomer engineer\\\\b"]')
    monkeypatch.setenv("AUTOAPPLY_SCORE_WEIGHTS", '{"title_high": 12}')
    monkeypatch.setenv("AUTOAPPLY_MIN_SCORE", "62")
    settings = load_settings()
    assert settings.bypass_level("Senior Customer Engineer")
    assert not settings.bypass_level("Senior AI Engineer")
    assert settings.min_score == 62
    assert simplify.heuristic_score(listing("AI Engineer"), settings) == 47
    monkeypatch.setenv("AUTOAPPLY_SCORE_WEIGHTS", '{"typo": 2}')
    with pytest.raises(ValueError):
        load_settings()


def test_sync_persists_breakdown_and_updates(tmp_path, monkeypatch) -> None:
    settings = Settings(home=tmp_path)
    feed = [{
        "id": "test", "company_name": "Example", "title": "AI Engineer New Grad",
        "url": "https://example.com/job", "locations": ["San Francisco, CA"],
        "active": True, "is_visible": True,
        "date_posted": int(dt.datetime.now(dt.UTC).timestamp()),
    }]
    monkeypatch.setattr(simplify, "fetch_listings", lambda **_: feed)
    simplify.sync(settings, resolve_redirects=False)
    conn = db.connect(settings.db_path)
    row = conn.execute("SELECT * FROM job_score_breakdowns").fetchone()
    parts = json.loads(row["contributions"])
    assert parts == {"title_high": 55, "entry_level": 30, "location_high": 35,
                     "fresh_7_days": 10, "clamp": -30}
    assert sum(parts.values()) == row["total"] == 100
    settings.score_weights["title_high"] = 1
    simplify.sync(settings, resolve_redirects=False)
    assert conn.execute("SELECT total FROM job_score_breakdowns").fetchone()[0] == 76
    monkeypatch.setattr(cli, "load_settings", lambda: settings)
    result = CliRunner().invoke(cli.app, ["list", "--explain"])
    assert result.exit_code == 0, result.output
    assert "title_high=+1" in result.output
    settings.min_score = 90
    assert "no jobs" in CliRunner().invoke(cli.app, ["list"]).output
    assert "Example" in CliRunner().invoke(cli.app, ["list", "--min-score", "1"]).output
    assert "unavailable" in scoring.explain(conn, "legacy", 80)
    conn.close()


def test_yc_level_bypass_and_breakdown() -> None:
    job = ycombinator.Posting(
        id="yc-test", company_name="Example", title="Senior FDE", url="https://example.com",
        location="San Francisco, CA", min_experience="5+ years", role_type="Backend",
        job_type="Full-time", batch="S25",
    )
    result = ycombinator.score_breakdown(job)
    assert result.total == 65 == sum(result.contributions.values())
    assert ycombinator.score_posting(replace(job, title="Senior Software Engineer")) == 0
    assert ycombinator.score_posting(replace(job, title="Senior FDE - French")) == 0
    assert ycombinator.score_posting(replace(job, location="London, UK")) == 0


def test_referral_level_bypass_and_breakdown() -> None:
    profile = Profile.model_validate({"identity": {
        "first_name": "Test", "last_name": "User", "email": "test@example.com",
    }})
    job = referral.ReferralJob("ref-test", "Example", "Senior Solutions Architect",
                               "https://example.com", "Seattle, USA", None, None)
    result = referral.score_breakdown(job, profile)
    assert result.total == 54 == sum(result.contributions.values())
    assert referral.fit_score(replace(job, title="Senior Software Engineer"), profile) == 0
    assert referral.fit_score(replace(job, title="Senior AI Engineer - UF Only"), profile) == 0


def test_yc_sync_persists(tmp_path, monkeypatch) -> None:
    job = ycombinator.Posting(
        id="yc-test", company_name="Example", title="Senior FDE", url="https://example.com",
        location="San Francisco, CA", min_experience="5+ years", role_type="Backend",
        job_type="Full-time", batch="S25",
    )
    monkeypatch.setattr(ycombinator, "fetch_postings", lambda _: [job])
    settings = Settings(home=tmp_path)
    assert ycombinator.sync(settings).upserted == 1
    conn = db.connect(settings.db_path)
    row = conn.execute("SELECT total, contributions FROM job_score_breakdowns").fetchone()
    assert row[0] == sum(json.loads(row[1]).values()) == 65
    settings.level_bypass_patterns = ()
    assert ycombinator.sync(settings).upserted == 0
    assert conn.execute("SELECT score FROM jobs").fetchone()[0] == 0
    assert "level_experience" in scoring.explain(conn, job.id, 0)
    conn.close()


def test_queue_threshold_and_zero_exclusions(tmp_path, monkeypatch) -> None:
    from autoapply.away import _top_up_queue

    settings = Settings(home=tmp_path, min_score=60)
    conn = db.connect(settings.db_path)
    for job_id, score in (("excluded", 0), ("low", 40), ("high", 80)):
        db.upsert_job(conn, id=job_id, company_name=job_id, title="AI Engineer",
                      url=f"https://example.com/{job_id}", now_iso="2026-10-12T00:00:00Z",
                      ats="greenhouse", score=score)
    conn.commit()
    monkeypatch.setattr(cli, "load_settings", lambda: settings)
    result = CliRunner().invoke(cli.app, ["queue", "add", "--top", "10"])
    assert result.exit_code == 0, result.output
    queued = conn.execute("SELECT job_id FROM applications").fetchall()
    assert [r[0] for r in queued] == ["high"]
    assert _top_up_queue(conn, settings, batch=10, min_score=0) == 1
    assert conn.execute("SELECT 1 FROM applications WHERE job_id='excluded'").fetchone() is None
    conn.close()
