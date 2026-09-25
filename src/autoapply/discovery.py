"""Unattended job discovery — decoupled from the apply/fill step.

Discovery is pure HTTP: it fetches job boards, scores them, and writes rows to
SQLite. It must never import Playwright, launch a browser, or call Ollama, so it
can run headless from launchd with no GUI session, no window server, and no
foreground terminal. ``tests/test_discovery.py`` enforces that.

The apply step (:mod:`autoapply.runner`) and this module communicate ONLY
through the SQLite ``jobs`` table. Neither imports the other.

Every run appends one JSON object per source to ``settings.discovery_log`` so a
failure is inspectable after the fact instead of scrolling past in a terminal
nobody was watching. A run that never happens is the failure mode that actually
bit us (2026-08-04, Mac powered off through the 08:00 window), so staleness is
a first-class check here — see :func:`staleness_hours`.
"""

from __future__ import annotations

import datetime as dt
import json
import time
from collections.abc import Callable
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import TypeVar

import httpx

from autoapply.config import Settings

_T = TypeVar("_T")

#: Transient conditions worth retrying. A 404 or a parse error is not one of
#: them — those mean the source changed shape and retrying just wastes time.
_RETRYABLE_STATUS = frozenset({408, 425, 429, 500, 502, 503, 504})


@dataclass(slots=True)
class SourceResult:
    """Outcome of one source within a discovery run."""

    source: str
    ok: bool
    fetched: int = 0
    new: int = 0
    duration_s: float = 0.0
    error: str = ""
    attempts: int = 1


def _is_retryable(exc: BaseException) -> bool:
    if isinstance(exc, httpx.HTTPStatusError):
        return exc.response.status_code in _RETRYABLE_STATUS
    return isinstance(exc, httpx.TimeoutException | httpx.TransportError)


class RetryError(Exception):
    """Wraps a failure with how many attempts it took, for the run log."""

    def __init__(self, original: BaseException, attempts: int) -> None:
        self.original = original
        self.attempts = attempts
        super().__init__(f"{type(original).__name__}: {original}")


def with_retry(
    fn: Callable[[], _T], *, attempts: int = 3, base_delay: float = 2.0
) -> tuple[_T, int]:
    """Call ``fn``, retrying transient HTTP failures with exponential backoff.

    Returns ``(result, attempts_used)``. Raises :class:`RetryError` carrying the
    attempt count, so a failed source records whether retries actually ran
    rather than always logging 1.
    """
    for attempt in range(1, attempts + 1):
        try:
            return fn(), attempt
        except Exception as exc:  # noqa: BLE001 - classified immediately below
            if not _is_retryable(exc) or attempt == attempts:
                raise RetryError(exc, attempt) from exc
            time.sleep(base_delay * (2 ** (attempt - 1)))
    raise AssertionError("unreachable")  # pragma: no cover


def _run_source(name: str, fn: Callable[[], tuple[int, int]], retries: int) -> SourceResult:
    """Run one source to a SourceResult. Never raises — a bad source is data."""
    started = time.monotonic()
    try:
        (fetched, new), attempts = with_retry(fn, attempts=retries)
        return SourceResult(
            source=name, ok=True, fetched=fetched, new=new,
            duration_s=round(time.monotonic() - started, 2), attempts=attempts,
        )
    except RetryError as exc:
        return SourceResult(
            source=name, ok=False, duration_s=round(time.monotonic() - started, 2),
            error=str(exc)[:300], attempts=exc.attempts,
        )
    except Exception as exc:  # noqa: BLE001 - one dead source must not lose the others
        return SourceResult(
            source=name, ok=False, duration_s=round(time.monotonic() - started, 2),
            error=f"{type(exc).__name__}: {exc}"[:300],
        )


def _simplify(settings: Settings) -> tuple[int, int]:
    from autoapply.sources import simplify

    r = simplify.sync(settings)
    return r.total, r.new


def _ycombinator(settings: Settings) -> tuple[int, int]:
    from autoapply.sources import ycombinator

    r = ycombinator.sync(settings)
    return r.fetched, r.new


def _referrals(settings: Settings) -> tuple[int, int]:
    from autoapply.sources import referral

    r = referral.scout(settings)
    # scout() collects per-company errors instead of raising. Fetching nothing
    # while reporting errors is a failure, not an empty day — surface it or a
    # dead referral scraper looks identical to "no new postings".
    if r.errors and r.fetched == 0:
        raise RuntimeError("; ".join(r.errors[:3]))
    return r.fetched, r.upserted


#: name -> callable(settings) -> (fetched, new)
SOURCES: dict[str, Callable[[Settings], tuple[int, int]]] = {
    "simplify": _simplify,
    "ycombinator": _ycombinator,
    "referrals": _referrals,
}


def discover(
    settings: Settings, *, only: tuple[str, ...] | None = None
) -> list[SourceResult]:
    """Run every source, isolated. Appends results to the discovery log."""
    names = only or tuple(SOURCES)
    results = [
        _run_source(n, lambda n=n: SOURCES[n](settings), settings.discovery_retries)
        for n in names
        if n in SOURCES
    ]
    append_results(settings.discovery_log, results)
    return results


# ---- structured log -------------------------------------------------------


def append_results(path: Path, results: list[SourceResult]) -> None:
    """Append one JSON line per source. Best-effort: logging must not fail a run."""
    ts = dt.datetime.now(dt.UTC).isoformat()
    try:
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("a") as fh:
            for r in results:
                fh.write(json.dumps({"ts": ts, **asdict(r)}) + "\n")
    except OSError:
        pass


def read_records(path: Path, *, limit: int = 500) -> list[dict]:
    """Most-recent-last records from the JSONL log. Tolerates partial lines."""
    if not path.exists():
        return []
    out: list[dict] = []
    try:
        lines = path.read_text().splitlines()
    except OSError:
        return []
    for line in lines[-limit:]:
        try:
            out.append(json.loads(line))
        except ValueError:
            continue  # a torn write must not break --status
    return out


def last_success(path: Path) -> dt.datetime | None:
    """Timestamp of the most recent run where at least one source succeeded."""
    for rec in reversed(read_records(path)):
        if rec.get("ok"):
            try:
                return dt.datetime.fromisoformat(rec["ts"])
            except (KeyError, ValueError):
                continue
    return None


def staleness_hours(path: Path) -> float | None:
    """Hours since the last successful discovery; None if there has never been one."""
    ts = last_success(path)
    if ts is None:
        return None
    if ts.tzinfo is None:
        ts = ts.replace(tzinfo=dt.UTC)
    return (dt.datetime.now(dt.UTC) - ts).total_seconds() / 3600


def should_skip(settings: Settings) -> bool:
    """True when a success is recent enough that this scheduled run is redundant.

    Lets the timer fire on boot/wake catch-up without double-syncing when the
    machine reboots twice inside one interval.
    """
    hours = staleness_hours(settings.discovery_log)
    return hours is not None and hours < settings.discovery_min_interval_hours


def is_stale(settings: Settings) -> bool:
    """True when discovery has not succeeded within the watchdog window."""
    hours = staleness_hours(settings.discovery_log)
    return hours is None or hours > settings.discovery_stale_hours
