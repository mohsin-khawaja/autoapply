"""GenericAdapter tests: fallback detect, best-effort extract, graceful degradation."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from autoapply.ats import base
from autoapply.ats.generic import GenericAdapter

if TYPE_CHECKING:
    from playwright.sync_api import Page

FIXTURES = Path(__file__).parent / "fixtures"
GENERIC_URL = FIXTURES.joinpath("generic_apply.html").as_uri()
NO_FORM_URL = FIXTURES.joinpath("no_form.html").as_uri()


@pytest.fixture(scope="module")
def page():
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        pg = browser.new_page()
        yield pg
        browser.close()


def _plan(fields: list[base.FieldPlan], resume: Path | None = None) -> base.FillPlan:
    return base.FillPlan(
        job_id="zzzz9999",
        job_url="https://random-company.com/careers/apply",
        ats=base.ATSKind.GENERIC,
        resume_path=resume,
        fields=fields,
    )


def _fp(field: base.FormField, value, source="profile", needs_input=False, conf=0.95):
    return base.FieldPlan(
        field=field, value=value, source=source, confidence=conf, needs_input=needs_input
    )


class TestDetect:
    def test_any_http_url(self):
        assert GenericAdapter.detect("https://random-company.com/careers")
        assert GenericAdapter.detect("http://example.org/apply?id=1")

    def test_non_http(self):
        assert not GenericAdapter.detect("ftp://example.com/x")
        assert not GenericAdapter.detect("mailto:hr@example.com")

    def test_is_fallback(self):
        assert base.resolve_adapter("https://random-company.com/careers") is GenericAdapter
        assert base.resolve_adapter("https://jobs.lever.co/x/y") is not GenericAdapter


class TestExtract:
    def test_visible_form_fields(self, page: Page):
        page.goto(GENERIC_URL)
        fields = GenericAdapter().extract_form(page)
        by_name = {f.name: f for f in fields}
        assert "job_ref" not in by_name  # hidden skipped
        assert "honeypot" not in by_name  # display:none skipped
        assert "q" not in by_name  # search/nav form skipped
        assert by_name["first_name"].label == "First name"  # <label for>
        assert by_name["first_name"].autocomplete == "given-name"
        assert by_name["first_name"].required
        assert by_name["pronouns"].label == "Pronouns"  # aria-label
        assert by_name["email"].field_type == "email"
        assert by_name["cover_letter"].field_type == "textarea"
        assert by_name["source"].options == ["LinkedIn", "Friend"]
        assert by_name["resume"].field_type == "file"

    def test_no_form_returns_empty(self, page: Page):
        page.goto(NO_FORM_URL)
        assert GenericAdapter().extract_form(page) == []


class TestFill:
    def test_fill_upload_flag_and_no_submit(self, page: Page, tmp_path: Path):
        page.goto(GENERIC_URL)
        resume = tmp_path / "resume.pdf"
        resume.write_bytes(b"%PDF-1.4 fake")
        adapter = GenericAdapter()
        fields = {f.name: f for f in adapter.extract_form(page)}
        plans = [
            _fp(fields["first_name"], "Mohsin"),
            _fp(fields["last_name"], "Khawaja"),
            _fp(fields["email"], "mohsinkhawaja10@gmail.com"),
            _fp(fields["source"], "LinkedIn"),
            _fp(fields["resume"], None, source="file"),
            _fp(fields["cover_letter"], None, source="unmapped", needs_input=True, conf=0.0),
        ]
        result = adapter.fill(page, _plan(plans, resume))

        assert result.status == "needs_input"
        assert result.filled_count == 5
        assert result.needs_input_labels == ["Cover letter"]
        assert page.input_value("#fname") == "Mohsin"
        assert page.input_value("#src") == "li"
        assert page.evaluate("() => document.querySelector('#cv').files[0].name") == "resume.pdf"
        assert "red" in page.evaluate("() => document.querySelector('#cover').style.outline")
        # SAFETY sentinel: submit never clicked
        assert page.evaluate("() => document.body.getAttribute('data-submitted')") is None

    def test_low_confidence_not_guessed(self, page: Page):
        page.goto(GENERIC_URL)
        adapter = GenericAdapter()
        fields = {f.name: f for f in adapter.extract_form(page)}
        plans = [_fp(fields["pronouns"], "maybe?", conf=0.2)]
        result = adapter.fill(page, _plan(plans))
        assert result.status == "needs_input"
        assert result.filled_count == 0
        assert page.input_value('[name="pronouns"]') == ""

    def test_formless_page_degrades_to_needs_input(self, page: Page):
        page.goto(NO_FORM_URL)
        adapter = GenericAdapter()
        fields = adapter.extract_form(page)
        assert fields == []
        result = adapter.fill(page, _plan([]))
        assert result.status == "needs_input"
        assert "no application form" in result.notes
