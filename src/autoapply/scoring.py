"""Auditable scoring and additive SQLite storage; no frozen DB API changes."""
from __future__ import annotations

import json
import sqlite3
from dataclasses import dataclass, field


@dataclass
class Score:
    contributions: dict[str, int] = field(default_factory=dict)
    excluded: str | None = None

    @property
    def total(self) -> int:
        return sum(self.contributions.values())


def finish(parts: dict[str, int]) -> Score:
    raw = sum(parts.values())
    adjustment = max(0, min(100, raw)) - raw
    if adjustment:
        parts["clamp"] = adjustment
    return Score(parts)


def persist(conn: sqlite3.Connection, job_id: str, score: Score, source: str) -> None:
    """Call inside the same transaction as the job upsert. Legacy rows stay unknown."""
    conn.execute("""CREATE TABLE IF NOT EXISTS job_score_breakdowns (
        job_id TEXT PRIMARY KEY REFERENCES jobs(id) ON DELETE CASCADE,
        source TEXT NOT NULL, contributions TEXT NOT NULL, excluded TEXT,
        total INTEGER NOT NULL, scored_at TEXT NOT NULL DEFAULT CURRENT_TIMESTAMP
    )""")
    conn.execute("""INSERT INTO job_score_breakdowns
        (job_id, source, contributions, excluded, total) VALUES (?, ?, ?, ?, ?)
        ON CONFLICT(job_id) DO UPDATE SET source=excluded.source,
        contributions=excluded.contributions, excluded=excluded.excluded,
        total=excluded.total, scored_at=CURRENT_TIMESTAMP""",
        (job_id, source, json.dumps(score.contributions), score.excluded, score.total))


def refresh_existing(
    conn: sqlite3.Connection, job_id: str, score: Score, source: str,
) -> None:
    """Do not leave an old eligible score after a config change rejects a job."""
    changed = conn.execute("UPDATE jobs SET score=? WHERE id=?", (score.total, job_id))
    if changed.rowcount:
        persist(conn, job_id, score, source)


def explain(conn: sqlite3.Connection, job_id: str, total: int) -> str:
    exists = conn.execute(
        "SELECT 1 FROM sqlite_master WHERE type='table' AND name='job_score_breakdowns'"
    ).fetchone()
    row = conn.execute(
        "SELECT contributions, excluded, total FROM job_score_breakdowns WHERE job_id=?",
        (job_id,),
    ).fetchone() if exists else None
    if row is None or row[2] != total:
        return "unavailable; rediscover to record breakdown"
    if row[1]:
        return "excluded: " + row[1]
    return ", ".join(f"{k}={v:+d}" for k, v in json.loads(row[0]).items())
