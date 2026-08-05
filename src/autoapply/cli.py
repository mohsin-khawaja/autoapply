"""autoapply CLI (Typer). Milestone 0: init/sync/list are real; the rest are
stubbed but present so the surface is stable for parallel workstreams (SPEC.md §7).
"""

from __future__ import annotations

import shutil

import typer
from rich.console import Console
from rich.table import Table

from autoapply import db
from autoapply.config import load_settings
from autoapply.ollama import OllamaClient
from autoapply.profile import load_profile
from autoapply.sources import simplify

app = typer.Typer(
    add_completion=False,
    help="Local-first job application agent (Ollama + Playwright + SQLite).",
    no_args_is_help=True,
)
queue_app = typer.Typer(help="Manage the application queue.")
answers_app = typer.Typer(help="Inspect/edit cached free-text answers.")
app.add_typer(queue_app, name="queue")
app.add_typer(answers_app, name="answers")

console = Console()

_WS_D = "[yellow]stub[/] — implemented by workstream D (feat/llm-answers)."


@app.command()
def init() -> None:
    """Create dirs + DB, seed check, and verify Ollama + Playwright."""
    settings = load_settings()
    settings.ensure_dirs()
    conn = db.connect(settings.db_path)
    conn.close()
    console.print(f"[green]✓[/] data dir: {settings.home}")
    console.print(f"[green]✓[/] database: {settings.db_path}")

    # profile.yaml
    profile = None
    try:
        profile = load_profile(settings.profile_path)
        console.print(
            f"[green]✓[/] profile: {profile.identity.first_name} "
            f"{profile.identity.last_name} <{profile.identity.email}>"
        )
    except Exception as e:  # noqa: BLE001 - surface any validation error to the user
        console.print(f"[red]✗[/] profile.yaml invalid: {e}")

    # resume present?
    resume = profile.documents.resume_path(settings.repo_root) if profile else None
    if resume and resume.exists():
        console.print(f"[green]✓[/] resume: {resume}")
    else:
        console.print(f"[yellow]![/] resume missing — drop it at {resume}")

    # Ollama
    oc = OllamaClient(host=settings.ollama_host, model=settings.ollama_model)
    ok, msg = oc.health()
    if not ok:
        resolved = oc.resolve_model(fallback=settings.ollama_fallback_model)
        if resolved:
            ok, msg = True, f"Ollama OK (using installed {resolved})"
    console.print(f"[{'green' if ok else 'yellow'}]{'✓' if ok else '!'}[/] {msg}")

    # Playwright chromium
    if _playwright_ready():
        console.print("[green]✓[/] Playwright Chromium installed")
    else:
        console.print(
            "[yellow]![/] Playwright browser missing — run "
            "`uv run playwright install chromium`"
        )
    console.print("\n[bold]next:[/] uv run autoapply sync")


@app.command()
def sync(
    resolve_redirects: bool = typer.Option(
        False, help="Follow simplify.jobs links to the final ATS URL (slower)."
    ),
) -> None:
    """Pull the SimplifyJobs feed, score, and upsert into SQLite."""
    settings = load_settings()
    settings.ensure_dirs()
    with console.status("fetching listings…"):
        result = simplify.sync(settings, resolve_redirects=resolve_redirects)
    console.print(
        f"[green]synced[/] {result.total} listings — "
        f"new {result.new}, changed {result.changed}, inactive {result.inactive}, "
        f"score≥70 {result.scored_ge_70}"
    )


@app.command()
def yc() -> None:
    """Pull new-grad-eligible YC startup roles into the job table.

    Filters on the board's own ``minExperience`` field, so only postings that
    say new grads are welcome are kept. YC applications go through a Work at a
    Startup login and are a message to the founder, so these land in the manual
    tier for you to send — the runner never auto-applies to them.
    """
    from autoapply.sources import ycombinator

    settings = load_settings()
    settings.ensure_dirs()
    with console.status("fetching YC jobs…"):
        result = ycombinator.sync(settings)
    console.print(
        f"[green]YC synced[/] {result.fetched} postings — "
        f"new-grad eligible {result.new_grad}, new {result.new}"
    )
    console.print("[dim]YC roles need a Work at a Startup login — see the manual tier.[/]")


@app.command()
def referrals(
    min_score: int = typer.Option(1, "--min-score", help="Skip postings below this fit."),
) -> None:
    """Pull postings from referral companies (Amazon, Odoo) and fit-score them."""
    from autoapply.sources import referral

    settings = load_settings()
    settings.ensure_dirs()
    with console.status("searching referral companies…"):
        result = referral.scout(settings, min_score=min_score)
    console.print(
        f"[green]found[/] {result.fetched} postings — "
        f"upserted {result.upserted}, score≥50 {result.scored_ge_50}"
    )
    for err in result.errors:
        console.print(f"[yellow]![/] {err}")


@app.command(name="list")
def list_jobs(
    min_score: int = typer.Option(0, "--min-score", help="Only show jobs at/above this score."),
    limit: int = typer.Option(40, help="Max rows."),
    all_jobs: bool = typer.Option(False, "--all", help="Include inactive/hidden jobs."),
) -> None:
    """Show scored jobs as a table."""
    settings = load_settings()
    conn = db.connect(settings.db_path)
    rows = db.list_jobs(conn, min_score=min_score, active_only=not all_jobs, limit=limit)
    conn.close()
    if not rows:
        console.print("[yellow]no jobs[/] — run `autoapply sync` first.")
        raise typer.Exit()

    table = Table(title=f"jobs (min_score={min_score})")
    table.add_column("score", justify="right", style="bold")
    table.add_column("company")
    table.add_column("title")
    table.add_column("ats")
    table.add_column("location")
    table.add_column("id", style="dim")
    for r in rows:
        table.add_row(
            str(r.score), r.company_name, r.title, r.ats or "?",
            ", ".join(r.locations[:2]) or "—", r.id[:8],
        )
    console.print(table)


@queue_app.command("add")
def queue_add(
    job_id: str = typer.Argument(None, help="Job id (or unique prefix) to enqueue."),
    top: int = typer.Option(None, "--top", help="Enqueue the top-N by score."),
    min_score: int = typer.Option(70, "--min-score", help="Score floor for --top."),
    fillable_only: bool = typer.Option(
        True,
        "--fillable-only/--all-ats",
        help="Skip login-walled ATSs (Workday/iCIMS/YC) that an away run cannot complete.",
    ),
) -> None:
    """Enqueue a job by id, or the top-N scored jobs."""
    from autoapply.runner import _ACCOUNT_WALLED, enqueue

    settings = load_settings()
    conn = db.connect(settings.db_path)
    if top is not None:
        # Over-fetch, then drop the login-walled rows so --top N really yields
        # N postings the runner can fill rather than N rows it will mark manual.
        pool = db.list_jobs(conn, min_score=min_score, limit=top * 20 if fillable_only else top)
        # Drop anything already attempted, otherwise those rows eat the N slots
        # and are only discarded later by the INSERT OR IGNORE — "--top 40"
        # would report 40 tracked and queue nothing.
        tracked = {r["job_id"] for r in conn.execute("SELECT job_id FROM applications")}
        pool = [j for j in pool if j.id not in tracked]
        if fillable_only:
            pool = [j for j in pool if (j.ats or "") not in _ACCOUNT_WALLED]
            # Spend the N slots on ATSs that can actually be completed. A
            # 'generic' posting is usually a careers page with no inline form,
            # so a high score there still ends the run in a dead end.
            rank = {"greenhouse": 0, "lever": 1, "ashby": 2}
            pool.sort(key=lambda j: (rank.get(j.ats or "", 3), -j.score))
        ids = [j.id for j in pool[:top]]
    elif job_id:
        rows = conn.execute(
            "SELECT id FROM jobs WHERE id LIKE ?", (job_id + "%",)
        ).fetchall()
        if len(rows) != 1:
            console.print(f"[red]{len(rows)} jobs match {job_id!r}[/] — need a unique id/prefix.")
            conn.close()
            raise typer.Exit(1)
        ids = [rows[0]["id"]]
    else:
        console.print("[red]give a job id or --top N[/]")
        conn.close()
        raise typer.Exit(1)
    added = enqueue(conn, ids)
    conn.close()
    console.print(f"[green]queued[/] {added} job(s) ({len(ids) - added} already tracked)")


@queue_app.command("list")
def queue_list() -> None:
    """Show the queue."""
    settings = load_settings()
    conn = db.connect(settings.db_path)
    rows = conn.execute(
        """SELECT a.job_id, j.company_name, j.title, j.score, j.ats
           FROM applications a JOIN jobs j ON j.id = a.job_id
           WHERE a.status = 'queued' ORDER BY j.score DESC"""
    ).fetchall()
    conn.close()
    if not rows:
        console.print("[yellow]queue empty[/]")
        raise typer.Exit()
    table = Table(title="queue")
    table.add_column("score", justify="right", style="bold")
    table.add_column("company")
    table.add_column("title")
    table.add_column("ats")
    table.add_column("id", style="dim")
    for r in rows:
        table.add_row(
            str(r["score"]), r["company_name"], r["title"], r["ats"] or "?", r["job_id"][:8]
        )
    console.print(table)


@queue_app.command("remove")
def queue_remove(job_id: str = typer.Argument(..., help="Job id (or prefix) to dequeue.")) -> None:
    """Remove a queued job (only rows still in 'queued')."""
    settings = load_settings()
    conn = db.connect(settings.db_path)
    with db.transaction(conn):
        cur = conn.execute(
            "DELETE FROM applications WHERE status = 'queued' AND job_id LIKE ?",
            (job_id + "%",),
        )
    conn.close()
    console.print(f"[green]removed[/] {cur.rowcount} queued row(s)")


@app.command()
def run(
    dry_run: bool = typer.Option(False, "--dry-run", help="Plan field mappings; fill nothing."),
    auto_submit: bool = typer.Option(False, "--auto-submit", help="Allowlisted ATSs only."),
    max_per_run: int = typer.Option(15, "--max-per-run", help="Cap applications per run."),
    unattended: bool = typer.Option(
        False, "--unattended",
        help="Never wait for input: skip anything needing a human, record it, keep going.",
    ),
) -> None:
    """Process the queue: fill → review pause → submit (human clicks Submit)."""
    from autoapply.runner import run_queue

    settings = load_settings()
    settings.ensure_dirs()
    run_queue(
        settings, dry_run=dry_run, auto_submit=auto_submit,
        max_per_run=max_per_run, unattended=unattended,
    )


@app.command()
def open(job_id: str = typer.Argument(..., help="Manual-tier job id (or prefix).")) -> None:
    """Open a manual-tier posting in the persistent browser."""
    from autoapply.browser import launch_context

    settings = load_settings()
    conn = db.connect(settings.db_path)
    row = conn.execute(
        "SELECT COALESCE(final_url, url) u FROM jobs WHERE id LIKE ?", (job_id + "%",)
    ).fetchone()
    conn.close()
    if row is None:
        console.print(f"[red]no job matching {job_id!r}[/]")
        raise typer.Exit(1)
    console.print(f"opening {row['u']} — close the browser window when done.")
    with launch_context(settings.browser_data_dir, headed=True) as (_, page):
        page.goto(row["u"])
        console.input("[dim]Enter to close…[/] ")


@app.command()
def skip(
    pattern: str = typer.Argument(
        ..., help="Company or title text to skip, e.g. 'palantir' or 'defense'."
    ),
    undo: bool = typer.Option(False, "--undo", help="Put skipped matches back in the queue."),
) -> None:
    """Skip queued applications matching PATTERN — takes effect on a live run.

    Safe to use while a background batch is going: the runner re-checks each
    job's status just before it starts, so a skip applies immediately without
    restarting the run. Already-submitted applications are never touched.
    """
    settings = load_settings()
    conn = db.connect(settings.db_path)
    like = f"%{pattern.lower()}%"
    if undo:
        with db.transaction(conn):
            cur = conn.execute(
                """UPDATE applications SET status='queued', notes=NULL
                   WHERE status='skipped' AND job_id IN (
                     SELECT id FROM jobs
                     WHERE lower(company_name) LIKE ? OR lower(title) LIKE ?)""",
                (like, like),
            )
        console.print(f"[green]re-queued[/] {cur.rowcount} application(s) matching {pattern!r}")
        conn.close()
        raise typer.Exit()

    rows = conn.execute(
        """SELECT j.company_name, j.title FROM applications a JOIN jobs j ON j.id = a.job_id
           WHERE a.status='queued' AND (lower(j.company_name) LIKE ? OR lower(j.title) LIKE ?)""",
        (like, like),
    ).fetchall()
    if not rows:
        console.print(f"[yellow]nothing queued matches[/] {pattern!r}")
        conn.close()
        raise typer.Exit(1)
    with db.transaction(conn):
        conn.execute(
            """UPDATE applications SET status='skipped', notes='skipped by request'
               WHERE status='queued' AND job_id IN (
                 SELECT id FROM jobs
                 WHERE lower(company_name) LIKE ? OR lower(title) LIKE ?)""",
            (like, like),
        )
    conn.close()
    console.print(f"[green]skipped[/] {len(rows)} application(s):")
    for r in rows[:10]:
        console.print(f"  [dim]{r['company_name']} — {r['title'][:50]}[/]")
    if len(rows) > 10:
        console.print(f"  [dim]… and {len(rows) - 10} more[/]")
    console.print("[dim]a running batch picks this up on its next job[/]")


@app.command()
def status() -> None:
    """Show application tracking."""
    settings = load_settings()
    conn = db.connect(settings.db_path)
    counts = conn.execute(
        "SELECT status, COUNT(*) c FROM applications GROUP BY status ORDER BY c DESC"
    ).fetchall()
    rows = conn.execute(
        """SELECT a.status, a.submitted_at, a.filled_at, j.company_name, j.title
           FROM applications a JOIN jobs j ON j.id = a.job_id
           WHERE a.status != 'queued'
           ORDER BY COALESCE(a.submitted_at, a.filled_at) DESC LIMIT 30"""
    ).fetchall()
    conn.close()
    if not counts:
        console.print("[yellow]no applications tracked[/] — `autoapply queue add` first.")
        raise typer.Exit()
    console.print("  ".join(f"[bold]{r['status']}[/] {r['c']}" for r in counts))
    if rows:
        table = Table(title="recent")
        table.add_column("status")
        table.add_column("company")
        table.add_column("title")
        table.add_column("when", style="dim")
        for r in rows:
            table.add_row(
                r["status"], r["company_name"], r["title"],
                (r["submitted_at"] or r["filled_at"] or "")[:16],
            )
        console.print(table)


@app.command()
def export(fmt: str = typer.Argument("csv", help="Export format (csv).")) -> None:
    """Export applications to runs/applications.csv."""
    import csv

    if fmt != "csv":
        console.print(f"[red]unsupported format {fmt!r}[/] — only csv.")
        raise typer.Exit(1)
    settings = load_settings()
    settings.ensure_dirs()
    conn = db.connect(settings.db_path)
    rows = conn.execute(
        """SELECT a.job_id, j.company_name, j.title, j.ats, j.score, a.status,
                  a.filled_at, a.submitted_at, COALESCE(j.final_url, j.url) AS url
           FROM applications a JOIN jobs j ON j.id = a.job_id ORDER BY a.id"""
    ).fetchall()
    conn.close()
    out = settings.runs_dir / "applications.csv"
    with out.open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow([
            "job_id", "company", "title", "ats", "score", "status",
            "filled_at", "submitted_at", "url",
        ])
        w.writerows([list(r) for r in rows])
    console.print(f"[green]exported[/] {len(rows)} rows -> {out}")


@app.command()
def retry(job_id: str = typer.Argument(..., help="Job id (or prefix) to retry.")) -> None:
    """Re-queue a failed/skipped/needs_input application."""
    settings = load_settings()
    conn = db.connect(settings.db_path)
    with db.transaction(conn):
        cur = conn.execute(
            """UPDATE applications SET status='queued', notes=NULL
               WHERE job_id LIKE ? AND status IN ('failed','skipped','needs_input')""",
            (job_id + "%",),
        )
    conn.close()
    if cur.rowcount == 0:
        console.print("[yellow]nothing to retry[/] (must be failed/skipped/needs_input)")
        raise typer.Exit(1)
    console.print(f"[green]re-queued[/] {cur.rowcount} application(s)")


def _edit_text(text: str) -> str | None:
    """Open ``text`` in $EDITOR (fallback vi); return edited text or None on abort."""
    import os
    import subprocess
    import tempfile
    from pathlib import Path

    editor = os.environ.get("EDITOR") or os.environ.get("VISUAL") or "vi"
    with tempfile.NamedTemporaryFile("w", suffix=".txt", delete=False) as f:
        f.write(text)
        path = Path(f.name)
    try:
        result = subprocess.run([*editor.split(), str(path)], check=False)
        if result.returncode != 0:
            return None
        return path.read_text()
    finally:
        path.unlink(missing_ok=True)


@answers_app.command("list")
def answers_list(
    limit: int = typer.Option(50, help="Max rows."),
) -> None:
    """Show cached free-text answers."""
    settings = load_settings()
    conn = db.connect(settings.db_path)
    rows = conn.execute(
        "SELECT id, company, question, answer, edited_at FROM answers"
        " ORDER BY id LIMIT ?",
        (limit,),
    ).fetchall()
    conn.close()
    if not rows:
        console.print("[yellow]no cached answers[/] — they appear after `autoapply run`.")
        raise typer.Exit()

    def trunc(s: str, n: int) -> str:
        return s if len(s) <= n else s[: n - 1] + "…"

    table = Table(title="cached answers")
    table.add_column("id", justify="right", style="bold")
    table.add_column("company")
    table.add_column("question")
    table.add_column("answer")
    table.add_column("edited_at", style="dim")
    for r in rows:
        table.add_row(
            str(r["id"]), r["company"], trunc(r["question"], 40),
            trunc(r["answer"], 60), r["edited_at"] or "—",
        )
    console.print(table)


@answers_app.command("edit")
def answers_edit(answer_id: int = typer.Argument(..., help="Answer id.")) -> None:
    """Edit a cached free-text answer in $EDITOR."""
    from datetime import UTC, datetime

    settings = load_settings()
    conn = db.connect(settings.db_path)
    try:
        row = conn.execute(
            "SELECT question_hash, company, question, answer FROM answers WHERE id = ?",
            (answer_id,),
        ).fetchone()
        if row is None:
            console.print(f"[red]no answer with id {answer_id}[/] — see `autoapply answers list`.")
            raise typer.Exit(code=1)
        edited = _edit_text(row["answer"])
        if edited is None or edited.strip() == row["answer"].strip():
            console.print("[yellow]unchanged[/]")
            raise typer.Exit()
        with db.transaction(conn):
            db.put_answer(
                conn,
                question_hash=row["question_hash"],
                company=row["company"],
                question=row["question"],
                answer=edited.strip(),
                now_iso=datetime.now(UTC).isoformat(),
                edited=True,
            )
        console.print(f"[green]saved[/] answer {answer_id} ({row['company']})")
    finally:
        conn.close()


def _playwright_ready() -> bool:
    """True if a Chromium build looks installed (cheap check, no launch)."""
    if shutil.which("playwright") is None:
        # installed as a python module even without the CLI on PATH; check import
        try:
            import playwright  # noqa: F401
        except ImportError:
            return False
    from pathlib import Path

    cache = Path.home() / "Library" / "Caches" / "ms-playwright"
    linux = Path.home() / ".cache" / "ms-playwright"
    for base in (cache, linux):
        if base.exists() and any(base.glob("chromium-*")):
            return True
    return False


if __name__ == "__main__":
    app()
