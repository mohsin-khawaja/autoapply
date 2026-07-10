"""Schema, upsert idempotency, dedupe, answer cache (SPEC.md §6)."""

from __future__ import annotations

import json

import pytest

from autoapply import db


def _upsert(conn, jid="j1", title="ML Engineer", score=90, active=True):
    return db.upsert_job(
        conn,
        id=jid,
        company_name="Acme",
        title=title,
        url="https://jobs.ashbyhq.com/acme/1",
        final_url="https://jobs.ashbyhq.com/acme/1",
        ats="ashby",
        category="AI/ML",
        locations_json=json.dumps(["San Francisco, CA"]),
        sponsorship="Other",
        active=active,
        is_visible=True,
        score=score,
        date_posted=1_700_000_000,
        now_iso="2026-07-09T00:00:00+00:00",
    )


def test_upsert_new_then_unchanged(conn):
    assert _upsert(conn) == "new"
    assert _upsert(conn) == "unchanged"


def test_upsert_detects_change(conn):
    _upsert(conn)
    assert _upsert(conn, title="Senior ML Engineer") == "changed"


def test_list_jobs_min_score_and_order(conn):
    _upsert(conn, jid="hi", score=90)
    _upsert(conn, jid="lo", score=40)
    rows = db.list_jobs(conn, min_score=70)
    assert [r.id for r in rows] == ["hi"]
    assert rows[0].locations == ["San Francisco, CA"]


def test_mark_inactive_absent(conn):
    _upsert(conn, jid="keep")
    _upsert(conn, jid="drop")
    n = db.mark_inactive_absent(conn, {"keep"}, "2026-07-09T00:00:00+00:00")
    assert n == 1
    assert db.get_job(conn, "drop").active is False


def test_application_idempotent_and_status_validation(conn):
    _upsert(conn, jid="j1")
    db.record_application(conn, job_id="j1", status="queued")
    db.record_application(conn, job_id="j1", status="filled", filled_at="t")
    assert db.application_status(conn, "j1") == "filled"
    with pytest.raises(ValueError):
        db.record_application(conn, job_id="j1", status="bogus")


def test_answer_cache_roundtrip(conn):
    assert db.get_answer(conn, "h1", "Acme") is None
    db.put_answer(
        conn, question_hash="h1", company="Acme", question="Why us?",
        answer="Because.", now_iso="t",
    )
    assert db.get_answer(conn, "h1", "Acme") == "Because."
    db.put_answer(
        conn, question_hash="h1", company="Acme", question="Why us?",
        answer="Edited.", now_iso="t", edited=True,
    )
    assert db.get_answer(conn, "h1", "Acme") == "Edited."
