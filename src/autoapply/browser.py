"""Persistent Playwright Chromium context (SPEC.md §2).

A ``user_data_dir`` context keeps cookies/sessions across runs so repeat visits
to an ATS stay logged in. Headed by default — the human watches the fill and
clicks Submit. Never runs a headless stealth arms race (SPEC.md §10).
"""

from __future__ import annotations

from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path
from typing import TYPE_CHECKING

if TYPE_CHECKING:  # pragma: no cover
    from playwright.sync_api import BrowserContext, Page


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
        context = p.chromium.launch_persistent_context(
            user_data_dir=str(user_data_dir),
            headless=not headed,
            slow_mo=slow_mo_ms,
            viewport={"width": 1360, "height": 900},
        )
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
    needles = ("captcha", "are you a robot", "verify you are human", "cf-challenge", "hcaptcha")
    try:
        html = page.content().lower()
    except Exception:  # noqa: BLE001 - detection must never raise
        return False
    return any(n in html for n in needles)
