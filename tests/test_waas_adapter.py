"""Adapter DOM interaction against fixture HTML loaded into a real page.

Skipped when Chromium isn't available (e.g. minimal CI) — the parse/validation
tests cover the logic without a browser.
"""

from __future__ import annotations

from pathlib import Path

import pytest

from autoapply.ats import waas as waas_ats

FIXTURES = Path(__file__).parent / "fixtures"


@pytest.fixture
def page():
    pw = pytest.importorskip("playwright.sync_api")
    try:
        p = pw.sync_playwright().start()
        browser = p.chromium.launch()
    except Exception as e:  # noqa: BLE001 - no browser binary installed
        pytest.skip(f"chromium unavailable: {e}")
    pg = browser.new_page()
    yield pg
    browser.close()
    p.stop()


def test_fill_message_types_into_interest_box(page):
    page.set_content((FIXTURES / "synthetic_role.html").read_text())
    adapter = waas_ats.WaasMessageAdapter()
    ok = adapter.fill_message(page, "Hi Vellum, hello.")
    assert ok is True
    assert page.locator("textarea[name='message']").input_value() == "Hi Vellum, hello."


def test_fill_message_returns_false_when_no_box(page):
    page.set_content("<html><body><p>no form here</p></body></html>")
    adapter = waas_ats.WaasMessageAdapter()
    assert adapter.fill_message(page, "x") is False
