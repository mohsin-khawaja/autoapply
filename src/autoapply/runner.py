"""Milestone 2 pipeline: queue -> fill -> review pause -> record (SPEC.md §9).

Safety (SPEC §1, §10): the pipeline never clicks Submit. ``BaseAdapter.fill``
is contractually forbidden from submitting; after the review pause the human
submits in the headed browser. ``adapter.submit`` runs only when
``--auto-submit`` is set AND the ATS is on ``Settings.auto_submit_allowlist``
(empty by default).
"""

from __future__ import annotations

import json
import random
import signal
import sqlite3
import threading
import time
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from rich.console import Console
from rich.table import Table

from autoapply import db, llm
from autoapply.ats import base
from autoapply.browser import launch_context, looks_blocked
from autoapply.config import Settings
from autoapply.filling import llm_answers, mapper
from autoapply.profile import Profile, load_profile
from autoapply.sources.simplify import classify_ats

if TYPE_CHECKING:  # pragma: no cover
    from playwright.sync_api import Page

console = Console()


@dataclass(slots=True)
class QueuedJob:
    """A queued application joined with its job row."""

    job_id: str
    company_name: str
    title: str
    url: str
    ats: str | None

    @property
    def id(self) -> str:
        """Alias so QueuedJob satisfies :class:`mapper.JobContext`."""
        return self.job_id


def queued_jobs(conn: sqlite3.Connection, limit: int) -> list[QueuedJob]:
    """Queued applications, highest-scored job first."""
    rows = conn.execute(
        """SELECT a.job_id, j.company_name, j.title,
                  COALESCE(j.final_url, j.url) AS url, j.ats
           FROM applications a JOIN jobs j ON j.id = a.job_id
           WHERE a.status = 'queued'
           -- Work the ATSs that can actually complete an application first.
           -- A high-scoring 'generic' posting is usually a careers page with no
           -- inline form, so scoring alone sends the batch to dead ends.
           ORDER BY CASE j.ats
                      WHEN 'greenhouse' THEN 0
                      WHEN 'lever'      THEN 1
                      WHEN 'ashby'      THEN 2
                      ELSE 3
                    END,
                    j.score DESC, j.date_posted DESC LIMIT ?""",
        (limit,),
    ).fetchall()
    return [QueuedJob(r["job_id"], r["company_name"], r["title"], r["url"], r["ats"]) for r in rows]


def enqueue(conn: sqlite3.Connection, job_ids: list[str]) -> int:
    """Insert queued application rows; skips jobs already tracked. Returns count added."""
    added = 0
    with db.transaction(conn):
        for jid in job_ids:
            cur = conn.execute(
                "INSERT OR IGNORE INTO applications (job_id, status) VALUES (?, 'queued')",
                (jid,),
            )
            added += cur.rowcount
    return added


def _fill_llm_answers(
    conn: sqlite3.Connection,
    plan: base.FillPlan,
    profile: Profile,
    job: QueuedJob,
    client: llm.ChatClient,
) -> None:
    """Resolve ``source=="llm"`` fields via the answer cache / configured LLM.

    Two shapes: a constrained field (has options) asks the model to pick the
    best visible option; a free-text field asks it to write an answer. Both are
    best-effort — if the provider is down, or returns no valid option, the
    field simply stays needs_input rather than being filled with a guess.
    """
    for fp in plan.fields:
        if fp.source != "llm" or fp.value is not None:
            continue
        question = fp.field.label or fp.field.key
        try:
            if fp.field.options:  # constrained: pick one of the visible options
                answer = llm_answers.choose_option(
                    question, fp.field.options, profile, job, client
                )
                if answer is None:  # model declined / no valid match — leave flagged
                    fp.note = "llm could not pick a safe option — needs you"
                    continue
            else:  # free-text: generate and cache
                answer = llm_answers.get_or_generate(
                    conn, question, job.company_name, profile=profile, job=job, client=client
                )
        except Exception as e:  # noqa: BLE001 - degrade to manual, never abort the run
            fp.note = f"llm unavailable: {e}"
            continue
        fp.value = answer
        fp.source = "answer_cache"
        fp.needs_input = False
        fp.confidence = 1.0
        fp.note = "llm estimated (review before submit)"


def print_plan(plan: base.FillPlan) -> None:
    """Terminal diff of what will be (or was) entered."""
    table = Table(title=f"fill plan — {plan.job_id[:8]} ({plan.ats.value})")
    table.add_column("field")
    table.add_column("value")
    table.add_column("source", style="dim")
    table.add_column("!", style="bold red")
    for fp in plan.fields:
        val = str(fp.value) if fp.value is not None else "—"
        table.add_row(
            fp.field.label or fp.field.key,
            val if len(val) <= 60 else val[:59] + "…",
            f"{fp.source} {fp.confidence:.0%}",
            "needs input" if fp.needs_input else "",
        )
    console.print(table)


def process_one(
    page: Page,
    job: QueuedJob,
    *,
    conn: sqlite3.Connection,
    profile: Profile,
    settings: Settings,
    dry_run: bool,
    auto_submit: bool,
    unattended: bool = False,
) -> str:
    """Run the pipeline for one queued job. Returns the recorded status.

    ``unattended`` skips the interactive review pause: the outcome is recorded
    and the caller moves to the next job, so a batch never blocks on input.
    The submit gate is unchanged — only the allowlist + fully-resolved check
    can submit.
    """
    now = lambda: datetime.now(UTC).isoformat()  # noqa: E731
    def mark_manual(note: str) -> str:
        if not dry_run:  # dry-run must not mutate application state
            db.record_application(conn, job_id=job.job_id, status="manual", notes=note)
        else:
            console.print(f"[yellow]would mark manual:[/] {note}")
        return "manual" if not dry_run else "queued"

    # Portals that require an account before any form exists. The generic
    # adapter would load the page, find nothing, and mark it manual anyway —
    # so skip straight to manual and save the round trip.
    ats = job.ats or classify_ats(job.url).value  # ats can be NULL on older rows
    if ats == "yc":
        return mark_manual("YC — sign in at Work at a Startup and message the founder")
    if ats in _ACCOUNT_WALLED:
        return mark_manual(f"{ats} needs an account — apply manually")

    adapter_cls = base.resolve_adapter(job.url)
    if adapter_cls is None:  # generic detects any http(s); None => unusable URL
        return mark_manual("no adapter")
    adapter = adapter_cls()

    page.goto(job.url, wait_until="domcontentloaded")
    try:  # SPA ATSs (Ashby) render the form after load; wait for the network to settle
        page.wait_for_load_state("networkidle", timeout=10_000)
    except Exception:  # noqa: BLE001 - busy pages never go idle; proceed anyway
        pass
    if looks_blocked(page):
        console.print("[yellow]blocked page — manual tier; finish it in the browser.[/]")
        return mark_manual("bot wall/captcha")

    fields = adapter.extract_form(page)
    if not fields:  # one retry after a settle — React forms mount late
        page.wait_for_timeout(3_000)
        fields = adapter.extract_form(page)
    if not fields:
        return mark_manual("no form found")

    class _Cache:
        def get(self, question_hash: str, company: str) -> str | None:
            return db.get_answer(conn, question_hash, company)

    resume = profile.documents.resume_path(settings.repo_root)
    plan = mapper.build_plan(
        fields,
        profile,
        _Cache(),
        job,  # QueuedJob satisfies JobContext (id via job_id alias below)
        job_url=job.url,
        ats=adapter.kind,
        resume_path=resume if resume and resume.exists() else None,
        fuzzy_threshold=settings.fuzzy_threshold,
    )

    # Keep any single generation well inside the per-job cap — one long essay
    # prompt must not spend the whole budget on a single field.
    llm_timeout = max(20.0, settings.per_job_seconds / 3) if settings.per_job_seconds else 120.0
    client = llm.make_client(settings, timeout=llm_timeout)
    # Run on whatever model this machine actually has pulled, so answer
    # generation works offline without matching config exactly.
    resolved = client.resolve_model(fallback=settings.ollama_fallback_model)
    if resolved and resolved != client.model:
        console.print(f"[dim]ollama: using {resolved}[/]")
        client.model = resolved
    _fill_llm_answers(conn, plan, profile, job, client)

    if dry_run:
        print_plan(plan)
        return "queued"  # untouched

    # A stuck widget should cost seconds, not Playwright's 30s default: with a
    # 100-job batch those timeouts dominate the run. Per-field isolation in the
    # adapters turns the fast failure into a flag rather than a lost job.
    page.set_default_timeout(8_000)
    try:
        result = adapter.fill(page, plan)
    finally:
        page.set_default_timeout(30_000)
    if result.status == "failed":
        # A fill that blew up still produced a part-filled form worth finishing
        # by hand, so it belongs in the manual queue rather than a dead 'failed'
        # bucket the dashboard's Finish-manually card never surfaces.
        db.record_application(
            conn, job_id=job.job_id, status="needs_input",
            notes=json.dumps([f"fill error: {(result.error or 'unknown')[:80]}"]),
        )
        return "needs_input"

    run_dir = settings.runs_dir / datetime.now(UTC).strftime("%Y%m%d")
    run_dir.mkdir(parents=True, exist_ok=True)
    shot = adapter.review_pause(page, plan, run_dir)
    print_plan(plan)

    if auto_submit and adapter.kind in settings.auto_submit_allowlist and not plan.unresolved:
        after = run_dir / f"{plan.job_id[:12]}-submitted.png"
        sub = adapter.submit(page, after)
        status = sub.status  # submitted | needs_input (bounced) | filled | failed
        proof = str(after) if after.exists() else str(shot)
        if status == "submitted":
            sent = conn.execute(
                "SELECT COUNT(*) FROM applications WHERE status = 'submitted'"
            ).fetchone()[0] + 1
            console.print(
                f"[bold green]✓ SUBMITTED[/] {job.company_name} — {job.title[:48]} "
                f"[dim](#{sent} total)[/]"
            )
        db.record_application(
            conn, job_id=job.job_id, status=status, filled_at=now(),
            submitted_at=now() if status == "submitted" else None,
            screenshot=proof, notes=sub.notes,
        )
        return status

    # Review pause: the human inspects the headed browser and clicks Submit
    # themselves (SPEC §1 — the tool never submits outside the allowlist path).
    # Unattended batches record the outcome instead of waiting, so a form that
    # needs a human lands on the dashboard and the run continues.
    if unattended:
        status = "needs_input" if plan.unresolved else "filled"
        if plan.unresolved:
            console.print(
                f"[yellow]needs you:[/] {len(plan.unresolved)} field(s) — "
                "queued on the dashboard, moving on"
            )
    else:
        answer = console.input(
            "[bold]review the browser[/] — type [green]s[/] if you submitted, "
            "[yellow]k[/] to skip, Enter to record as-is: "
        ).strip().lower()
        if answer == "s":
            status = "submitted"
        elif answer == "k":
            status = "skipped"
        else:
            status = "needs_input" if plan.unresolved else "filled"
    db.record_application(
        conn, job_id=job.job_id, status=status, filled_at=now(),
        submitted_at=now() if status == "submitted" else None,
        screenshot=str(shot),
        notes=json.dumps([fp.field.label for fp in plan.unresolved]) if plan.unresolved else None,
    )
    return status


#: ATS families that gate the application behind a login, so there is nothing
#: to fill on the public page.
_ACCOUNT_WALLED = frozenset(
    {"workday", "icims", "smartrecruiters", "rippling", "yc"}
)


class JobTimeout(Exception):
    """A single application exceeded ``settings.per_job_seconds``."""


@contextmanager
def job_deadline(seconds: float):
    """Raise :class:`JobTimeout` in the main thread after ``seconds``.

    Playwright's sync API blocks on a socket, so SIGALRM is what actually
    interrupts a hung widget — a thread-based timer could only watch. Disabled
    when ``seconds`` <= 0 or when off the main thread (tests, workers), where
    signal handlers cannot be installed.
    """
    usable = seconds > 0 and threading.current_thread() is threading.main_thread()
    if not usable:
        yield
        return

    def _fire(signum, frame):  # noqa: ANN001, ARG001
        raise JobTimeout(f"exceeded {seconds:.0f}s cap")

    prev = signal.signal(signal.SIGALRM, _fire)
    signal.setitimer(signal.ITIMER_REAL, seconds)
    try:
        yield
    finally:
        signal.setitimer(signal.ITIMER_REAL, 0)
        signal.signal(signal.SIGALRM, prev)


def _browser_is_dead(exc: BaseException) -> bool:
    """True when Playwright reports the browser/context/page is gone.

    Once that happens every remaining job fails instantly, so the batch must
    relaunch rather than grind through the queue sleeping between corpses.
    """
    msg = str(exc)
    return (
        "browser has been closed" in msg
        or "Target page, context or browser has been closed" in msg
        or "Target closed" in msg
    )


def _recover_page(page: Page) -> bool:
    """Return the page to a clean state after a timeout. False => relaunch needed.

    An interrupted Playwright call can leave the page mid-navigation, so prove
    it still responds by parking it on a blank page before the next job.
    """
    try:
        page.goto("about:blank", wait_until="domcontentloaded", timeout=10_000)
    except Exception:  # noqa: BLE001 - unresponsive page; caller relaunches the browser
        return False
    return True


def run_queue(
    settings: Settings,
    *,
    dry_run: bool = False,
    auto_submit: bool = False,
    max_per_run: int | None = None,
    unattended: bool = False,
) -> None:
    """Process the queue in a headed persistent browser with jittered pacing."""
    conn = db.connect(settings.db_path)
    profile = load_profile(settings.profile_path)
    # `or` would treat an explicit 0 as "unset" and fall back to 15 — a request
    # to apply to nothing must apply to nothing.
    limit = settings.max_per_run if max_per_run is None else max_per_run
    jobs = queued_jobs(conn, limit)
    if not jobs:
        console.print("[yellow]queue empty[/] — `autoapply queue add --top 10` first.")
        conn.close()
        return

    tally: dict[str, int] = {}
    pending = list(jobs)
    restarts = 0
    while pending:
        crashed = False
        with launch_context(settings.browser_data_dir, headed=True) as (_, page):
            while pending:
                job = pending[0]
                done = len(jobs) - len(pending) + 1
                # Re-read status: `autoapply skip` may have retired this job
                # after the batch list was built, and an unattended run has no
                # other way to hear about it.
                current = conn.execute(
                    "SELECT status FROM applications WHERE job_id = ?", (job.job_id,)
                ).fetchone()
                if current is not None and current["status"] != "queued":
                    console.print(
                        f"[dim]{done}/{len(jobs)} {job.company_name} — skipped "
                        f"({current['status']})[/]"
                    )
                    pending.pop(0)
                    continue
                console.print(
                    f"\n[bold]{done}/{len(jobs)}[/] {job.company_name} — {job.title} "
                    f"[dim]({job.ats or '?'})[/]"
                )
                try:
                    with job_deadline(settings.per_job_seconds):
                        status = process_one(
                            page, job, conn=conn, profile=profile, settings=settings,
                            dry_run=dry_run, auto_submit=auto_submit, unattended=unattended,
                        )
                except JobTimeout as e:
                    # Distribution beats perfection: cache the half-worked form to
                    # the dashboard and move on. Nothing was submitted — the gate
                    # never ran — so it is safe to finish this one by hand later.
                    db.record_application(
                        conn, job_id=job.job_id, status="needs_input", notes=f"timed out: {e}"
                    )
                    console.print(f"[yellow]timed out after {settings.per_job_seconds:.0f}s[/]")
                    status = "needs_input"
                    if not _recover_page(page):
                        crashed = True
                        pending.pop(0)  # already recorded; don't retry it after relaunch
                        conn.commit()
                        tally[status] = tally.get(status, 0) + 1
                        break
                except Exception as e:  # noqa: BLE001 - one bad posting must not kill the run
                    if _browser_is_dead(e):
                        # Leave this job queued and rebuild the browser around it.
                        crashed = True
                        break
                    db.record_application(
                        conn, job_id=job.job_id, status="failed", notes=str(e)
                    )
                    console.print(f"[red]failed:[/] {e}")
                    status = "failed"
                conn.commit()  # record_application runs outside db.transaction
                console.print(f"[dim]recorded:[/] {status}")
                tally[status] = tally.get(status, 0) + 1
                pending.pop(0)
                # Pace only jobs where a form was actually touched. "manual"
                # and "skipped" sent nothing, so the anti-blast delay would
                # just burn hours on postings the tool cannot act on.
                if not dry_run and pending and status not in ("manual", "skipped"):
                    delay = random.uniform(settings.rate_min_seconds, settings.rate_max_seconds)
                    console.print(f"[dim]rate limit — sleeping {delay:.0f}s[/]")
                    time.sleep(delay)
        if not crashed:
            break
        restarts += 1
        if restarts > 5:
            console.print("[red]browser keeps dying — stopping so the queue is preserved.[/]")
            break
        console.print(
            f"[yellow]browser closed — relaunching "
            f"(restart {restarts}, {len(pending)} job(s) left)[/]"
        )
        time.sleep(5)
    conn.close()
    if tally:
        console.print(
            "\n[bold]run summary:[/] "
            + "  ".join(f"{k} {v}" for k, v in sorted(tally.items()))
        )
        if any(k in tally for k in ("needs_input", "manual", "failed")):
            console.print(
                "[dim]anything needing you is on the dashboard: "
                "uv run autoapply dashboard[/]"
            )
