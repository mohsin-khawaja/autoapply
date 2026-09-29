"""Ashby adapter tests — local HTML fixtures only, no live network (SPEC.md §10)."""

from __future__ import annotations

from pathlib import Path

import pytest

from autoapply.ats import base
from autoapply.ats.ashby import AshbyAdapter
from autoapply.ats.base import ATSKind, FieldPlan, FillPlan, FormField

FIXTURES = Path(__file__).parent / "fixtures"


# ---- pure unit tests: detect / registration ----


@pytest.mark.parametrize(
    "url",
    [
        "https://jobs.ashbyhq.com/acme/abc-123",
        "https://jobs.ashbyhq.com/openai/550e8400/application",
        "http://jobs.ashbyhq.com/x",
    ],
)
def test_detect_matches_ashby_urls(url: str) -> None:
    assert AshbyAdapter.detect(url)


@pytest.mark.parametrize(
    "url",
    [
        "https://boards.greenhouse.io/globex/jobs/123",
        "https://jobs.lever.co/initech/xyz",
        "https://ashbyhq.com/customers",
        "https://evil.com/jobs.ashbyhq.com/fake",
        "https://jobs.ashbyhq.com.evil.com/fake",
        "not a url",
        "",
    ],
)
def test_detect_rejects_non_ashby_urls(url: str) -> None:
    assert not AshbyAdapter.detect(url)


def test_registered_and_resolvable() -> None:
    assert AshbyAdapter in base.ADAPTERS
    assert base.resolve_adapter("https://jobs.ashbyhq.com/acme/abc") is AshbyAdapter
    assert AshbyAdapter.kind is ATSKind.ASHBY


# ---- browser-driven tests over file:// fixtures ----


@pytest.fixture(scope="module")
def browser_page():
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()
        yield page
        browser.close()


def _load(page, fixture: str) -> None:
    page.goto((FIXTURES / fixture).as_uri())


def _by_key(fields: list[FormField]) -> dict[str, FormField]:
    return {f.key: f for f in fields}


def _plan(fields: list[FieldPlan], resume: Path | None = None) -> FillPlan:
    return FillPlan(
        job_id="aaaa1111",
        job_url="https://jobs.ashbyhq.com/acme/abc",
        ats=ATSKind.ASHBY,
        resume_path=resume,
        fields=fields,
    )


def _fp(field: FormField, value, *, needs_input: bool = False) -> FieldPlan:
    return FieldPlan(
        field=field,
        value=value,
        source="unmapped" if needs_input else "profile",
        confidence=0.0 if needs_input else 0.95,
        needs_input=needs_input,
    )


class TestExtractMultiStep:
    def test_types_labels_required_groups(self, browser_page) -> None:
        _load(browser_page, "ashby_multistep.html")
        fields = _by_key(AshbyAdapter().extract_form(browser_page))

        name = fields["_systemfield_name"]
        assert name.field_type == "text"
        assert name.label == "Name"
        assert name.required
        assert name.group == "Your information"
        assert name.autocomplete == "name"

        assert fields["_systemfield_email"].field_type == "email"
        assert fields["phone"].field_type == "tel"
        assert not fields["phone"].required

        resume = fields["_systemfield_resume"]
        assert resume.field_type == "file"
        assert resume.required
        assert resume.group == "Your information"

        auth = fields["work_auth"]
        assert auth.field_type == "radio"
        assert auth.label.startswith("Are you authorized")
        assert auth.options == ["Yes", "No"]
        assert auth.required
        assert auth.group == "Additional questions"

        sponsor = fields["sponsorship"]
        assert sponsor.field_type == "radio"
        assert not sponsor.required

        assert fields["why"].field_type == "textarea"
        assert fields["why"].group == "Additional questions"
        assert fields["relocate"].field_type == "checkbox"
        assert fields["relocate"].label == "Willing to relocate"

        # Two distinct steps modeled via group.
        assert {f.group for f in fields.values()} == {
            "Your information",
            "Additional questions",
        }

    def test_no_submit_button_extracted(self, browser_page) -> None:
        _load(browser_page, "ashby_multistep.html")
        fields = AshbyAdapter().extract_form(browser_page)
        assert all("submit" not in (f.name or "").lower() for f in fields)
        assert all(f.attrs.get("type") != "submit" for f in fields)


class TestFillMultiStep:
    def test_fill_uploads_flags_and_never_submits(self, browser_page, tmp_path) -> None:
        resume = tmp_path / "resume.pdf"
        resume.write_bytes(b"%PDF-1.4 test")

        _load(browser_page, "ashby_multistep.html")
        browser_page.click("#next-btn")  # reveal step 2 (progressive disclosure)
        adapter = AshbyAdapter()
        fields = _by_key(adapter.extract_form(browser_page))

        plan = _plan(
            [
                _fp(fields["_systemfield_name"], "Mohsin Khawaja"),
                _fp(fields["_systemfield_email"], "mohsinkhawaja10@gmail.com"),
                _fp(fields["phone"], "555-0100"),
                _fp(fields["_systemfield_resume"], None),  # falls back to resume_path
                _fp(fields["work_auth"], "Yes"),
                _fp(fields["sponsorship"], "No"),
                _fp(fields["relocate"], "yes"),
                _fp(fields["why"], None, needs_input=True),  # human writes this
            ],
            resume=resume,
        )
        result = adapter.fill(browser_page, plan)

        assert result.status == "needs_input"
        assert result.filled_count == 7
        assert result.needs_input_labels == ["Why do you want to work at Acme?"]

        assert browser_page.input_value("#_systemfield_name") == "Mohsin Khawaja"
        assert browser_page.input_value("#phone") == "555-0100"
        assert browser_page.is_checked('input[name="work_auth"][value="yes"]')
        assert browser_page.is_checked('input[name="sponsorship"][value="no"]')
        assert browser_page.is_checked('input[name="relocate"]')
        uploaded = browser_page.evaluate(
            "document.getElementById('resume-input').files[0]?.name"
        )
        assert uploaded == "resume.pdf"

        # needs_input field is red-outlined, untouched
        assert browser_page.input_value("#why") == ""
        outline = browser_page.evaluate("document.getElementById('why').style.outline")
        assert "red" in outline

        # SAFETY: the submit sentinel must never fire.
        assert browser_page.evaluate("window.__submitted") is False

    def test_fill_all_satisfied_reports_filled(self, browser_page, tmp_path) -> None:
        resume = tmp_path / "resume.pdf"
        resume.write_bytes(b"%PDF-1.4 test")
        _load(browser_page, "ashby_multistep.html")
        browser_page.click("#next-btn")
        adapter = AshbyAdapter()
        fields = _by_key(adapter.extract_form(browser_page))

        plan = _plan(
            [
                _fp(fields["_systemfield_name"], "Mohsin Khawaja"),
                _fp(fields["_systemfield_email"], "mohsinkhawaja10@gmail.com"),
                _fp(fields["_systemfield_resume"], None),
                _fp(fields["work_auth"], "Yes"),
            ],
            resume=resume,
        )
        result = adapter.fill(browser_page, plan)
        assert result.status == "filled"
        assert result.filled_count == 4
        assert result.needs_input_labels == []
        assert browser_page.evaluate("window.__submitted") is False

    def test_missing_required_value_flags_needs_input(self, browser_page) -> None:
        _load(browser_page, "ashby_multistep.html")
        adapter = AshbyAdapter()
        fields = _by_key(adapter.extract_form(browser_page))

        # Required resume, no value and no plan.resume_path -> needs_input.
        plan = _plan([_fp(fields["_systemfield_resume"], None)], resume=None)
        result = adapter.fill(browser_page, plan)
        assert result.status == "needs_input"
        assert result.needs_input_labels == ["Resume"]
        assert browser_page.evaluate("window.__submitted") is False

    def test_unmatchable_radio_value_flags(self, browser_page) -> None:
        _load(browser_page, "ashby_multistep.html")
        browser_page.click("#next-btn")
        adapter = AshbyAdapter()
        fields = _by_key(adapter.extract_form(browser_page))

        plan = _plan([_fp(fields["work_auth"], "Maybe")])
        result = adapter.fill(browser_page, plan)
        assert result.status == "needs_input"
        assert result.needs_input_labels[0].startswith("Are you authorized")


class TestCombobox:
    def test_extract_peeks_rendered_options(self, browser_page) -> None:
        _load(browser_page, "ashby_combobox.html")
        fields = _by_key(AshbyAdapter().extract_form(browser_page))

        loc = fields["location"]
        assert loc.field_type == "combobox"
        assert loc.label == "Preferred location"
        assert loc.required
        assert loc.group == "Application"
        assert loc.options == [
            "San Francisco, CA",
            "New York, NY",
            "Austin, TX",
            "Remote (US)",
        ]
        assert loc.attrs.get("aria-haspopup") == "listbox"
        # Peeking must close the listbox again.
        assert browser_page.is_hidden("#location-listbox")

    def test_fill_selects_option_by_typing(self, browser_page) -> None:
        _load(browser_page, "ashby_combobox.html")
        adapter = AshbyAdapter()
        fields = _by_key(adapter.extract_form(browser_page))

        plan = _plan(
            [
                _fp(fields["location"], "San Francisco, CA"),
                _fp(fields["fullname"], "Mohsin Khawaja"),
            ]
        )
        result = adapter.fill(browser_page, plan)
        assert result.status == "filled"
        assert result.filled_count == 2
        assert browser_page.input_value("#location") == "San Francisco, CA"
        assert browser_page.is_hidden("#location-listbox")
        assert browser_page.evaluate("window.__submitted") is False

    def test_fill_combobox_no_match_flags(self, browser_page) -> None:
        _load(browser_page, "ashby_combobox.html")
        adapter = AshbyAdapter()
        fields = _by_key(adapter.extract_form(browser_page))

        plan = _plan([_fp(fields["location"], "Atlantis")])
        result = adapter.fill(browser_page, plan)
        assert result.status == "needs_input"
        assert result.needs_input_labels == ["Preferred location"]
        assert browser_page.evaluate("window.__submitted") is False
