"""Guarded visible submit controls for Lever and Ashby (integration layer)."""

from __future__ import annotations

import re
import time
from pathlib import Path

from playwright.sync_api import Page

from autoapply.ats.base import ATSKind, FillPlan, FillResult

_CONFIRM = re.compile(
    r"thank you for (?:applying|your application)|application (?:has been )?submitted|"
    r"(?:we have|we've) received your application",
    re.I,
)
_SUBMIT = re.compile(r"^\s*submit(?:\s+application)?\s*$", re.I)


def submit_application(
    page: Page,
    plan: FillPlan,
    allowlist: set[ATSKind],
    shot_path: Path | None = None,
    *,
    timeout_ms: int = 10_000,
) -> FillResult:
    """One click only; an uncertain outcome must never become an automatic retry."""
    if plan.ats not in {ATSKind.LEVER, ATSKind.ASHBY} or plan.ats not in allowlist:
        return FillResult(status="needs_input", notes="ATS not enabled for submission")
    if plan.unresolved:
        return FillResult(status="needs_input", notes="Required fields unresolved")
    before_url = page.url
    before_text = page.locator("body").inner_text()
    if _CONFIRM.search(before_text):
        return FillResult(status="filled", notes="Confirmation already visible; inspect history")
    candidates = page.get_by_role("button", name=_SUBMIT).all()
    buttons = [b for b in candidates if b.is_visible() and b.is_enabled()]
    if len(buttons) != 1:
        return FillResult(status="needs_input", notes="Expected one visible enabled Submit button")
    # Native validation catches blank required controls the extraction missed.
    if page.locator("input:invalid, select:invalid, textarea:invalid").count():
        return FillResult(status="needs_input", notes="Native required-field validation failed")
    result = FillResult(
        status="filled", notes="Submit outcome uncertain; verify manually, do not retry"
    )
    try:
        buttons[0].click(timeout=10_000)
        deadline = time.monotonic() + timeout_ms / 1000
        while True:
            errors = page.locator("[aria-invalid='true']:visible, .field-error:visible")
            if errors.count():
                result = FillResult(
                    status="needs_input", notes="Submit rejected: validation errors"
                )
                break
            text = page.locator("body").inner_text()
            confirmed_text = bool(_CONFIRM.search(text))
            confirmed_url = page.url != before_url and bool(
                re.search(r"/(?:confirmation|thank-you|thank_you|submitted)(?:[/?#]|$)", page.url)
            )
            if confirmed_text or confirmed_url:
                result = FillResult(
                    status="submitted",
                    confirmation_detected=True,
                    notes="Submission confirmed by visible message or confirmation URL",
                )
                break
            if time.monotonic() >= deadline:
                break
            page.wait_for_timeout(200)
    except Exception:  # noqa: BLE001 - a click timeout may still have sent the application
        pass
    if shot_path is not None:
        try:
            page.screenshot(path=str(shot_path), full_page=True)
        except Exception:  # noqa: BLE001 - do not lose outcome if screenshot fails
            pass
    return result
