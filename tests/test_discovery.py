"""Discovery: decoupling, retry/backoff, structured logging, staleness."""

from __future__ import annotations

import datetime as dt
import json

import httpx
import pytest

from autoapply import discovery
from autoapply.config import Settings
from autoapply.discovery import RetryError, SourceResult, with_retry


def _settings(tmp_path) -> Settings:
    s = Settings(home=tmp_path)
    s.repo_root = tmp_path  # discovery_log hangs off runs_dir
    return s


# ---- decoupling ------------------------------------------------------------


def test_discovery_never_imports_the_browser_or_llm():
    """Discovery must run headless under launchd: no Playwright, no Ollama.

    This is the structural guarantee behind "decoupled from apply/fill" — if
    someone imports the runner here, discovery silently regains a GUI/session
    dependency and starts failing only when unattended.
    """
    src = (discovery.__file__)
    text = open(src).read()
    for forbidden in ("playwright", "launch_context", "OllamaClient", "from autoapply.runner"):
        assert forbidden not in text, f"discovery imports {forbidden}"


def test_runner_does_not_import_discovery():
    """The two halves talk only through SQLite."""
    from autoapply import runner

    assert "import discovery" not in open(runner.__file__).read()


# ---- retry -----------------------------------------------------------------


def test_transient_failures_retry_with_backoff(monkeypatch):
    slept: list[float] = []
    monkeypatch.setattr(discovery.time, "sleep", slept.append)
    calls = []

    def boom():
        calls.append(1)
        raise httpx.ConnectTimeout("network blip")

    with pytest.raises(RetryError) as ei:
        with_retry(boom, attempts=3, base_delay=2.0)
    assert len(calls) == 3
    assert ei.value.attempts == 3
    assert slept == [2.0, 4.0]  # exponential, not fixed


def test_non_transient_failure_does_not_burn_the_budget(monkeypatch):
    monkeypatch.setattr(discovery.time, "sleep", lambda _s: None)
    calls = []

    def bad_shape():
        calls.append(1)
        raise ValueError("YC changed their page shape")

    with pytest.raises(RetryError):
        with_retry(bad_shape, attempts=3)
    assert len(calls) == 1, "a parse error must fail fast, not retry"


def test_http_status_retry_classification(monkeypatch):
    monkeypatch.setattr(discovery.time, "sleep", lambda _s: None)

    def status(code: int):
        def fn():
            raise httpx.HTTPStatusError(
                "boom", request=httpx.Request("GET", "https://x"),
                response=httpx.Response(code),
            )
        return fn

    with pytest.raises(RetryError) as ei:
        with_retry(status(503), attempts=2)
    assert ei.value.attempts == 2  # retried
    with pytest.raises(RetryError) as ei2:
        with_retry(status(404), attempts=2)
    assert ei2.value.attempts == 1  # not retried


# ---- isolation + logging ---------------------------------------------------


def test_one_dead_source_does_not_lose_the_others(tmp_path, monkeypatch):
    s = _settings(tmp_path)
    monkeypatch.setitem(discovery.SOURCES, "good", lambda _s: (10, 2))
    monkeypatch.setitem(
        discovery.SOURCES, "bad", lambda _s: (_ for _ in ()).throw(RuntimeError("down"))
    )
    results = {r.source: r for r in discovery.discover(s, only=("good", "bad"))}
    assert results["good"].ok and results["good"].fetched == 10
    assert not results["bad"].ok and "down" in results["bad"].error


def test_run_is_recorded_as_json_lines(tmp_path, monkeypatch):
    s = _settings(tmp_path)
    monkeypatch.setitem(discovery.SOURCES, "good", lambda _s: (5, 1))
    discovery.discover(s, only=("good",))
    lines = s.discovery_log.read_text().strip().splitlines()
    rec = json.loads(lines[-1])
    assert rec["source"] == "good" and rec["ok"] is True and rec["new"] == 1
    assert "ts" in rec and "duration_s" in rec


def test_status_survives_a_torn_log_line(tmp_path):
    """A partial write must not break --status or the staleness check."""
    s = _settings(tmp_path)
    s.discovery_log.parent.mkdir(parents=True, exist_ok=True)
    s.discovery_log.write_text('{"ts": "2026-08-05T00:00:00+00:00", "ok": true}\n{"tr')
    assert len(discovery.read_records(s.discovery_log)) == 1
    assert discovery.last_success(s.discovery_log) is not None


# ---- staleness (the 2026-08-04 failure) ------------------------------------


def _log(s: Settings, hours_ago: float, ok: bool = True) -> None:
    ts = dt.datetime.now(dt.UTC) - dt.timedelta(hours=hours_ago)
    s.discovery_log.parent.mkdir(parents=True, exist_ok=True)
    s.discovery_log.write_text(
        json.dumps({"ts": ts.isoformat(), "source": "simplify", "ok": ok}) + "\n"
    )


def test_recent_success_skips_a_redundant_scheduled_run(tmp_path):
    s = _settings(tmp_path)
    _log(s, hours_ago=1)
    assert discovery.should_skip(s) is True


def test_run_proceeds_once_the_interval_has_passed(tmp_path):
    s = _settings(tmp_path)
    _log(s, hours_ago=7)
    assert discovery.should_skip(s) is False


def test_long_outage_is_flagged_stale(tmp_path):
    """The Aug 4 case: Mac powered off through the window, nothing ran."""
    s = _settings(tmp_path)
    _log(s, hours_ago=30)
    assert discovery.is_stale(s) is True


def test_never_run_counts_as_stale(tmp_path):
    assert discovery.is_stale(_settings(tmp_path)) is True


def test_failed_runs_do_not_count_as_success(tmp_path):
    """A logged failure must not reset the staleness clock."""
    s = _settings(tmp_path)
    _log(s, hours_ago=1, ok=False)
    assert discovery.last_success(s.discovery_log) is None
    assert discovery.is_stale(s) is True
    assert discovery.should_skip(s) is False  # must retry, not skip


# ---- notification ----------------------------------------------------------


def test_ntfy_is_opt_in(monkeypatch, tmp_path):
    """No topic configured => no request leaves the machine."""
    from autoapply import notify

    called = []
    monkeypatch.setattr(notify.httpx, "post", lambda *a, **k: called.append(1))
    assert notify._ntfy("t", "m", _settings(tmp_path)) is False
    assert not called


def test_a_broken_notifier_never_breaks_the_run(monkeypatch, tmp_path):
    from autoapply import notify

    monkeypatch.setattr(notify, "_macos", lambda *a: (_ for _ in ()).throw(OSError("nope")))
    monkeypatch.setattr(notify, "_mail", lambda *a: True)
    assert notify.notify("t", "m", _settings(tmp_path)) == ["mail"]


def test_source_result_defaults():
    r = SourceResult(source="x", ok=True)
    assert r.fetched == 0 and r.attempts == 1 and r.error == ""
