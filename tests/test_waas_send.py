"""WaaS send-flow guards: caps, dedupe, deferral, auto-submit disabled (E3)."""

from __future__ import annotations

import datetime as dt

import pytest

from autoapply import db
from autoapply.ats import waas as waas_ats
from autoapply.sources import waas as waas_src


def _job(conn, jid, company="Acme", score=90, ats="waas"):
    db.upsert_job(
        conn, id=jid, company_name=company, title="ML Engineer",
        url=f"https://www.workatastartup.com/jobs/{jid}", final_url="x", ats=ats,
        category="WaaS", locations_json="[]", sponsorship="", active=True,
        is_visible=True, score=score, date_posted=None,
        now_iso="2026-07-09T00:00:00+00:00",
    )


def test_submit_is_hard_disabled():
    adapter = waas_ats.WaasMessageAdapter()
    assert adapter.AUTO_SUBMIT_DISABLED is True
    with pytest.raises(RuntimeError):
        adapter.submit(None)


def test_detect_matches_waas_host():
    assert waas_ats.WaasMessageAdapter.detect("https://www.workatastartup.com/jobs/x")
    assert not waas_ats.WaasMessageAdapter.detect("https://jobs.lever.co/x")


def test_daily_cap_clamped_to_ceiling():
    assert waas_ats.daily_cap(10) == 10
    assert waas_ats.daily_cap(99) == waas_ats.SENDS_PER_DAY_CEILING
    assert waas_ats.daily_cap(None) == waas_ats.MAX_SENDS_PER_DAY


def test_sends_today_counts_only_today(conn):
    _job(conn, "a")
    _job(conn, "b")
    today = dt.datetime.now(dt.UTC).date().isoformat()
    db.record_application(conn, job_id="a", status="messaged", submitted_at=f"{today}T10:00:00Z")
    db.record_application(conn, job_id="b", status="messaged", submitted_at="2020-01-01T00:00:00Z")
    assert waas_ats.sends_today(conn) == 1


def test_pending_excludes_already_actioned(conn):
    _job(conn, "queued1")
    _job(conn, "sent1")
    db.record_application(conn, job_id="sent1", status="messaged")
    ids = waas_ats.pending_waas_roles(conn, min_score=70)
    assert ids == ["queued1"]


def test_messaged_companies_dedupe(conn):
    _job(conn, "j1", company="Acme")
    db.record_application(conn, job_id="j1", status="messaged")
    assert waas_src.messaged_companies(conn) == {"Acme"}


def test_defer_remaining_marks_deferred(conn):
    _job(conn, "d1")
    _job(conn, "d2")
    now = dt.datetime(2026, 7, 9, tzinfo=dt.UTC)
    waas_ats._defer_remaining(conn, ["d1", "d2"], now)
    assert db.application_status(conn, "d1") == "deferred"
    assert db.application_status(conn, "d2") == "deferred"


def test_dry_run_prints_without_recording(tmp_path):
    # Real temp DB so run_waas opens/closes its own connection normally.
    from pathlib import Path

    from autoapply.profile import load_profile

    db_path = tmp_path / "t.db"
    seed = db.connect(db_path)
    _job(seed, "vellum-1", company="Vellum")
    role = waas_src.WaasRole(
        role_id="vellum-1", url="https://www.workatastartup.com/jobs/vellum-1",
        company="Vellum", title="Founding ML Engineer",
        description="Evaluation harness for LLM applications.",
    )
    waas_src.cache_role(seed, role, "2026-07-09T00:00:00+00:00")
    seed.commit()
    seed.close()

    class _Client:
        def chat(self, messages, temperature=0.2, model=None):
            return (
                "Hi Vellum, your eval harness maps to my Aviz work. I built agents that "
                "resolved 100+ tickets. In month one, I'd add regression evals for your "
                "eval pipeline. US work auth, SF Bay Area in-person or remote, available "
                "immediately.\nhttps://www.linkedin.com/in/mohsin-khawaja"
            )

    profile = load_profile(Path(__file__).resolve().parents[1] / "profile.yaml")
    settings = type("S", (), {"db_path": db_path, "runs_dir": tmp_path})()
    stats = waas_ats.run_waas(settings, profile, _Client(), dry_run=True, max_per_run=5)
    assert stats["generated"] == 1

    check = db.connect(db_path)
    assert db.application_status(check, "vellum-1") is None  # dry-run records nothing
    check.close()
