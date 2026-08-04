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


def test_auto_submit_clicks_submit_when_fully_resolved(
    monkeypatch, page, tmp_path, conn, feed_listings
):
    """The gate opens: allowlisted ATS + zero unresolved fields => submit() runs."""
    from autoapply import runner
    from autoapply.ats.base import ATSKind, FillResult
    from autoapply.config import Settings
    from autoapply.profile import Profile

    _seed_jobs(conn, feed_listings)
    enqueue(conn, ["bbbb2222"])
    job = runner.queued_jobs(conn, 1)[0]

    clicked: dict[str, bool] = {}

    def fake_submit(self, page, shot_path=None):
        clicked["yes"] = True
        return FillResult(status="submitted", confirmation_detected=True, notes="confirmed")

    # Fully-resolved plan so the gate's `not plan.unresolved` check passes.
    def fake_build_plan(fields, profile, answers, job_ctx, **kw):
        from autoapply.ats.base import FillPlan
        return FillPlan(
            job_id=job_ctx.id, job_url=kw["job_url"], ats=kw["ats"],
            resume_path=None, fields=[],
        )

    monkeypatch.setattr(GreenhouseAdapter, "submit", fake_submit)
    monkeypatch.setattr(runner.mapper, "build_plan", fake_build_plan)
    monkeypatch.setattr(runner.base, "resolve_adapter", lambda url: GreenhouseAdapter)
    monkeypatch.setattr(page, "goto", lambda *a, **k: None)
    monkeypatch.setattr(runner, "looks_blocked", lambda p: False)
    monkeypatch.setattr(
        GreenhouseAdapter, "extract_form",
        lambda self, pg: [
            base.FormField(key="k", field_type="text", label="Name", selector="#first_name")
        ],
    )
    monkeypatch.setattr(
        GreenhouseAdapter, "fill",
        lambda self, page, plan: FillResult(status="filled", filled_count=3),
    )
    monkeypatch.setattr(runner.console, "input", lambda *a, **k: "")

    settings = Settings(home=tmp_path)
    settings.auto_submit_allowlist = {ATSKind.GREENHOUSE}
    profile = Profile.model_validate(
        {"identity": {"first_name": "T", "last_name": "U", "email": "t@e.com", "phone": "5",
                      "location": {"city": "SD", "state": "CA", "country": "United States"}}}
    )

    page.goto((FIXTURES / "greenhouse_classic.html").as_uri())
    status = runner.process_one(
        page, job, conn=conn, profile=profile, settings=settings,
        dry_run=False, auto_submit=True, unattended=True,
    )

    assert clicked.get("yes") is True, "submit() was never called"
    assert status == "submitted"
    row = conn.execute(
        "SELECT status, submitted_at FROM applications WHERE job_id='bbbb2222'"
    ).fetchone()
    assert row["status"] == "submitted"
    assert row["submitted_at"] is not None  # timestamped as really sent


def test_auto_submit_withheld_when_ats_not_allowlisted(
    monkeypatch, page, tmp_path, conn, feed_listings
):
    """An empty allowlist must never submit, even on a fully-resolved form."""
    from autoapply import runner
    from autoapply.ats.base import FillResult
    from autoapply.config import Settings
    from autoapply.profile import Profile

    _seed_jobs(conn, feed_listings)
    enqueue(conn, ["bbbb2222"])
    job = runner.queued_jobs(conn, 1)[0]

    def must_not_run(self, page, shot_path=None):
        raise AssertionError("submit() ran with the ATS off the allowlist")

    monkeypatch.setattr(GreenhouseAdapter, "submit", must_not_run)
    monkeypatch.setattr(
        GreenhouseAdapter, "fill",
        lambda self, page, plan: FillResult(status="filled", filled_count=1),
    )
    monkeypatch.setattr(runner.base, "resolve_adapter", lambda url: GreenhouseAdapter)
    monkeypatch.setattr(page, "goto", lambda *a, **k: None)
    monkeypatch.setattr(runner, "looks_blocked", lambda p: False)
    monkeypatch.setattr(
        GreenhouseAdapter, "extract_form",
        lambda self, pg: [
            base.FormField(key="k", field_type="text", label="Name", selector="#first_name")
        ],
    )
    monkeypatch.setattr(runner.console, "input", lambda *a, **k: "")

    settings = Settings(home=tmp_path)  # allowlist empty by default
    profile = Profile.model_validate(
        {"identity": {"first_name": "T", "last_name": "U", "email": "t@e.com", "phone": "5",
                      "location": {"city": "SD", "state": "CA", "country": "United States"}}}
    )
    page.goto((FIXTURES / "greenhouse_classic.html").as_uri())
    status = runner.process_one(
        page, job, conn=conn, profile=profile, settings=settings,
        dry_run=False, auto_submit=True, unattended=True,
    )
    assert status != "submitted"


def _seeded_settings(tmp_path, feed_listings, extra=None):
    """Real on-disk DB so the CLI can open/close its own connection."""
    from autoapply.config import Settings

    settings = Settings(home=tmp_path)
    c = db.connect(settings.db_path)
    _seed_jobs(c, feed_listings)
    enqueue(c, ["aaaa1111", "bbbb2222"])
    if extra:
        extra(c)
    c.commit()
    c.close()
    return settings


def _statuses(settings):
    c = db.connect(settings.db_path)
    got = dict(c.execute("SELECT job_id, status FROM applications").fetchall())
    c.close()
    return got


def test_skip_command_retires_matching_queued_apps(monkeypatch, tmp_path, feed_listings):
    """`autoapply skip <pattern>` retires queued matches, leaves others alone."""
    from typer.testing import CliRunner

    from autoapply import cli

    settings = _seeded_settings(tmp_path, feed_listings)
    monkeypatch.setattr(cli, "load_settings", lambda: settings)

    res = CliRunner().invoke(cli.app, ["skip", "globex"])
    assert res.exit_code == 0

    got = _statuses(settings)
    assert got["bbbb2222"] == "skipped"   # matched
    assert got["aaaa1111"] == "queued"    # untouched

    CliRunner().invoke(cli.app, ["skip", "globex", "--undo"])
    assert _statuses(settings)["bbbb2222"] == "queued"


def test_skip_never_touches_submitted(monkeypatch, tmp_path, feed_listings):
    """A finished application must never be reopened by a skip."""
    from typer.testing import CliRunner

    from autoapply import cli

    settings = _seeded_settings(
        tmp_path, feed_listings,
        extra=lambda c: db.record_application(c, job_id="bbbb2222", status="submitted"),
    )
    monkeypatch.setattr(cli, "load_settings", lambda: settings)

    CliRunner().invoke(cli.app, ["skip", "globex"])
    assert _statuses(settings)["bbbb2222"] == "submitted"


def test_browser_death_is_detected():
    """A dead browser must be recognised so the batch relaunches, not grinds."""
    from autoapply.runner import _browser_is_dead

    assert _browser_is_dead(
        Exception("Page.goto: Target page, context or browser has been closed")
    )
    assert _browser_is_dead(Exception("browser has been closed"))
    # An ordinary per-job failure must NOT look like browser death, or one bad
    # posting would trigger a pointless relaunch.
    assert not _browser_is_dead(Exception("Page.fill: Timeout 8000ms exceeded"))
    assert not _browser_is_dead(Exception("no form found"))


def test_account_walled_ats_skips_without_loading(monkeypatch, tmp_path, conn, feed_listings):
    """Workday/iCIMS gate behind a login — mark manual without a page load."""
    from autoapply import runner
    from autoapply.config import Settings
    from autoapply.profile import Profile

    _seed_jobs(conn, feed_listings)
    enqueue(conn, ["dddd4444"])  # the workday listing in the fixture feed
    job = runner.queued_jobs(conn, 1)[0]
    # ats is NULL on this fixture row, so this also proves the URL fallback.
    assert job.ats is None and "myworkdayjobs" in job.url

    class _NoNav:
        def goto(self, *a, **k):
            raise AssertionError("must not load an account-walled portal")

    profile = Profile.model_validate(
        {"identity": {"first_name": "T", "last_name": "U", "email": "t@e.com", "phone": "5",
                      "location": {"city": "SD", "state": "CA", "country": "United States"}}}
    )
    status = runner.process_one(
        _NoNav(), job, conn=conn, profile=profile, settings=Settings(home=tmp_path),
        dry_run=False, auto_submit=True, unattended=True,
    )
    assert status == "manual"
    assert "account" in conn.execute(
        "SELECT notes FROM applications WHERE job_id='dddd4444'"
    ).fetchone()["notes"]
