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
def scout(
    min_score: int = typer.Option(0, "--min-score", help="Skip listings scoring below this."),
) -> None:
    """Pull big-tech career sites (Workday tenants + IBM), fit-score, upsert."""
    from autoapply.sources import bigtech

    settings = load_settings()
    settings.ensure_dirs()
    with console.status("scouting big-tech career sites…"):
        result = bigtech.scout(settings, min_score=min_score)
    console.print(
        f"[green]scouted[/] {result.fetched} listings — "
        f"upserted {result.upserted}, score≥70 {result.scored_ge_70}"
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
    job_id: str = typer.Argument(None, help="Job id to enqueue."),
    top: int = typer.Option(None, "--top", help="Enqueue the top-N by score."),
) -> None:
    """Enqueue a job (or the top-N). [stub]"""
    console.print(_MS2)


@app.command()
def run(
    dry_run: bool = typer.Option(False, "--dry-run", help="Plan field mappings; fill nothing."),
    auto_submit: bool = typer.Option(False, "--auto-submit", help="Allowlisted ATSs only."),
    max_per_run: int = typer.Option(15, "--max-per-run", help="Cap applications per run."),
) -> None:
    """Process the queue: fill → review pause → submit. [stub — needs adapters]"""
    console.print(_MS2)
    console.print(
        "[dim]adapters land via workstreams A–D; `run` is wired in Milestone 2.[/]"
    )


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
