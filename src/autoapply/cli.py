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
messages_app = typer.Typer(help="Inspect/edit generated WaaS founder messages.")
app.add_typer(queue_app, name="queue")
app.add_typer(answers_app, name="answers")
app.add_typer(messages_app, name="messages")

console = Console()

_MS2 = "[yellow]stub[/] — wired in Milestone 2 integration (SPEC.md §9)."
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
    ok, msg = OllamaClient(host=settings.ollama_host, model=settings.ollama_model).health()
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
    source: str = typer.Option("simplify", "--source", help="simplify | waas"),
    resolve_redirects: bool = typer.Option(
        False, help="Follow simplify.jobs links to the final ATS URL (slower)."
    ),
) -> None:
    """Pull a job source, score, and upsert into SQLite."""
    settings = load_settings()
    settings.ensure_dirs()
    if source == "waas":
        from autoapply.sources import waas as waas_src

        with console.status("scraping Work at a Startup…"):
            r = waas_src.sync(settings)
        console.print(
            f"[green]waas synced[/] scraped {r.scraped}, upserted {r.upserted}, "
            f"skipped(messaged) {r.skipped_messaged}, score≥70 {r.scored_ge_70}"
        )
        return
    with console.status("fetching listings…"):
        result = simplify.sync(settings, resolve_redirects=resolve_redirects)
    console.print(
        f"[green]synced[/] {result.total} listings — "
        f"new {result.new}, changed {result.changed}, inactive {result.inactive}, "
        f"score≥70 {result.scored_ge_70}"
    )


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
    job_id: str = typer.Argument(None, help="Job id to enqueue."),
    top: int = typer.Option(None, "--top", help="Enqueue the top-N by score."),
) -> None:
    """Enqueue a job (or the top-N). [stub]"""
    console.print(_MS2)


@app.command()
def run(
    source: str = typer.Option("simplify", "--source", help="simplify | waas"),
    dry_run: bool = typer.Option(False, "--dry-run", help="Plan/print; fill/send nothing."),
    auto_submit: bool = typer.Option(False, "--auto-submit", help="Allowlisted ATSs only."),
    max_per_run: int = typer.Option(15, "--max-per-run", help="Cap applications per run."),
    min_score: int = typer.Option(70, "--min-score", help="Only process jobs at/above this."),
    regen: bool = typer.Option(False, "--regen", help="Regenerate cached WaaS messages."),
) -> None:
    """Process the queue: fill/generate → review pause → human send."""
    if source == "waas":
        # HARD RULE (waas-addendum.md E3): auto-submit is permanently disabled for WaaS.
        if auto_submit:
            console.print("[yellow]--auto-submit ignored for waas[/] (human-approved sends).")
        settings = load_settings()
        profile = load_profile(settings.profile_path)
        client = OllamaClient(host=settings.ollama_host, model=settings.ollama_model)
        if not dry_run:
            client.ensure_available()
        from autoapply.ats import waas as waas_ats

        stats = waas_ats.run_waas(
            settings, profile, client,
            dry_run=dry_run, max_per_run=max_per_run, min_score=min_score, regen=regen,
        )
        console.print(f"[green]waas run[/] {stats}")
        return
    console.print(_MS2)
    console.print("[dim]ATS adapters land via workstreams A–D; wired in Milestone 2.[/]")


@app.command()
def open(job_id: str = typer.Argument(..., help="Manual-tier job id.")) -> None:
    """Open a manual-tier posting in the persistent browser. [stub]"""
    console.print(_MS2)


@app.command()
def status() -> None:
    """Show application tracking. [stub]"""
    console.print(_MS2)


@app.command()
def export(fmt: str = typer.Argument("csv", help="Export format (csv).")) -> None:
    """Export applications. [stub]"""
    console.print(_MS2)


@app.command()
def retry(job_id: str = typer.Argument(..., help="Job id to retry.")) -> None:
    """Retry a failed application. [stub]"""
    console.print(_MS2)


@answers_app.command("edit")
def answers_edit(answer_id: str = typer.Argument(..., help="Answer id.")) -> None:
    """Edit a cached free-text answer. [stub — workstream D]"""
    console.print(_WS_D)


# --- WaaS commands (workstream E) ------------------------------------------


@app.command()
def login(source: str = typer.Argument("waas", help="Source to log into (waas).")) -> None:
    """Open the persistent browser to log in manually. Never stores credentials."""
    if source != "waas":
        console.print("[yellow]only 'waas' login is supported.[/]")
        raise typer.Exit(1)
    from autoapply.sources import waas as waas_src

    settings = load_settings()
    ok = waas_src.login(settings)
    console.print("[green]logged in[/]" if ok else "[yellow]login not detected[/]")


@messages_app.command("list")
def messages_list() -> None:
    """List cached WaaS messages (company → role)."""
    settings = load_settings()
    conn = db.connect(settings.db_path)
    rows = conn.execute(
        "SELECT question_hash, company, question FROM answers WHERE question_hash LIKE 'waas:%'"
    ).fetchall()
    conn.close()
    if not rows:
        console.print("[yellow]no cached messages[/] — run `autoapply run --source waas`.")
        return
    table = Table(title="waas messages")
    table.add_column("key", style="dim")
    table.add_column("company")
    table.add_column("role")
    for r in rows:
        table.add_row(r["question_hash"], r["company"], r["question"])
    console.print(table)


@messages_app.command("edit")
def messages_edit(
    key: str = typer.Argument(..., help="Message key (waas:<role_id>)."),
    company: str = typer.Argument(..., help="Company (cache key)."),
) -> None:
    """Open a cached WaaS message in $EDITOR and save the edit."""
    import os
    import subprocess
    import tempfile

    settings = load_settings()
    conn = db.connect(settings.db_path)
    current = db.get_answer(conn, key, company) or ""
    with tempfile.NamedTemporaryFile("w+", suffix=".txt", delete=False) as fh:
        fh.write(current)
        path = fh.name
    subprocess.call([os.environ.get("EDITOR", "nano"), path])
    with open(path) as fh:
        edited = fh.read().strip()
    import datetime as dt

    db.put_answer(
        conn, question_hash=key, company=company, question="waas message (edited)",
        answer=edited, now_iso=dt.datetime.now(dt.UTC).isoformat(), edited=True,
    )
    conn.commit()
    conn.close()
    console.print("[green]saved.[/]")


@app.command()
def mark(
    job_id: str = typer.Argument(..., help="Job id."),
    state: str = typer.Argument(..., help="replied"),
) -> None:
    """Mark a WaaS application's reply state (e.g. `mark <id> replied`)."""
    if state not in ("replied",):
        console.print("[yellow]only 'replied' is supported.[/]")
        raise typer.Exit(1)
    settings = load_settings()
    conn = db.connect(settings.db_path)
    db.record_application(conn, job_id=job_id, status="replied")
    conn.commit()
    conn.close()
    console.print(f"[green]marked {job_id[:8]} replied.[/]")


@app.command()
def followups(days: int = typer.Option(7, "--days", help="No-reply age threshold.")) -> None:
    """List companies messaged > N days ago with no reply (waas-addendum.md E3)."""
    import datetime as dt

    settings = load_settings()
    conn = db.connect(settings.db_path)
    cutoff = (dt.datetime.now(dt.UTC) - dt.timedelta(days=days)).isoformat()
    rows = db.list_followups(conn, before_iso=cutoff)
    conn.close()
    if not rows:
        console.print(f"[green]no followups[/] older than {days}d.")
        return
    table = Table(title=f"followups (> {days}d, no reply)")
    table.add_column("company")
    table.add_column("role")
    table.add_column("sent")
    table.add_column("id", style="dim")
    for r in rows:
        sent = (r["submitted_at"] or "")[:10]
        table.add_row(r["company_name"], r["title"], sent, r["job_id"][:8])
    console.print(table)


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
