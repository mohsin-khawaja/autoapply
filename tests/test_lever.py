"""LeverAdapter tests: detect priority, extraction, fill (no network, file:// fixtures)."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import pytest

from autoapply.ats import base
from autoapply.ats.generic import GenericAdapter
from autoapply.ats.lever import LeverAdapter

if TYPE_CHECKING:
    from playwright.sync_api import Page

FIXTURES = Path(__file__).parent / "fixtures"
LEVER_URL = FIXTURES.joinpath("lever_apply.html").as_uri()


@pytest.fixture(scope="module")
def page():
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        pg = browser.new_page()
        yield pg
        browser.close()


@pytest.fixture
def lever_page(page: Page) -> Page:
    page.goto(LEVER_URL)
    return page


def _plan(fields: list[base.FieldPlan], resume: Path | None) -> base.FillPlan:
    return base.FillPlan(
        job_id="cccc3333",
        job_url="https://jobs.lever.co/initech/xyz/apply",
        ats=base.ATSKind.LEVER,
        resume_path=resume,
        fields=fields,
    )


def _fp(field: base.FormField, value, source="profile", needs_input=False, conf=0.95):
    return base.FieldPlan(
        field=field, value=value, source=source, confidence=conf, needs_input=needs_input
    )


class TestDetect:
    def test_lever_urls(self):
        assert LeverAdapter.detect("https://jobs.lever.co/initech/xyz/apply")
        assert LeverAdapter.detect("https://jobs.eu.lever.co/acme/123/apply")
        assert LeverAdapter.detect("http://jobs.lever.co/initech/xyz")

    def test_non_lever_urls(self):
        assert not LeverAdapter.detect("https://boards.greenhouse.io/globex/jobs/123")
        assert not LeverAdapter.detect("https://example.com/jobs.lever.co")
        assert not LeverAdapter.detect("https://fake-jobs.lever.co.evil.com/x")
        assert not LeverAdapter.detect("not a url")

    def test_resolve_priority(self):
        assert base.resolve_adapter("https://jobs.lever.co/initech/xyz") is LeverAdapter
        assert base.resolve_adapter("https://random-company.com/careers") is GenericAdapter
        # lever registered before generic
        assert base.ADAPTERS.index(LeverAdapter) < base.ADAPTERS.index(GenericAdapter)
        assert base.ADAPTERS[-1] is GenericAdapter


class TestExtract:
    def test_fields(self, lever_page: Page):
        fields = LeverAdapter().extract_form(lever_page)
        by_name = {f.name: f for f in fields}
        assert "csrf" not in by_name  # hidden skipped
        assert by_name["name"].label == "Full name"
        assert by_name["name"].required
        assert by_name["email"].field_type == "email"
        assert by_name["phone"].field_type == "tel"
        assert by_name["resume"].field_type == "file"
        assert by_name["urls[LinkedIn]"].label == "LinkedIn URL"
        card = by_name["cards[abc123][field0]"]
        assert card.field_type == "textarea"
        assert card.label == "Why do you want to work at Initech?"
        sel = by_name["cards[abc123][field1]"]
        assert sel.field_type == "select"
        assert sel.options == ["Yes", "No"]


class TestFill:
    def test_fill_upload_flag_and_no_submit(self, lever_page: Page, tmp_path: Path):
        resume = tmp_path / "resume.pdf"
        resume.write_bytes(b"%PDF-1.4 fake")
        adapter = LeverAdapter()
        fields = {f.name: f for f in adapter.extract_form(lever_page)}
        plans = [
            _fp(fields["name"], "Mohsin Khawaja"),
            _fp(fields["email"], "mohsinkhawaja10@gmail.com"),
            _fp(fields["org"], "Acme"),
            _fp(fields["resume"], None, source="file"),
            _fp(fields["cards[abc123][field1]"], "Yes"),
            _fp(fields["cards[abc123][field0]"], None, source="unmapped",
                needs_input=True, conf=0.0),
        ]
        result = adapter.fill(lever_page, _plan(plans, resume))

        assert result.status == "needs_input"
        assert result.filled_count == 5
        assert result.needs_input_labels == ["Why do you want to work at Initech?"]
        assert lever_page.input_value('[name="name"]') == "Mohsin Khawaja"
        assert lever_page.input_value('[name="cards[abc123][field1]"]') == "yes"
        assert lever_page.evaluate(
            '() => document.querySelector(\'[name="resume"]\').files[0].name'
        ) == "resume.pdf"
        # needs_input field red-outlined
        assert "red" in lever_page.evaluate(
            '() => document.querySelector(\'[name="cards[abc123][field0]"]\').style.outline'
        )
        # SAFETY sentinel: submit never clicked
        assert lever_page.evaluate("() => document.body.getAttribute('data-submitted')") is None

    def test_all_mapped_is_filled(self, lever_page: Page, tmp_path: Path):
        resume = tmp_path / "r.pdf"
        resume.write_bytes(b"%PDF")
        adapter = LeverAdapter()
        fields = {f.name: f for f in adapter.extract_form(lever_page)}
        plans = [
            _fp(fields["name"], "A B"),
            _fp(fields["email"], "a@b.co"),
            _fp(fields["resume"], None, source="file"),
        ]
        result = adapter.fill(lever_page, _plan(plans, resume))
        assert result.status == "filled"
        assert result.filled_count == 3
        assert lever_page.evaluate("() => document.body.getAttribute('data-submitted')") is None
