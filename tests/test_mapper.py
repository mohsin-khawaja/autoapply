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
    fields = [FormField(key="g", field_type="text", label="GitHub URL", selector="#g")]
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
