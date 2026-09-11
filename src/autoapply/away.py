"""Autonomous away mode: discover -> queue -> apply, on a loop, until killed.

The single-pass shell script exits the moment the queue drains, so a run left
alone stops minutes after it starts and the machine sits idle. This loop keeps
going: an empty cycle is a normal outcome, not a reason to exit.

Each cycle prints one summary line so a long unattended run stays readable:

    cycle 3 | 14:32 | new 52 | queued 12 | applied 9 (2 submitted, 5 need you,
    2 manual) | next wake 15:17

Never raises out of a cycle — discovery outages, browser deaths, and bad
postings are all recorded and the loop continues to the next cycle.
"""

from __future__ import annotations

import datetime as dt
import sqlite3
import time
from dataclasses import dataclass

from rich.console import Console

from autoapply import db
from autoapply.config import Settings

console = Console()

#: A failed, no-op cycle retries on this shorter delay instead of the full
#: interval — a transient browser-profile lock cost 45 idle minutes.
_ERROR_RETRY_SECONDS = 120.0

#: Postings newer than this are worked before older ones.
_FRESH_DAYS = 14


@dataclass(slots=True)
class CycleResult:
    """What one discover->queue->apply pass accomplished."""

    cycle: int
    new_jobs: int = 0
    queued: int = 0
    applied: int = 0
    submitted: int = 0
    needs_input: int = 0
    manual: int = 0
    error: str = ""

    def line(self, next_wake: str) -> str:
        if self.error:
            return f"cycle {self.cycle} | error: {self.error[:80]} | next wake {next_wake}"
        return (
            f"cycle {self.cycle} | new {self.new_jobs} | queued {self.queued} | "
            f"applied {self.applied} ({self.submitted} submitted, "
            f"{self.needs_input} need you, {self.manual} manual) | "
            f"next wake {next_wake}"
        )


def _submitted_count(conn: sqlite3.Connection) -> int:
    return conn.execute(
        "SELECT COUNT(*) n FROM applications WHERE submitted_at IS NOT NULL"
    ).fetchone()["n"]


def _status_counts(conn: sqlite3.Connection) -> dict[str, int]:
    return {
        r["status"]: r["n"]
        for r in conn.execute("SELECT status, COUNT(*) n FROM applications GROUP BY status")
    }


def run_cycle(
    settings: Settings,
    *,
    cycle: int,
    batch: int,
    min_score: int,
    auto_submit: bool,
) -> CycleResult:
    """One discover -> queue -> apply pass. Never raises."""
    from autoapply import discovery, runner

    res = CycleResult(cycle=cycle)
    conn = db.connect(settings.db_path)
    try:
        before_status = _status_counts(conn)
        before_submitted = _submitted_count(conn)

        # 1. Discovery. Its own min-interval guard makes a too-soon call a no-op,
        # so this is cheap to attempt every cycle.
        try:
            for r in discovery.discover(settings):
                res.new_jobs += r.new
        except Exception as e:  # noqa: BLE001 - a dead board must not end the loop
            res.error = f"discovery: {e}"

        # 2. Top the queue up with fillable, untried postings.
        res.queued = _top_up_queue(conn, settings, batch=batch, min_score=min_score)
        conn.commit()
    finally:
        conn.close()

    # 3. Apply. run_queue owns its own connection and browser lifecycle.
    try:
        runner.run_queue(
            settings,
            dry_run=False,
            auto_submit=auto_submit,
            max_per_run=batch,
            unattended=True,
        )
    except Exception as e:  # noqa: BLE001 - browser death must not end the loop
        res.error = f"apply: {e}"

    conn = db.connect(settings.db_path)
    try:
        after_status = _status_counts(conn)
        res.submitted = _submitted_count(conn) - before_submitted
        for key, attr in (("needs_input", "needs_input"), ("manual", "manual")):
            setattr(res, attr, after_status.get(key, 0) - before_status.get(key, 0))
        res.applied = res.submitted + res.needs_input + res.manual
    finally:
        conn.close()
    return res


def _top_up_queue(
    conn: sqlite3.Connection, settings: Settings, *, batch: int, min_score: int
) -> int:
    """Queue untried, fillable postings — fillable ATSs first. Returns count added.

    Mirrors `queue add --top N` but inline, so a cycle never shells out.
    """
    from autoapply.runner import enqueue

    # Only ATSs that have ever produced a submission. 'generic' postings are
    # careers pages with no inline form: 1,359 attempts, 1,359 "no form found",
    # zero submissions — queueing them spends the batch on guaranteed dead ends.
    fillable = ("greenhouse", "lever", "ashby")
    # Fresh postings first: a req posted this week is still accepting; one
    # from two months ago is often filled but not yet delisted.
    fresh_cutoff = time.time() - _FRESH_DAYS * 86400
    rows = conn.execute(
        """SELECT j.id FROM jobs j
           LEFT JOIN applications a ON a.job_id = j.id
           WHERE j.active = 1 AND j.is_visible = 1 AND j.score >= ?
             AND a.job_id IS NULL
             AND j.ats IN (?, ?, ?)
             -- Boards repost the same req under a new id. A second application
             -- to the same company + title reads as spam, not persistence.
             AND NOT EXISTS (
               SELECT 1 FROM applications s JOIN jobs sj ON sj.id = s.job_id
               WHERE s.submitted_at IS NOT NULL
                 AND sj.company_name = j.company_name
                 AND lower(trim(sj.title)) = lower(trim(j.title))
             )
           ORDER BY (COALESCE(j.date_posted, 0) >= ?) DESC,
                    CASE j.ats WHEN 'greenhouse' THEN 0 WHEN 'lever' THEN 1 ELSE 2 END,
                    j.date_posted DESC, j.score DESC
           LIMIT ?""",
        (min_score, *fillable, fresh_cutoff, batch),
    ).fetchall()
    added = enqueue(conn, [r["id"] for r in rows])
    if added >= batch:
        return added

    # Nothing untried left. Re-work applications that stalled on a missing field:
    # they are already filled but for a question or two, and the mapper keeps
    # gaining answers (pronouns, English proficiency, consent boxes, the option
    # and checkbox fixes), so a retry now often completes and submits.
    return added + _requeue_stalled(conn, limit=batch - added)


def _requeue_stalled(conn: sqlite3.Connection, *, limit: int) -> int:
    """Return needs_input/failed fillable applications to the queue. Returns count."""
    if limit <= 0:
        return 0
    rows = conn.execute(
        """SELECT a.job_id FROM applications a JOIN jobs j ON j.id = a.job_id
           WHERE a.status IN ('needs_input', 'failed')
             AND j.active = 1 AND j.ats IN ('greenhouse', 'lever', 'ashby')
           ORDER BY j.score DESC
           LIMIT ?""",
        (limit,),
    ).fetchall()
    for r in rows:
        conn.execute(
            "UPDATE applications SET status = 'queued' WHERE job_id = ?", (r["job_id"],)
        )
    return len(rows)


def away(
    settings: Settings,
    *,
    interval_seconds: float = 2700.0,
    batch: int = 40,
    min_score: int = 0,
    auto_submit: bool = True,
    max_cycles: int | None = None,
) -> None:
    """Loop until killed. An empty cycle sleeps and tries again, never exits."""
    cycle = 0
    started = dt.datetime.now()
    console.print(
        f"[bold]away mode[/] — batch {batch}, min-score {min_score}, "
        f"interval {interval_seconds / 60:.0f}m. Ctrl-C or `pkill -f 'autoapply away'` to stop."
    )
    while max_cycles is None or cycle < max_cycles:
        cycle += 1
        result = run_cycle(
            settings, cycle=cycle, batch=batch, min_score=min_score, auto_submit=auto_submit
        )
        # A cycle that errored without applying anything (a locked browser
        # profile, a dead board) must not cost a whole interval of idleness.
        delay = interval_seconds
        if result.error and result.applied == 0:
            delay = min(_ERROR_RETRY_SECONDS, interval_seconds)
        wake = dt.datetime.now() + dt.timedelta(seconds=delay)
        console.print(f"[bold cyan]{result.line(wake.strftime('%H:%M'))}[/]")
        if max_cycles is not None and cycle >= max_cycles:
            break
        try:
            time.sleep(delay)
        except KeyboardInterrupt:
            break
    elapsed = dt.datetime.now() - started
    console.print(f"[dim]away mode stopped after {cycle} cycle(s), {elapsed}[/]")
