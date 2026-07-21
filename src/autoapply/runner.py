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
import sqlite3
import time
from dataclasses import dataclass
from datetime import UTC, datetime
from typing import TYPE_CHECKING

from rich.console import Console
from rich.table import Table

from autoapply import db
from autoapply.ats import base
from autoapply.browser import launch_context, looks_blocked
from autoapply.config import Settings
from autoapply.filling import llm_answers, mapper
from autoapply.ollama import OllamaClient
from autoapply.profile import Profile, load_profile

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
           ORDER BY j.score DESC, j.date_posted DESC LIMIT ?""",
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
    client: OllamaClient,
) -> None:
    """Resolve ``source=="llm"`` free-text fields via the answer cache / Ollama.

    Best-effort: if Ollama is down the field simply stays needs_input.
    """
    for fp in plan.fields:
        if fp.source != "llm" or fp.value is not None:
            continue
        question = fp.field.label or fp.field.key
        try:
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
        fp.note = "generated answer (review before submit)"


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
) -> str:
    """Run the pipeline for one queued job. Returns the recorded status."""
    now = lambda: datetime.now(UTC).isoformat()  # noqa: E731
    def mark_manual(note: str) -> str:
        if not dry_run:  # dry-run must not mutate application state
            db.record_application(conn, job_id=job.job_id, status="manual", notes=note)
        else:
            console.print(f"[yellow]would mark manual:[/] {note}")
        return "manual" if not dry_run else "queued"

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

    client = OllamaClient(host=settings.ollama_host, model=settings.ollama_model)
    _fill_llm_answers(conn, plan, profile, job, client)

    if dry_run:
        print_plan(plan)
        return "queued"  # untouched

    result = adapter.fill(page, plan)
    if result.status == "failed":
        db.record_application(
            conn, job_id=job.job_id, status="failed", notes=result.error or "fill failed"
        )
        return "failed"

    run_dir = settings.runs_dir / datetime.now(UTC).strftime("%Y%m%d")
    run_dir.mkdir(parents=True, exist_ok=True)
    shot = adapter.review_pause(page, plan, run_dir)
    print_plan(plan)

    if auto_submit and adapter.kind in settings.auto_submit_allowlist and not plan.unresolved:
        after = run_dir / f"{plan.job_id[:12]}-submitted.png"
        sub = adapter.submit(page, after)
        status = sub.status  # submitted | needs_input (bounced) | filled | failed
        proof = str(after) if after.exists() else str(shot)
        db.record_application(
            conn, job_id=job.job_id, status=status, filled_at=now(),
            submitted_at=now() if status == "submitted" else None,
            screenshot=proof, notes=sub.notes,
        )
        return status

    # Review pause: the human inspects the headed browser and clicks Submit
    # themselves (SPEC §1 — the tool never submits outside the allowlist path).
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


def run_queue(
    settings: Settings,
    *,
    dry_run: bool = False,
    auto_submit: bool = False,
    max_per_run: int | None = None,
) -> None:
    """Process the queue in a headed persistent browser with jittered pacing."""
    conn = db.connect(settings.db_path)
    profile = load_profile(settings.profile_path)
    jobs = queued_jobs(conn, max_per_run or settings.max_per_run)
    if not jobs:
        console.print("[yellow]queue empty[/] — `autoapply queue add --top 10` first.")
        conn.close()
        return

    with launch_context(settings.browser_data_dir, headed=True) as (_, page):
        for i, job in enumerate(jobs):
            console.print(
                f"\n[bold]{i + 1}/{len(jobs)}[/] {job.company_name} — {job.title} "
                f"[dim]({job.ats or '?'})[/]"
            )
            try:
                status = process_one(
                    page, job, conn=conn, profile=profile, settings=settings,
                    dry_run=dry_run, auto_submit=auto_submit,
                )
            except Exception as e:  # noqa: BLE001 - one bad posting must not kill the run
                db.record_application(conn, job_id=job.job_id, status="failed", notes=str(e))
                console.print(f"[red]failed:[/] {e}")
                status = "failed"
            conn.commit()  # record_application runs outside db.transaction
            console.print(f"[dim]recorded:[/] {status}")
            if not dry_run and i < len(jobs) - 1:
                delay = random.uniform(settings.rate_min_seconds, settings.rate_max_seconds)
                console.print(f"[dim]rate limit — sleeping {delay:.0f}s[/]")
                time.sleep(delay)
    conn.close()
