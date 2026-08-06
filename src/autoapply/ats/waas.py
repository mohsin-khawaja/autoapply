"""WaaS send flow (waas-addendum.md E3).

The message flow does NOT fit the frozen ``BaseAdapter`` (no profile->field
mapping, no ATS submit). This module defines a **sibling** ``MessageAdapter`` ABC
and a ``WaasMessageAdapter``. PR proposes promoting ``MessageAdapter`` to
``ats/base.py`` via a future interface PR — it is NOT added to base.py here.

SAFETY — hard rules (waas-addendum.md E3), enforced and tested:
- Auto-submit is PERMANENTLY DISABLED for WaaS. ``submit()`` always raises; the run
  loop never calls it. Every send is a human keypress at the review pause.
- Max 10 sends/day (config ceiling 15), 60-180s jittered between sends.
- Any CAPTCHA/rate-limit signal stops the run; remaining queue -> ``deferred``.
"""

from __future__ import annotations

import datetime as dt
import random
from abc import ABC, abstractmethod
from pathlib import Path
from typing import TYPE_CHECKING, ClassVar

from autoapply import db
from autoapply.config import Settings
from autoapply.ollama import OllamaClient
from autoapply.profile import Profile
from autoapply.sources import waas as waas_src
from autoapply.sources.waas import WAAS_ATS, WaasRole

if TYPE_CHECKING:  # pragma: no cover
    from playwright.sync_api import Page

# --- caps (waas-addendum.md E3). Live here, not shared config.py. ----------
MAX_SENDS_PER_DAY = 10
SENDS_PER_DAY_CEILING = 15
JITTER_MIN_SECONDS = 60
JITTER_MAX_SECONDS = 180

INTEREST_SELECTORS = (
    "[data-testid='interest-message']",
    "textarea[name='message']",
    "textarea",
)


class MessageAdapter(ABC):
    """Sibling of ``BaseAdapter`` for free-text founder messages (not ATS forms)."""

    source: ClassVar[str]
    #: Enforced constant — WaaS sends are human-approved only. Never overridden True->False.
    AUTO_SUBMIT_DISABLED: ClassVar[bool] = True

    @classmethod
    @abstractmethod
    def detect(cls, url: str) -> bool:
        """True if this adapter handles ``url``."""

    @abstractmethod
    def open_role(self, page: Page, url: str) -> None:
        """Navigate to the role page."""

    @abstractmethod
    def fill_message(self, page: Page, message: str) -> bool:
        """Type ``message`` into the interest box. Never sends. Returns success."""

    def review_pause(self, page: Page, message: str, run_dir: Path, company: str) -> Path:
        """Screenshot, print the message, and block for human approval.

        Returns the screenshot path. Not overridden by concrete adapters.
        """
        run_dir.mkdir(parents=True, exist_ok=True)
        shot = run_dir / f"{waas_src._slug(company) or 'role'}.png"
        try:
            page.screenshot(path=str(shot))
        except Exception:  # noqa: BLE001 - screenshot best-effort
            shot = run_dir / "no-screenshot"
        print("\n----- MESSAGE (review) -----\n" + message + "\n----------------------------")
        return shot

    def submit(self, page: Page):
        """HARD-DISABLED. WaaS never auto-submits (waas-addendum.md E3)."""
        raise RuntimeError("WaaS never auto-submits; every send is human-approved")


class WaasMessageAdapter(MessageAdapter):
    source = WAAS_ATS

    @classmethod
    def detect(cls, url: str) -> bool:
        return waas_src.WAAS_HOST in (url or "")

    def open_role(self, page: Page, url: str) -> None:
        page.goto(url, wait_until="networkidle")

    def fill_message(self, page: Page, message: str) -> bool:
        for sel in INTEREST_SELECTORS:
            loc = page.locator(sel)
            if loc.count() > 0:
                loc.first.fill(message)
                return True
        return False


# --- helpers ---------------------------------------------------------------


def sends_today(conn, *, today: str | None = None) -> int:
    """Count messages sent today (status='messaged', submitted_at is today)."""
    day = today or dt.datetime.now(dt.UTC).date().isoformat()
    row = conn.execute(
        "SELECT COUNT(*) FROM applications WHERE status = 'messaged' "
        "AND substr(submitted_at, 1, 10) = ?",
        (day,),
    ).fetchone()
    return row[0]


def daily_cap(settings_cap: int | None = None) -> int:
    """Resolve the per-day cap, clamped to the ceiling."""
    cap = MAX_SENDS_PER_DAY if settings_cap is None else settings_cap
    return max(0, min(cap, SENDS_PER_DAY_CEILING))


def jitter_seconds() -> float:
    return random.uniform(JITTER_MIN_SECONDS, JITTER_MAX_SECONDS)


def pending_waas_roles(conn, *, min_score: int = 0) -> list[str]:
    """Role ids queued for messaging: waas jobs with no send/defer/reply yet."""
    rows = conn.execute(
        """SELECT j.id FROM jobs j
           LEFT JOIN applications a ON a.job_id = j.id
           WHERE j.ats = ? AND j.score >= ?
             AND (a.status IS NULL OR a.status = 'queued')
           ORDER BY j.score DESC""",
        (WAAS_ATS, min_score),
    ).fetchall()
    return [r[0] for r in rows]


# --- orchestration ---------------------------------------------------------


def run_waas(
    settings: Settings,
    profile: Profile,
    client: OllamaClient,
    *,
    dry_run: bool = False,
    max_per_run: int = MAX_SENDS_PER_DAY,
    min_score: int = 0,
    regen: bool = False,
) -> dict[str, int]:
    """Process the WaaS queue: generate -> review pause -> human send.

    Dry-run prints generated messages (no browser, no records). Live run opens
    each role, fills the interest box, pauses for approval, and records
    ``messaged`` on approval. Stops on a CAPTCHA/block signal and marks the
    remainder ``deferred``. NEVER auto-submits.
    """
    from autoapply.filling import waas_messages as wm

    conn = db.connect(settings.db_path)
    now = dt.datetime.now(dt.UTC)
    cap = daily_cap(getattr(settings, "waas_daily_cap", None))
    budget = min(max_per_run, cap - sends_today(conn))
    role_ids = pending_waas_roles(conn, min_score=min_score)
    stats = {"generated": 0, "messaged": 0, "deferred": 0, "skipped": 0, "flagged": 0}

    if budget <= 0:
        print(f"daily cap reached ({cap}/day); nothing to send.")
        conn.close()
        return stats

    if dry_run:
        for rid in role_ids[:max_per_run]:
            role = waas_src.load_role(conn, rid)
            if role is None:
                stats["skipped"] += 1
                continue
            msg, problems = _message_for(conn, profile, role, client, wm, regen=regen)
            stats["generated"] += 1
            if problems:
                stats["flagged"] += 1
            print(f"\n=== {role.company} — {role.title} ===")
            print(msg or "(no message)")
            if problems:
                print("FLAGGED:", "; ".join(problems))
        conn.close()
        return stats

    _run_live(settings, conn, profile, client, wm, now, budget, role_ids, stats)
    conn.close()
    return stats


def _message_for(conn, profile, role: WaasRole, client, wm, *, regen: bool):
    """Return (message, problems), using the answer cache unless ``regen``."""
    key = wm.cache_key(role)
    if not regen:
        cached = db.get_answer(conn, key, role.company)
        if cached:
            return cached, wm.validate_message(cached, profile, role)
    msg, problems = wm.generate_message(profile, role, client)
    if msg and not problems:
        db.put_answer(
            conn, question_hash=key, company=role.company,
            question=f"waas message for {role.title}", answer=msg,
            now_iso=dt.datetime.now(dt.UTC).isoformat(),
        )
    return msg, problems


def _run_live(settings, conn, profile, client, wm, now, budget, role_ids, stats) -> None:
    from autoapply.browser import launch_context, looks_blocked

    run_dir = settings.runs_dir / now.strftime("%Y%m%d-%H%M%S")
    adapter = WaasMessageAdapter()
    settings.ensure_dirs()
    with launch_context(settings.browser_data_dir, headed=True) as (_ctx, page):
        for rid in role_ids:
            if stats["messaged"] >= budget:
                break
            role = waas_src.load_role(conn, rid)
            if role is None:
                stats["skipped"] += 1
                continue
            adapter.open_role(page, role.url)
            if not waas_src.is_logged_in(page) or looks_blocked(page):
                print("blocked or logged out — deferring the rest.")
                _defer_remaining(conn, role_ids[role_ids.index(rid):], now)
                stats["deferred"] += 1
                break
            msg, problems = _message_for(conn, profile, role, client, wm, regen=False)
            if not msg:
                stats["skipped"] += 1
                continue
            adapter.fill_message(page, msg)
            adapter.review_pause(page, msg, run_dir, role.company)
            # Human decides. This NEVER auto-clicks submit.
            choice = input("approve send? [y]es / [s]kip / [q]uit: ").strip().lower()
            if choice == "q":
                _defer_remaining(conn, role_ids[role_ids.index(rid) + 1:], now)
                break
            if choice != "y":
                stats["skipped"] += 1
                continue
            db.record_application(
                conn, job_id=rid, status="messaged",
                submitted_at=dt.datetime.now(dt.UTC).isoformat(),
                screenshot=str(run_dir / f"{waas_src._slug(role.company)}.png"),
                notes="waas message sent (human-approved)",
            )
            conn.commit()
            stats["messaged"] += 1
            if stats["messaged"] < budget:
                page.wait_for_timeout(int(jitter_seconds() * 1000))


def _defer_remaining(conn, role_ids: list[str], now) -> None:
    for rid in role_ids:
        db.record_application(
            conn, job_id=rid, status="deferred",
            notes="deferred: rate/CAPTCHA/quit", filled_at=now.isoformat(),
        )
    conn.commit()
