from datetime import UTC, datetime

import pytest

from autoapply import db
from autoapply.away import _prune_queue, _requeue_stalled, _top_up_queue
from autoapply.config import Settings
from autoapply.priorities import role_priority
from autoapply.runner import enqueue, queued_jobs
from autoapply.sources.simplify import Listing, heuristic_score


def add_job(conn, jid, title, score=80, ats="ashby"):
    db.upsert_job(
        conn,
        id=jid,
        company_name=jid,
        title=title,
        url=f"https://jobs.ashbyhq.com/{jid}/123",
        ats=ats,
        locations_json='["San Francisco, CA"]',
        score=score,
        now_iso=datetime.now(UTC).isoformat(),
    )
    conn.commit()


def test_priority_precedes_ats_and_existing_score(conn):
    for jid, title, score, ats in [
        ("swe", "Software Engineer New Grad", 100, "greenhouse"),
        ("ml", "Machine Learning Engineer", 80, "ashby"),
        ("sales", "Sales Engineer", 70, "lever"),
        ("gtm", "GTM Engineer", 85, "ashby"),
    ]:
        add_job(conn, jid, title, score, ats)
    assert _top_up_queue(conn, Settings(), batch=4, min_score=1) == 4
    jobs = queued_jobs(conn, 4)
    assert {j.job_id for j in jobs[:2]} == {"sales", "gtm"}
    assert [j.job_id for j in jobs[2:]] == ["ml", "swe"]


def test_top_up_small_batch_does_not_starve_sales(conn):
    add_job(conn, "swe", "Software Engineer", 100, "greenhouse")
    add_job(conn, "sales", "Sales Engineer", 70, "lever")
    _top_up_queue(conn, Settings(), batch=1, min_score=1)
    assert [j.job_id for j in queued_jobs(conn, 10)] == ["sales"]


def test_prune_excluded_existing_queue_and_keep_other_families(conn):
    add_job(conn, "senior", "Principal GTM Engineer", 100)
    add_job(conn, "data", "Data Analyst New Grad", 60)
    enqueue(conn, ["senior", "data"])
    assert _prune_queue(conn, min_score=1) == 1
    assert [j.job_id for j in queued_jobs(conn, 10)] == ["data"]


def test_retry_preserves_floor_and_ignores_uncertain_submissions(conn):
    for jid, score, status in [
        ("low", 20, "needs_input"),
        ("ok", 90, "needs_input"),
        ("uncertain", 100, "filled"),
    ]:
        add_job(conn, jid, "Sales Engineer", score)
        db.record_application(conn, job_id=jid, status=status)
    assert _requeue_stalled(conn, limit=10, min_score=70) == 1
    assert [j.job_id for j in queued_jobs(conn, 10)] == ["ok"]


@pytest.mark.parametrize(
    "title,priority",
    [
        ("GTM Engineer", 2),
        ("Sales Engineer", 2),
        ("Go-to-market Engineer", 2),
        ("FDE", 1),
        ("Forward-Deployed Engineer", 1),
        ("AI/ML Engineer", 1),
        ("Software Engineer", 0),
    ],
)
def test_role_priority(title, priority):
    assert role_priority(title) == priority


@pytest.mark.parametrize(
    "title", ["Senior Sales Engineer", "Principal FDE", "AI Trainer - Bosnian"]
)
def test_priority_never_overrides_exclusion(title):
    listing = Listing(
        "id", "co", title, "https://example.com", ["San Francisco, CA"], "", "", True, True, None
    )
    assert heuristic_score(listing) == 0


def test_repost_of_uncertain_submission_is_blocked(conn):
    from autoapply.runner import QueuedJob, _already_submitted

    add_job(conn, "original", "Sales Engineer")
    db.record_application(conn, job_id="original", status="filled")
    repost = QueuedJob("repost", "original", "Sales Engineer", "https://example.com", "lever")
    assert _already_submitted(conn, repost)
