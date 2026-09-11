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


def test_job_deadline_interrupts_a_hung_job():
    """The cap must fire on blocking work, not merely between operations."""
    import time as _time

    from autoapply.runner import JobTimeout, job_deadline

    started = _time.monotonic()
    with pytest.raises(JobTimeout):
        with job_deadline(0.3):
            _time.sleep(30)  # stands in for a wedged Playwright call
    assert _time.monotonic() - started < 5, "deadline did not interrupt the blocking call"


def test_job_deadline_is_disarmed_after_use():
    """A completed job must not leave a timer armed for the next one."""
    import time as _time

    from autoapply.runner import job_deadline

    with job_deadline(0.3):
        pass
    _time.sleep(0.5)  # the old timer would fire here if it were still armed


def test_job_deadline_disabled_when_zero():
    import time as _time

    from autoapply.runner import job_deadline

    with job_deadline(0):
        _time.sleep(0.4)


def test_queue_add_top_skips_tracked_and_login_walled(monkeypatch, tmp_path, conn):
    """--top N must yield N *new, fillable* jobs, not N wasted slots."""
    from typer.testing import CliRunner

    from autoapply import cli, db
    from autoapply.config import Settings

    now = datetime.now(UTC).isoformat()
    # Highest scores are all either already attempted or login-walled; the only
    # fresh fillable job scores lowest, so naive top-N ordering would miss it.
    rows = [
        ("done1", "greenhouse", 100), ("done2", "lever", 99),
        ("walled1", "workday", 98), ("walled2", "yc", 97),
        ("fresh1", "greenhouse", 80),
    ]
    for jid, ats, score in rows:
        db.upsert_job(
            conn, id=jid, company_name="C", title="Software Engineer I",
            url=f"https://boards.greenhouse.io/{jid}", now_iso=now, ats=ats, score=score,
        )
    db.record_application(conn, job_id="done1", status="submitted")
    db.record_application(conn, job_id="done2", status="manual")
    conn.commit()

    settings = Settings(home=tmp_path)
    monkeypatch.setattr(settings.__class__, "db_path", property(lambda s: tmp_path / "x.db"))
    monkeypatch.setattr(cli, "load_settings", lambda: settings)
    class _KeepOpen:  # the CLI closes what it opens; the test still needs it
        def __getattr__(self, name):
            return getattr(conn, name)

        def close(self):
            pass

    monkeypatch.setattr(db, "connect", lambda _p: _KeepOpen())

    CliRunner().invoke(cli.app, ["queue", "add", "--top", "3", "--min-score", "50"])
    queued = {
        r["job_id"]
        for r in conn.execute("SELECT job_id FROM applications WHERE status='queued'")
    }
    assert queued == {"fresh1"}, queued


def test_max_per_run_zero_applies_to_nothing(tmp_path, conn, feed_listings):
    """An explicit 0 must not fall back to the default batch size."""
    from autoapply import runner

    _seed_jobs(conn, feed_listings)
    enqueue(conn, [r["id"] for r in conn.execute("SELECT id FROM jobs")])
    conn.commit()
    assert runner.queued_jobs(conn, 0) == []


def test_interval_parsing():
    from autoapply.cli import _parse_interval

    assert _parse_interval("45m") == 2700.0
    assert _parse_interval("2h") == 7200.0
    assert _parse_interval("90") == 90.0
    import typer

    for bad in ("nonsense", "10s"):  # 10s is below the 30s floor
        with pytest.raises(typer.BadParameter):
            _parse_interval(bad)


def test_away_survives_an_empty_cycle(monkeypatch, tmp_path):
    """The whole point: an empty queue must sleep and retry, never exit."""
    from autoapply import away as away_mod
    from autoapply.config import Settings

    cycles = []

    def fake_run_cycle(settings, *, cycle, batch, min_score, auto_submit):
        cycles.append(cycle)
        return away_mod.CycleResult(cycle=cycle)  # all zeros — nothing to do

    monkeypatch.setattr(away_mod, "run_cycle", fake_run_cycle)
    monkeypatch.setattr(away_mod.time, "sleep", lambda _s: None)
    away_mod.away(Settings(home=tmp_path), interval_seconds=60, max_cycles=3)
    assert cycles == [1, 2, 3], "an empty cycle must not end the loop"


def test_away_continues_after_a_failing_cycle(monkeypatch, tmp_path):
    """A browser death or dead board must not end an unattended run."""
    from autoapply import away as away_mod
    from autoapply.config import Settings

    seen = []

    def fake_run_cycle(settings, *, cycle, batch, min_score, auto_submit):
        seen.append(cycle)
        return away_mod.CycleResult(cycle=cycle, error="apply: browser closed")

    monkeypatch.setattr(away_mod, "run_cycle", fake_run_cycle)
    monkeypatch.setattr(away_mod.time, "sleep", lambda _s: None)
    away_mod.away(Settings(home=tmp_path), interval_seconds=60, max_cycles=2)
    assert seen == [1, 2]


def test_cycle_summary_line_is_one_line():
    from autoapply.away import CycleResult

    line = CycleResult(cycle=3, new_jobs=52, queued=12, applied=9,
                       submitted=2, needs_input=5, manual=2).line("15:17")
    assert "\n" not in line
    assert "cycle 3" in line and "2 submitted" in line and "next wake 15:17" in line


def test_stale_profile_lock_is_released(tmp_path, monkeypatch):
    """An orphaned Chrome held the profile and every away cycle failed to launch."""
    import os

    from autoapply.browser import release_profile_lock

    profile = tmp_path / "browser_data"
    profile.mkdir()
    (profile / "SingletonLock").symlink_to(f"some-host-{os.getpid()}")
    (profile / "SingletonCookie").symlink_to("123")

    killed = []
    monkeypatch.setattr("autoapply.browser.os.kill", lambda pid, sig: killed.append(pid))
    monkeypatch.setattr("autoapply.browser.time.sleep", lambda _s: None)

    class _Proc:
        stdout = f"Chrome --user-data-dir={profile} --foo"

    monkeypatch.setattr("subprocess.run", lambda *a, **k: _Proc())

    assert release_profile_lock(profile) is True
    assert killed == [os.getpid()], "must terminate the process holding this profile"
    assert not (profile / "SingletonLock").is_symlink()
    assert not (profile / "SingletonCookie").is_symlink()


def test_lock_release_ignores_a_process_using_another_profile(tmp_path, monkeypatch):
    """Never kill the user's own Chrome — only a process on THIS profile dir."""

    from autoapply.browser import release_profile_lock

    profile = tmp_path / "browser_data"
    profile.mkdir()
    (profile / "SingletonLock").symlink_to("some-host-424242")

    killed = []
    monkeypatch.setattr("autoapply.browser.os.kill", lambda pid, sig: killed.append(pid))
    monkeypatch.setattr("autoapply.browser.time.sleep", lambda _s: None)

    class _Proc:
        stdout = "Google Chrome --user-data-dir=/Users/someone/Library/Chrome"

    monkeypatch.setattr("subprocess.run", lambda *a, **k: _Proc())
    release_profile_lock(profile)
    assert killed == [], "must not kill a process using a different profile"


def test_failed_cycle_retries_sooner_than_the_full_interval(monkeypatch, tmp_path):
    """A locked browser cost 45 idle minutes; a no-op failure must retry fast."""
    from autoapply import away as away_mod
    from autoapply.config import Settings

    slept = []
    monkeypatch.setattr(away_mod.time, "sleep", slept.append)
    monkeypatch.setattr(
        away_mod, "run_cycle",
        lambda s, *, cycle, batch, min_score, auto_submit: away_mod.CycleResult(
            cycle=cycle, error="apply: existing browser session"
        ),
    )
    away_mod.away(Settings(home=tmp_path), interval_seconds=2700, max_cycles=2)
    assert slept and slept[0] <= 120, slept


def test_away_requeues_stalled_applications_when_nothing_untried_remains(conn, feed_listings):
    """770 needs_input jobs on fillable ATSs were the only submission pool left.

    They are filled but for a field or two, and the mapper keeps gaining
    answers, so a retry often completes them.
    """
    from autoapply import away as away_mod
    from autoapply.config import Settings

    now = datetime.now(UTC).isoformat()
    db.upsert_job(
        conn, id="gh1", company_name="C", title="Software Engineer I",
        url="https://boards.greenhouse.io/x", now_iso=now, ats="greenhouse", score=80,
    )
    db.record_application(conn, job_id="gh1", status="needs_input")
    conn.commit()

    added = away_mod._top_up_queue(conn, Settings(), batch=10, min_score=0)
    assert added == 1
    assert conn.execute(
        "SELECT status FROM applications WHERE job_id='gh1'"
    ).fetchone()["status"] == "queued"


def test_away_never_queues_generic_dead_ends(conn):
    """generic: 1,359 attempts, zero submissions — never spend a slot on one."""
    from autoapply import away as away_mod
    from autoapply.config import Settings

    now = datetime.now(UTC).isoformat()
    db.upsert_job(
        conn, id="gen1", company_name="BigCo", title="Software Engineer I",
        url="https://careers.bigco.com/x", now_iso=now, ats="generic", score=100,
    )
    conn.commit()
    away_mod._top_up_queue(conn, Settings(), batch=10, min_score=0)
    row = conn.execute("SELECT status FROM applications WHERE job_id='gen1'").fetchone()
    assert row is None, "a generic posting must never be queued"


def test_away_never_reapplies_to_a_submitted_company_title(conn):
    """Boards repost a req under a new id; a second application reads as spam."""
    from autoapply import away as away_mod
    from autoapply.config import Settings

    now = datetime.now(UTC).isoformat()
    for jid in ("rep1", "rep2"):
        db.upsert_job(
            conn, id=jid, company_name="Voyager", title="ML Engineer - Associate",
            url=f"https://boards.greenhouse.io/v/{jid}", now_iso=now, ats="greenhouse", score=80,
        )
    db.record_application(conn, job_id="rep1", status="submitted")
    conn.execute("UPDATE applications SET submitted_at=? WHERE job_id='rep1'", (now,))
    conn.commit()
    away_mod._top_up_queue(conn, Settings(), batch=10, min_score=0)
    assert conn.execute("SELECT status FROM applications WHERE job_id='rep2'").fetchone() is None
