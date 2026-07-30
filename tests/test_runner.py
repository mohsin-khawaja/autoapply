"""Milestone 2 integration: queue helpers, review_pause/submit, CLI wiring."""

from __future__ import annotations

from datetime import UTC, datetime
from pathlib import Path

import pytest

from autoapply import db
from autoapply.ats import base
from autoapply.ats.greenhouse import GreenhouseAdapter
from autoapply.runner import enqueue, queued_jobs

FIXTURES = Path(__file__).parent / "fixtures"


def _seed_jobs(conn, feed_listings) -> None:
    now = datetime.now(UTC).isoformat()
    with db.transaction(conn):
        for i, item in enumerate(feed_listings):
            db.upsert_job(
                conn,
                id=item["id"],
                company_name=item["company_name"],
                title=item["title"],
                url=item["url"],
                now_iso=now,
                active=item["active"],
                is_visible=item["is_visible"],
                score=90 - i * 10,
                date_posted=item["date_posted"],
            )


def test_enqueue_and_queued_jobs(conn, feed_listings):
    _seed_jobs(conn, feed_listings)
    added = enqueue(conn, ["aaaa1111", "bbbb2222"])
    assert added == 2
    # idempotent: re-adding tracked jobs is a no-op
    assert enqueue(conn, ["aaaa1111"]) == 0

    jobs = queued_jobs(conn, limit=10)
    assert [j.job_id for j in jobs] == ["aaaa1111", "bbbb2222"]  # score desc
    assert jobs[0].company_name == "Acme AI"
    assert jobs[0].id == jobs[0].job_id  # JobContext alias


def test_queued_jobs_excludes_non_queued(conn, feed_listings):
    _seed_jobs(conn, feed_listings)
    enqueue(conn, ["aaaa1111", "bbbb2222"])
    with db.transaction(conn):
        db.record_application(conn, job_id="aaaa1111", status="submitted")
    assert [j.job_id for j in queued_jobs(conn, 10)] == ["bbbb2222"]


@pytest.fixture(scope="module")
def page():
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch()
        pg = browser.new_page()
        yield pg
        browser.close()


def _plan(job_id: str = "aaaa1111") -> base.FillPlan:
    return base.FillPlan(
        job_id=job_id, job_url="file://x", ats=base.ATSKind.GREENHOUSE,
        resume_path=None, fields=[],
    )


def test_review_pause_screenshots(page, tmp_path):
    page.goto((FIXTURES / "greenhouse_classic.html").as_uri())
    shot = GreenhouseAdapter().review_pause(page, _plan(), tmp_path / "run")
    assert shot.exists()
    assert shot.suffix == ".png"


def test_submit_no_confirmation_reports_filled(page):
    page.goto((FIXTURES / "greenhouse_classic.html").as_uri())
    result = GreenhouseAdapter().submit(page)
    # fixture form has no action -> no confirmation text appears
    assert result.status in ("filled", "failed")
    assert result.confirmation_detected is False


def test_submit_detects_confirmation(page, tmp_path):
    html = tmp_path / "confirm.html"
    html.write_text(
        "<form><button type='submit' "
        "onclick=\"event.preventDefault();document.body.innerHTML="
        "'<p>Thank you for applying!</p>'\">Send</button></form>"
    )
    page.goto(html.as_uri())
    result = GreenhouseAdapter().submit(page)
    assert result.confirmation_detected is True
    assert result.status == "submitted"


def test_cli_has_no_stubs_left():
    import inspect

    from autoapply import cli

    src = inspect.getsource(cli)
    assert "_MS2" not in src
    assert "[stub]" not in src


def test_unattended_never_waits_for_input(monkeypatch, page, tmp_path, conn, feed_listings):
    """--unattended records the outcome instead of blocking on console.input."""
    from autoapply import runner
    from autoapply.config import Settings
    from autoapply.profile import Profile

    _seed_jobs(conn, feed_listings)
    enqueue(conn, ["bbbb2222"])  # a greenhouse-classified job

    def boom(*a, **k):  # any prompt attempt is a failure of the contract
        raise AssertionError("unattended run must not call console.input")

    monkeypatch.setattr(runner.console, "input", boom)
    profile = Profile.model_validate(
        {
            "identity": {
                "first_name": "T", "last_name": "U", "email": "t@e.com", "phone": "5",
                "location": {"city": "San Diego", "state": "CA", "country": "United States"},
            }
        }
    )
    settings = Settings(home=tmp_path)
    job = runner.queued_jobs(conn, 1)[0]

    page.goto((FIXTURES / "greenhouse_classic.html").as_uri())
    status = runner.process_one(
        page, job, conn=conn, profile=profile, settings=settings,
        dry_run=False, auto_submit=False, unattended=True,
    )
    # Recorded without any prompt; a form needing a human lands as needs_input.
    assert status in ("needs_input", "filled", "manual")
    assert conn.execute(
        "SELECT status FROM applications WHERE job_id='bbbb2222'"
    ).fetchone()["status"] == status


def test_unattended_flag_reaches_run_queue(monkeypatch):
    """The CLI --unattended flag is plumbed through to run_queue."""
    from typer.testing import CliRunner

    import autoapply.runner as runner_mod
    from autoapply import cli

    seen = {}

    def fake_run_queue(settings, **kw):
        seen.update(kw)

    monkeypatch.setattr(runner_mod, "run_queue", fake_run_queue)
    CliRunner().invoke(cli.app, ["run", "--unattended", "--max-per-run", "2"])
    assert seen.get("unattended") is True
