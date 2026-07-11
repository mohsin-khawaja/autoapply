"""Tests for the Greenhouse adapter: detect, extract_form, fill.

All browser tests run against local HTML fixtures over file:// URLs — never a
live posting (SPEC.md safety). Each fixture carries a submit-click sentinel
(``window.__submitted``) that must stay False after fill().
"""

from __future__ import annotations

from collections.abc import Iterator
from pathlib import Path

import pytest

from autoapply.ats import base
from autoapply.ats.base import ATSKind, FieldPlan, FillPlan, FormField
from autoapply.ats.greenhouse import GreenhouseAdapter

FIXTURES = Path(__file__).parent / "fixtures"


def fixture_url(name: str) -> str:
    return (FIXTURES / name).resolve().as_uri()


# ---- detect (pure unit) ----


@pytest.mark.parametrize(
    "url",
    [
        "https://boards.greenhouse.io/globex/jobs/123",
        "https://job-boards.greenhouse.io/acme/jobs/456?gh_src=abc",
        "https://boards.greenhouse.io/embed/job_app?for=globex&token=1",
        "http://boards.greenhouse.io/globex",
        "https://acme.greenhouse.io/jobs/789",
    ],
)
def test_detect_greenhouse_urls(url: str) -> None:
    assert GreenhouseAdapter.detect(url)


@pytest.mark.parametrize(
    "url",
    [
        "https://jobs.lever.co/initech/xyz",
        "https://jobs.ashbyhq.com/acme/abc",
        "https://umbrella.wd1.myworkdayjobs.com/careers/job/999",
        "https://greenhouse.io.evil.com/jobs/1",
        "https://example.com/boards.greenhouse.io",
        "not a url",
    ],
)
def test_detect_rejects_non_greenhouse(url: str) -> None:
    assert not GreenhouseAdapter.detect(url)


def test_registered_and_resolvable() -> None:
    assert GreenhouseAdapter in base.ADAPTERS
    assert base.resolve_adapter("https://boards.greenhouse.io/globex/jobs/123") is (
        GreenhouseAdapter
    )
    assert GreenhouseAdapter.kind is ATSKind.GREENHOUSE


# ---- browser fixtures ----


@pytest.fixture(scope="module")
def browser_page() -> Iterator:
    from playwright.sync_api import sync_playwright

    with sync_playwright() as p:
        browser = p.chromium.launch(headless=True)
        page = browser.new_page()
        yield page
        browser.close()


@pytest.fixture
def adapter() -> GreenhouseAdapter:
    return GreenhouseAdapter()


def by_key(fields: list[FormField]) -> dict[str, FormField]:
    return {f.key: f for f in fields}


def make_plan(
    fields: list[FormField],
    values: dict[str, object],
    resume_path: Path | None = None,
    needs_input_keys: set[str] | None = None,
) -> FillPlan:
    needs = needs_input_keys or set()
    plans = []
    for f in fields:
        ni = f.key in needs
        plans.append(
            FieldPlan(
                field=f,
                value=values.get(f.key),
                source="unmapped" if ni else ("file" if f.field_type == "file" else "profile"),
                confidence=0.0 if ni else 0.95,
                needs_input=ni,
            )
        )
    return FillPlan(
        job_id="test-job",
        job_url="https://boards.greenhouse.io/globex/jobs/123",
        ats=ATSKind.GREENHOUSE,
        resume_path=resume_path,
        fields=plans,
    )


# ---- extract_form: classic boards.greenhouse.io ----


def test_extract_classic(browser_page, adapter) -> None:
    browser_page.goto(fixture_url("greenhouse_classic.html"))
    fields = by_key(adapter.extract_form(browser_page))

    assert "first_name" in fields
    fn = fields["first_name"]
    assert fn.field_type == "text"
    assert fn.label == "First Name *"
    assert fn.required
    assert fn.autocomplete == "given-name"
    assert fn.selector == "#first_name"

    assert fields["email"].field_type == "email"
    assert fields["email"].required
    assert fields["phone"].field_type == "tel"
    assert not fields["phone"].required
    assert fields["resume"].field_type == "file"
    assert fields["resume"].required

    ta = fields["job_application_answers_attributes_0_text_value"]
    assert ta.field_type == "textarea"
    assert "Globex" in ta.label

    sel = fields["job_application_answers_attributes_1_boolean_value"]
    assert sel.field_type == "select"
    assert sel.options == ["Yes", "No"]
    assert sel.required

    radio = fields["job_application[referral]"]
    assert radio.field_type == "radio"
    assert radio.options == ["LinkedIn", "Friend", "Other"]
    assert radio.label == "How did you hear about this job?"

    # hidden and submit inputs never surface
    assert not any(f.name == "security_code" for f in fields.values())
    assert "submit_app" not in fields


# ---- extract_form: job-boards.greenhouse.io (React) ----


def test_extract_job_boards(browser_page, adapter) -> None:
    browser_page.goto(fixture_url("greenhouse_job_boards.html"))
    fields = by_key(adapter.extract_form(browser_page))

    combo = fields["education_degree"]
    assert combo.field_type == "combobox"
    assert combo.required
    assert combo.options == ["Bachelor's Degree", "Master's Degree", "PhD"]
    assert combo.label == "Highest degree earned *"

    assert fields["first_name"].required
    assert not fields["question_linkedin"].required
    assert fields["question_cover"].field_type == "textarea"
    assert fields["resume"].field_type == "file"


# ---- fill: classic ----


def test_fill_classic(browser_page, adapter, tmp_path) -> None:
    browser_page.goto(fixture_url("greenhouse_classic.html"))
    fields = adapter.extract_form(browser_page)
    resume = tmp_path / "resume.pdf"
    resume.write_bytes(b"%PDF-1.4 test")

    values = {
        "first_name": "Ada",
        "last_name": "Lovelace",
        "email": "ada@example.com",
        "phone": "555-0100",
        "job_application_answers_attributes_0_text_value": "I love compilers.",
        "job_application_answers_attributes_1_boolean_value": "Yes",
        "job_application[referral]": "LinkedIn",
        "gender": "Decline To Self Identify",
    }
    plan = make_plan(fields, values, resume_path=resume)
    result = adapter.fill(browser_page, plan)

    assert result.status == "filled"
    assert result.filled_count == 9  # 8 values + resume
    assert result.needs_input_labels == []
    assert result.error is None

    assert browser_page.input_value("#first_name") == "Ada"
    assert browser_page.input_value("#email") == "ada@example.com"
    assert (
        browser_page.input_value("#job_application_answers_attributes_0_text_value")
        == "I love compilers."
    )
    # select filled by visible label -> value "1"
    assert (
        browser_page.input_value("#job_application_answers_attributes_1_boolean_value") == "1"
    )
    assert browser_page.locator(
        'input[type="radio"][name="job_application[referral]"][value="linkedin"]'
    ).is_checked()
    # resume uploaded
    assert browser_page.evaluate(
        "document.querySelector('#resume').files.length"
    ) == 1
    assert (
        browser_page.evaluate("document.querySelector('#resume').files[0].name")
        == "resume.pdf"
    )
    # SAFETY: submit sentinel untouched
    assert browser_page.evaluate("window.__submitted") is False


def test_fill_flags_needs_input(browser_page, adapter, tmp_path) -> None:
    browser_page.goto(fixture_url("greenhouse_classic.html"))
    fields = adapter.extract_form(browser_page)
    resume = tmp_path / "resume.pdf"
    resume.write_bytes(b"%PDF-1.4")

    values = {"first_name": "Ada", "last_name": "Lovelace", "phone": "555-0100"}
    needs = {
        "email",
        "job_application_answers_attributes_0_text_value",
        "job_application_answers_attributes_1_boolean_value",
        "job_application[referral]",
        "gender",
    }
    plan = make_plan(fields, values, resume_path=resume, needs_input_keys=needs)
    result = adapter.fill(browser_page, plan)

    assert result.status == "needs_input"  # required email + questions unresolved
    assert "Email *" in result.needs_input_labels
    assert len(result.needs_input_labels) == len(needs)
    # flagged field got the red outline
    assert (
        browser_page.evaluate("document.querySelector('#email').style.outline")
        in ("3px solid red", "red solid 3px")
    )
    # unflagged filled field untouched by outline
    assert browser_page.evaluate("document.querySelector('#first_name').style.outline") == ""
    assert browser_page.evaluate("window.__submitted") is False


def test_fill_optional_unresolved_still_filled(browser_page, adapter, tmp_path) -> None:
    browser_page.goto(fixture_url("greenhouse_classic.html"))
    fields = adapter.extract_form(browser_page)
    resume = tmp_path / "resume.pdf"
    resume.write_bytes(b"%PDF-1.4")

    values = {
        "first_name": "Ada",
        "last_name": "Lovelace",
        "email": "ada@example.com",
        "phone": "555-0100",
        "job_application_answers_attributes_0_text_value": "Hi.",
        "job_application_answers_attributes_1_boolean_value": "Yes",
    }
    # gender + referral optional -> flagged but status stays "filled"
    plan = make_plan(
        fields, values, resume_path=resume, needs_input_keys={"gender", "job_application[referral]"}
    )
    result = adapter.fill(browser_page, plan)
    assert result.status == "filled"
    assert len(result.needs_input_labels) == 2


# ---- fill: job-boards React combobox ----


def test_fill_job_boards_combobox(browser_page, adapter, tmp_path) -> None:
    browser_page.goto(fixture_url("greenhouse_job_boards.html"))
    fields = adapter.extract_form(browser_page)
    resume = tmp_path / "cv.pdf"
    resume.write_bytes(b"%PDF-1.4")

    values = {
        "first_name": "Grace",
        "last_name": "Hopper",
        "email": "grace@example.com",
        "phone": "555-0101",
        "education_degree": "Master's Degree",
        "question_linkedin": "https://linkedin.com/in/grace",
        "question_cover": "COBOL forever.",
    }
    plan = make_plan(fields, values, resume_path=resume)
    result = adapter.fill(browser_page, plan)

    assert result.status == "filled"
    assert result.error is None
    assert browser_page.input_value("#education_degree") == "Master's Degree"
    assert (
        browser_page.evaluate("document.getElementById('education_degree').dataset.selected")
        == "Master's Degree"
    )
    assert browser_page.evaluate(
        "document.querySelector('#resume').files[0].name"
    ) == "cv.pdf"
    assert browser_page.evaluate("window.__submitted") is False


def test_fill_failure_reports_failed(browser_page, adapter) -> None:
    browser_page.goto(fixture_url("greenhouse_classic.html"))
    bogus = FormField(
        key="ghost",
        field_type="text",
        label="Ghost",
        selector="#does_not_exist",
        required=False,
    )
    plan = make_plan([bogus], {"ghost": "boo"})
    browser_page.set_default_timeout(1500)
    try:
        result = adapter.fill(browser_page, plan)
    finally:
        browser_page.set_default_timeout(30000)
    assert result.status == "failed"
    assert result.error
