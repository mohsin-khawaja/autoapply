"""Tests for workstream D: LLM answer generation, caching, and CLI editing."""

from __future__ import annotations

import sqlite3
from dataclasses import dataclass
from pathlib import Path

import pytest
from typer.testing import CliRunner

from autoapply import db
from autoapply.cli import app
from autoapply.filling import llm_answers
from autoapply.filling.mapper import question_hash
from autoapply.ollama import OllamaClient
from autoapply.profile import Identity, Profile

runner = CliRunner()


@dataclass
class Job:
    company_name: str
    title: str


@pytest.fixture
def profile() -> Profile:
    return Profile(
        identity=Identity(first_name="Ada", last_name="Lovelace", email="ada@example.com"),
        experience=[
            {"company": "Analytical Engines Inc", "title": "ML Engineer",
             "start": "2022-01", "end": "present", "summary": "Built training pipelines"},
        ],
        education=[{"school": "UC Berkeley", "degree": "BS", "major": "EECS"}],
        skills=["Python", "PyTorch"],
    )


@pytest.fixture
def job() -> Job:
    return Job(company_name="Globex", title="Software Engineer I")


class FakeOllama(OllamaClient):
    """Records chat calls; returns a canned answer. No network."""

    def __init__(self, reply: str = "I am excited to apply.") -> None:
        super().__init__()
        self.reply = reply
        self.calls: list[list[dict[str, str]]] = []

    def chat(self, messages, *, temperature=0.2, model=None):  # type: ignore[override]
        self.calls.append(messages)
        return self.reply


# ---- prompt construction ----------------------------------------------------


def test_prompt_includes_profile_facts_and_grounding(profile: Profile, job: Job) -> None:
    msgs = llm_answers.build_messages("Why do you want to work here?", profile, job)
    system = msgs[0]["content"]
    user = msgs[1]["content"]
    assert "never invent" in system.lower()
    assert "first person" in system.lower()
    assert "120 words" in system
    assert "Ada Lovelace" in user
    assert "Analytical Engines Inc" in user
    assert "UC Berkeley" in user
    assert "Python" in user
    assert "Globex" in user
    assert "Why do you want to work here?" in user


def test_generate_answer_calls_client(profile: Profile, job: Job) -> None:
    client = FakeOllama(reply="  Because I love building.  ")
    out = llm_answers.generate_answer("Why us?", profile, job, client)
    assert out == "Because I love building."
    assert len(client.calls) == 1


# ---- cache behavior ---------------------------------------------------------


@pytest.fixture
def conn(tmp_path: Path):
    c = db.connect(tmp_path / "test.db")
    yield c
    c.close()


def test_cache_miss_generates_and_stores(
    conn: sqlite3.Connection, profile: Profile, job: Job
) -> None:
    client = FakeOllama(reply="Generated answer.")
    q = "Describe a project you are proud of."
    out = llm_answers.get_or_generate(
        conn, q, job.company_name, profile=profile, job=job, client=client
    )
    assert out == "Generated answer."
    assert len(client.calls) == 1
    assert db.get_answer(conn, question_hash(q), job.company_name) == "Generated answer."


def test_cache_hit_skips_generation(
    conn: sqlite3.Connection, profile: Profile, job: Job
) -> None:
    q = "Why this role?"
    db.put_answer(
        conn,
        question_hash=question_hash(q),
        company=job.company_name,
        question=q,
        answer="Cached answer.",
        now_iso="2026-01-01T00:00:00+00:00",
    )
    conn.commit()
    client = FakeOllama()
    out = llm_answers.get_or_generate(
        conn, q, job.company_name, profile=profile, job=job, client=client
    )
    assert out == "Cached answer."
    assert client.calls == []


def test_question_hash_stable_and_normalized() -> None:
    assert question_hash("Why us?") == question_hash("  why US?  ")
    assert question_hash("Why us?") != question_hash("Why them?")
    assert len(question_hash("Why us?")) == 16


# ---- CLI --------------------------------------------------------------------


def _seed(db_path: Path) -> int:
    conn = db.connect(db_path)
    db.put_answer(
        conn,
        question_hash=question_hash("Why us?"),
        company="Globex",
        question="Why us?",
        answer="Original answer.",
        now_iso="2026-01-01T00:00:00+00:00",
    )
    conn.commit()
    row_id = conn.execute("SELECT id FROM answers").fetchone()["id"]
    conn.close()
    return int(row_id)


@pytest.fixture
def cli_db(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    db_path = tmp_path / "cli.db"

    class FakeSettings:
        pass

    s = FakeSettings()
    s.db_path = db_path
    monkeypatch.setattr("autoapply.cli.load_settings", lambda: s)
    return db_path


def test_answers_list_shows_rows(cli_db: Path) -> None:
    _seed(cli_db)
    result = runner.invoke(app, ["answers", "list"])
    assert result.exit_code == 0
    assert "Globex" in result.output
    assert "Why us?" in result.output


def test_answers_list_empty(cli_db: Path) -> None:
    result = runner.invoke(app, ["answers", "list"])
    assert result.exit_code == 0
    assert "no cached answers" in result.output


def test_answers_edit_updates_row(cli_db: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    row_id = _seed(cli_db)
    monkeypatch.setattr("autoapply.cli._edit_text", lambda text: "Edited answer.")
    result = runner.invoke(app, ["answers", "edit", str(row_id)])
    assert result.exit_code == 0
    conn = db.connect(cli_db)
    row = conn.execute("SELECT answer, edited_at FROM answers WHERE id = ?", (row_id,)).fetchone()
    conn.close()
    assert row["answer"] == "Edited answer."
    assert row["edited_at"] is not None


def test_answers_edit_missing_id(cli_db: Path) -> None:
    result = runner.invoke(app, ["answers", "edit", "999"])
    assert result.exit_code == 1
    assert "no answer with id 999" in result.output


def test_answers_edit_unchanged_keeps_row(
    cli_db: Path, monkeypatch: pytest.MonkeyPatch
) -> None:
    row_id = _seed(cli_db)
    # editor closed without saving
    monkeypatch.setattr("autoapply.cli._edit_text", lambda text: None)
    result = runner.invoke(app, ["answers", "edit", str(row_id)])
    assert result.exit_code == 0
    conn = db.connect(cli_db)
    row = conn.execute("SELECT answer, edited_at FROM answers WHERE id = ?", (row_id,)).fetchone()
    conn.close()
    assert row["answer"] == "Original answer."
    assert row["edited_at"] is None
