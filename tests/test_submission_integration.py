import pytest
from playwright.sync_api import sync_playwright

from autoapply.ats.base import ATSKind, FieldPlan, FillPlan, FormField
from autoapply.submission import submit_application


@pytest.fixture(scope="module")
def page():
    with sync_playwright() as p:
        browser = p.chromium.launch()
        page = browser.new_page()
        yield page
        browser.close()


def plan(kind=ATSKind.LEVER):
    return FillPlan(job_id="fixture", job_url="about:blank", ats=kind, fields=[], resume_path=None)


def form(page, button_type="button", extra="", outcome="Thank you for applying!"):
    page.goto("about:blank")
    page.set_content(
        '<form><button type="submit" hidden>Hidden</button>'
        + extra
        + f'<button {button_type} onclick="event.preventDefault();'
        f"document.body.innerHTML='<p>{outcome}</p>'\">Submit application</button></form>"
    )


@pytest.mark.parametrize("kind,attribute", [(ATSKind.LEVER, 'type="button"'), (ATSKind.ASHBY, "")])
def test_visible_submit_handles_missing_or_non_submit_type(page, tmp_path, kind, attribute):
    form(page, attribute)
    screenshot = tmp_path / "confirmation.png"
    result = submit_application(page, plan(kind), {kind}, screenshot, timeout_ms=100)
    assert result.status == "submitted"
    assert result.confirmation_detected
    assert screenshot.exists()


def test_allowlist_blocks_click(page):
    form(page)
    assert submit_application(page, plan(), set()).status == "needs_input"
    assert page.get_by_role("button", name="Submit application").count() == 1


def test_required_unresolved_blocks_click(page):
    form(page)
    pending = plan()
    pending.fields.append(
        FieldPlan(
            field=FormField(
                key="missing",
                label="Missing",
                required=True,
                field_type="text",
                selector="#missing",
            ),
            value=None,
            source="unmapped",
            needs_input=True,
            confidence=0.0,
        )
    )
    assert submit_application(page, pending, {ATSKind.LEVER}).status == "needs_input"
    assert page.get_by_role("button", name="Submit application").count() == 1


def test_native_invalid_required_control_blocks_click(page):
    form(page, extra='<input required name="email">')
    assert submit_application(page, plan(), {ATSKind.LEVER}).status == "needs_input"


def test_ambiguous_visible_buttons_never_click(page):
    form(page, extra='<button type="button">Submit application</button>')
    assert submit_application(page, plan(), {ATSKind.LEVER}).status == "needs_input"


def test_hidden_confirmation_is_not_proof(page):
    page.goto("about:blank")
    page.set_content('<p hidden>Thank you for applying!</p><button type="button">Submit</button>')
    result = submit_application(page, plan(), {ATSKind.LEVER}, timeout_ms=10)
    assert result.status == "filled"
    assert not result.confirmation_detected


def test_form_disappearing_alone_is_not_success(page):
    form(page, outcome="Something went wrong")
    result = submit_application(page, plan(), {ATSKind.LEVER}, timeout_ms=10)
    assert result.status == "filled"


def test_validation_error_prevents_success(page):
    page.goto("about:blank")
    page.set_content("""<button type="button" onclick="document.body.innerHTML=
        '<input aria-invalid=true><p>Thank you for applying!</p>'">Submit</button>""")
    result = submit_application(page, plan(), {ATSKind.LEVER}, timeout_ms=10)
    assert result.status == "needs_input"
