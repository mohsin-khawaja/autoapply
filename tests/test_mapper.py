"""Mapper: deterministic mapping, fuzzy options, needs_input flagging (SPEC.md §5)."""

from __future__ import annotations

from pathlib import Path

from autoapply.ats.base import ATSKind, FormField
from autoapply.filling.mapper import build_plan
from autoapply.profile import load_profile

REPO = Path(__file__).resolve().parents[1]


class _Answers:
    def __init__(self, data=None):
        self.data = data or {}

    def get(self, qh, company):
        return self.data.get((qh, company))


class _Job:
    id = "j1"
    company_name = "Acme"
    title = "ML Engineer"


def _plan(fields, answers=None, resume=Path("/tmp/resume.pdf"), threshold=82):
    profile = load_profile(REPO / "profile.yaml")
    return build_plan(
        fields, profile, answers or _Answers(), _Job(),
        job_url="https://x", ats=ATSKind.GENERIC, resume_path=resume,
        fuzzy_threshold=threshold,
    )


def test_maps_basic_identity_fields():
    fields = [
        FormField(key="f1", field_type="text", label="First Name", selector="#a"),
        FormField(key="f2", field_type="email", label="Email", selector="#b"),
        FormField(key="f3", field_type="tel", label="Phone Number", selector="#c"),
    ]
    plan = _plan(fields)
    by_key = {fp.field.key: fp for fp in plan.fields}
    assert by_key["f1"].value == "Mohsin"
    assert by_key["f2"].value == "mkhawaja@ucsd.edu"
    assert by_key["f3"].value == "510-949-7141"
    assert not plan.unresolved


def test_file_input_gets_resume():
    fields = [FormField(key="r", field_type="file", label="Resume", selector="#r")]
    plan = _plan(fields)
    assert plan.fields[0].value == Path("/tmp/resume.pdf")
    assert plan.fields[0].source == "file"


def test_missing_profile_value_flags_needs_input():
    # GPA maps to education.0.gpa, which is empty in profile.yaml -> needs_input.
    fields = [FormField(key="g", field_type="text", label="GPA", selector="#g")]
    plan = _plan(fields)
    assert plan.fields[0].needs_input is True
    assert plan.fields[0].source == "unmapped"


def test_select_fuzzy_match_picks_option():
    fields = [
        FormField(
            key="auth", field_type="select", label="Are you authorized to work in the US?",
            selector="#auth", options=["Yes", "No"],
        )
    ]
    plan = _plan(fields)
    assert plan.fields[0].value == "Yes"
    assert plan.fields[0].source == "profile"


def test_select_below_threshold_needs_input():
    fields = [
        FormField(
            key="auth", field_type="select", label="Work authorization",
            selector="#auth", options=["Absolutely not relevant option"],
        )
    ]
    plan = _plan(fields, threshold=95)
    assert plan.fields[0].needs_input is True


def test_freetext_uses_cached_answer():
    from autoapply.filling.mapper import question_hash

    q = "Why do you want to work here?"
    ans = _Answers({(question_hash(q), "Acme"): "I love this."})
    fields = [FormField(key="w", field_type="textarea", label=q, selector="#w")]
    plan = _plan(fields, answers=ans)
    assert plan.fields[0].value == "I love this."
    assert plan.fields[0].source == "answer_cache"


def test_freetext_without_cache_marks_llm():
    fields = [
        FormField(key="w", field_type="textarea", label="Tell us about a project", selector="#w")
    ]
    plan = _plan(fields)
    assert plan.fields[0].source == "llm"
    assert plan.fields[0].needs_input is True


def test_bare_yes_matches_spelled_out_option():
    """'Yes' must select 'Yes, I am legally authorized...' — fuzzy scores it 60."""
    opts = [
        "Yes, I am legally authorized to work in the United States for any employer",
        "No, I require sponsorship to work in the United States",
    ]
    plan = _plan([
        FormField(
            key="auth", field_type="select",
            label="Are you legally authorized to work in the United States?",
            selector="#auth", options=opts,
        )
    ])
    fp = plan.fields[0]
    assert not fp.needs_input, "a mapped Yes/No answer must not block the application"
    assert str(fp.value).startswith("Yes,")


def test_yes_no_matching_never_picks_a_lookalike():
    """'No' must not select 'Not applicable' or 'None'; non-yes/no values opt out."""
    from autoapply.filling.mapper import _yes_no_option

    assert _yes_no_option("No", ["Not applicable", "None of the above"]) is None
    assert _yes_no_option("Yes", ["Yesterday"]) is None
    assert _yes_no_option("B.S.", ["Yes", "No"]) is None


def test_bare_name_label_maps_without_stealing_qualified_labels():
    """A lone "Name" is the full name, but must not claim qualified variants."""
    from autoapply.filling.synonyms import match_key

    def k(label):
        return match_key(label=label, name="", field_id="", aria="", autocomplete="")

    assert k("Name") == "identity.full_name"
    assert k("Full Legal Name") == "identity.full_name"
    # Qualified labels keep their own mapping — the bare pattern runs last.
    assert k("First Name") == "identity.first_name"
    assert k("Last Name") == "identity.last_name"
    assert k("School Name") == "education.0.school"
    assert k("Name of University") == "education.0.school"


def test_bare_name_fills_from_profile():
    """The mapped full name resolves to a real value, not needs_input."""
    plan = _plan([FormField(key="n", field_type="text", label="Name", selector="#n")])
    fp = plan.fields[0]
    assert not fp.needs_input
    assert fp.value and " " in str(fp.value)  # "First Last"


def test_captcha_fields_are_never_answered():
    """CAPTCHA plumbing must flag for a human, never be filled or LLM-answered."""
    from autoapply.filling.mapper import is_bot_infra_field

    for key in ("g-recaptcha-response", "h-captcha-response", "cf-turnstile-response"):
        assert is_bot_infra_field(
            FormField(key=key, field_type="text", label="", selector=f"#{key}")
        )
    # A normal question is not infrastructure.
    assert not is_bot_infra_field(
        FormField(key="q1", field_type="textarea", label="Why us?", selector="#q1")
    )

    plan = _plan([
        FormField(key="g-recaptcha-response", field_type="text", label="", selector="#g")
    ])
    fp = plan.fields[0]
    assert fp.needs_input and fp.value is None
    assert fp.source == "unmapped"  # never routed to the LLM


def test_gpa_and_test_scores_are_never_estimated():
    """Verifiable credentials not in the profile must stay needs_input, not LLM."""
    from autoapply.filling.mapper import is_unfabricable_fact

    for label in ("GPA (Undergraduate)", "SAT Score", "ACT Score", "GRE Score"):
        f = FormField(key="k", field_type="text", label=label, selector="#k")
        assert is_unfabricable_fact(f), f"{label} must be guarded"
        plan = _plan([f])
        assert plan.fields[0].needs_input
        assert plan.fields[0].source == "unmapped"  # never routed to the LLM


def test_unmapped_freetext_and_choices_route_to_llm():
    """A free-text question and an unmapped dropdown both defer to the LLM."""
    text = FormField(key="q", field_type="textarea", label="Why do you want this?", selector="#q")
    choice = FormField(
        key="c", field_type="select", label="Preferred team?", selector="#c",
        options=["Platform", "Product", "Infra"],
    )
    plan = _plan([text, choice])
    assert all(fp.source == "llm" for fp in plan.fields)


def test_gpa_question_never_borrows_the_home_state():
    """"Please state the GPA" must not fill "CA" from identity.location.state.

    Seen live: the field scored profile/100%, counted as resolved, and would
    have passed the submit gate with a wrong answer on a real application.
    """
    f = FormField(
        key="g", field_type="textarea",
        label=(
            "What was your cumulative GPA upon graduation? "
            "Please state the GPA and degree obtained."
        ),
        selector="#g",
    )
    plan = _plan([f])
    fp = plan.fields[0]
    assert fp.needs_input, "an unanswerable credential must block, not guess"
    assert fp.value is None
    assert fp.source == "unmapped"


def test_state_as_a_verb_does_not_map_to_location():
    from autoapply.filling.synonyms import match_key

    def k(label):
        return match_key(label=label, name="", field_id="", aria="", autocomplete="")

    assert k("Please state your desired salary") != "identity.location.state"
    assert k("State the reason for leaving") != "identity.location.state"
    # ...while a real location field still maps.
    assert k("State") == "identity.location.state"
    assert k("State / Province") == "identity.location.state"


def test_optional_unmappable_checkboxes_do_not_block_submission():
    """A 26-language checkbox matrix must not count as 26 blockers.

    Seen live: one form contributed "Croatian", "Dutch", "Other", "Not
    Applicable" and 20+ more as unresolved fields across 62 applications,
    holding the submit gate shut on forms that were otherwise complete.
    Leaving an optional checkbox unticked IS the answer.
    """
    boxes = [
        FormField(key=f"lang{i}", field_type="checkbox", label=lang, selector=f"#l{i}")
        for i, lang in enumerate(["Croatian", "Dutch", "Italian", "Other", "Not Applicable"])
    ]
    plan = _plan(boxes)
    assert plan.unresolved == [], [f.field.label for f in plan.unresolved]
    assert all(fp.value is None for fp in plan.fields)  # nothing invented


def test_a_required_checkbox_still_blocks():
    """Consent boxes are required and must still reach a human."""
    f = FormField(
        key="c", field_type="checkbox", label="I consent to the privacy policy",
        selector="#c", required=True,
    )
    assert _plan([f]).unresolved


def test_mapped_checkbox_is_still_filled_from_profile():
    """The skip only applies to fields with no profile mapping."""
    f = FormField(
        key="w", field_type="checkbox",
        label="Are you legally authorized to work in the United States?", selector="#w",
    )
    fp = _plan([f]).fields[0]
    assert fp.source != "optional_skip"
    assert fp.value == "Yes"


def test_bare_location_maps_to_city():
    fp = _plan([FormField(key="l", field_type="text", label="Location", selector="#l")]).fields[0]
    assert fp.value == "Berkeley"
    assert not fp.needs_input


def test_split_radio_options_do_not_each_become_a_blocker():
    """Some forms emit each radio choice as its own field labelled "YES"/"NO".

    Seen live on a Greenhouse form: two unmapped fields, two blockers, and the
    application stalled at needs_input despite everything else being filled.
    """
    fields = [
        FormField(key="y", field_type="radio", label="YES", selector="#y"),
        FormField(key="n", field_type="radio", label="NO", selector="#n"),
    ]
    plan = _plan(fields)
    assert plan.unresolved == [], [f.field.label for f in plan.unresolved]


def test_a_real_radio_group_with_options_still_maps():
    """A radio group that carries its question and options is unaffected."""
    f = FormField(
        key="a", field_type="radio",
        label="Are you legally authorized to work in the United States?",
        selector="#a", options=["Yes", "No"],
    )
    fp = _plan([f]).fields[0]
    assert fp.value == "Yes" and not fp.needs_input


def test_required_split_radio_still_blocks():
    f = FormField(key="y", field_type="radio", label="YES", selector="#y", required=True)
    assert _plan([f]).unresolved
