"""Persistent Playwright Chromium context (SPEC.md §2).

A ``user_data_dir`` context keeps cookies/sessions across runs so repeat visits
to an ATS stay logged in. Headed by default — the human watches the fill and
clicks Submit. Never runs a headless stealth arms race (SPEC.md §10).
"""

from __future__ import annotations

import os
import time
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover
    from playwright.sync_api import BrowserContext, Page



def release_profile_lock(user_data_dir: Path) -> bool:
    """Terminate an orphaned Chrome holding ``user_data_dir`` and clear its locks.

    Only ever touches a process whose command line contains this exact profile
    path, so a user's own Chrome is never affected. Returns True if anything
    was cleaned up.
    """
    import signal as _signal
    import subprocess

    cleaned = False
    lock = user_data_dir / "SingletonLock"
    try:
        target = os.readlink(lock) if lock.is_symlink() else ""
    except OSError:
        target = ""
    pid = target.rsplit("-", 1)[-1] if "-" in target else ""
    if pid.isdigit():
        try:
            cmdline = subprocess.run(
                ["ps", "-o", "command=", "-p", pid],
                capture_output=True, text=True, timeout=5,
            ).stdout
        except (OSError, subprocess.SubprocessError):
            cmdline = ""
        if str(user_data_dir) in cmdline:
            try:
                os.kill(int(pid), _signal.SIGTERM)
                time.sleep(2)
                cleaned = True
            except (OSError, ProcessLookupError):
                pass
    for name in ("SingletonLock", "SingletonCookie", "SingletonSocket"):
        f = user_data_dir / name
        try:
            if f.is_symlink() or f.exists():
                f.unlink()
                cleaned = True
        except OSError:
            pass
    return cleaned

@contextmanager
def launch_context(
    user_data_dir: str | Path, *, headed: bool = True, slow_mo_ms: int = 0
) -> Iterator[tuple[BrowserContext, Page]]:
    """Yield a persistent ``(context, page)`` and clean up on exit.

    ``headed`` defaults True (visible browser). Import of Playwright is deferred
    so the rest of the package (sync/list/init health checks) works before
    ``playwright install`` has run.
    """
    from playwright.sync_api import sync_playwright

    Path(user_data_dir).mkdir(parents=True, exist_ok=True)
    with sync_playwright() as p:
        def _launch():
            return p.chromium.launch_persistent_context(
                user_data_dir=str(user_data_dir),
                headless=not headed,
                slow_mo=slow_mo_ms,
                viewport={"width": 1360, "height": 900},
            )

        try:
            context = _launch()
        except Exception as e:  # noqa: BLE001 - classified by message below
            if "existing browser session" not in str(e):
                raise
            # An orphaned Chrome from a killed run still owns the profile, so
            # every launch fails and an unattended loop burns whole cycles doing
            # nothing. The profile is this tool's alone, so a process holding it
            # is by definition our leftover.
            release_profile_lock(Path(user_data_dir))
            context = _launch()
        page = context.pages[0] if context.pages else context.new_page()
        try:
            yield context, page
        finally:
            context.close()


def flag_field(page: Page, selector: str) -> None:
    """Red-outline a field that needs manual input (SPEC.md §4, §5)."""
    page.eval_on_selector(
        selector,
        "el => { el.style.outline = '3px solid red'; el.style.outlineOffset = '2px'; }",
    )


def looks_blocked(page: Page) -> bool:
    """Heuristic: does the page look like a CAPTCHA / bot wall?

    We never solve these — the caller surfaces the window and waits (SPEC.md §1,
    §10). Detection only, best-effort.
    """
    # Visible text only: ATS pages (e.g. Ashby) ship captcha *scripts* in their
    # bundles on every page, so matching raw HTML false-positives constantly.
    needles = (
        "are you a robot",
        "verify you are human",
        "verifying you are human",
        "please complete the security check",
        "unusual traffic",
    )
    try:
        text = page.inner_text("body").lower()
    except Exception:  # noqa: BLE001 - detection must never raise
        return False
    return any(n in text for n in needles)
