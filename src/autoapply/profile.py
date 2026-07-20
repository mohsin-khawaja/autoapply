"""Profile schema and loader — the single source of truth for applicant facts.

FROZEN shape: the mapper (:mod:`autoapply.filling.mapper`) and every ATS adapter
import :class:`Profile` and read fields by attribute. Adding optional fields is
backward compatible; renaming/removing is an interface change (SPEC.md §9).

Never invent facts. Empty strings mean "not provided" — the mapper must flag the
field for manual entry rather than fabricate a value.
"""

from __future__ import annotations

from pathlib import Path

import yaml
from pydantic import BaseModel, ConfigDict, Field


class Location(BaseModel):
    model_config = ConfigDict(extra="forbid")
    city: str = ""
    state: str = ""
    country: str = ""


class Links(BaseModel):
    model_config = ConfigDict(extra="forbid")
    linkedin: str = ""
    website: str = ""
    github: str = ""


class Identity(BaseModel):
    @property
    def full_name(self) -> str:
        return f"{self.first_name} {self.last_name}".strip()

    model_config = ConfigDict(extra="forbid")
    first_name: str
    last_name: str
    email: str
    phone: str = ""
    location: Location = Field(default_factory=Location)
    links: Links = Field(default_factory=Links)


class Education(BaseModel):
    model_config = ConfigDict(extra="forbid")
    school: str
    degree: str = ""
    major: str = ""
    minor: str = ""
    # Dates are free-form (YAML may parse ``2021-08`` as a date); keep as str.
    start: str = ""
    end: str = ""
    gpa: str = ""

    # Derived date parts for ATS month/year sub-fields ("YYYY-MM" -> parts).
    @property
    def start_year(self) -> str:
        return self.start.split("-")[0] if self.start else ""

    @property
    def start_month(self) -> str:
        return _month_name(self.start)

    @property
    def end_year(self) -> str:
        return self.end.split("-")[0] if self.end else ""

    @property
    def end_month(self) -> str:
        return _month_name(self.end)


_MONTHS = (
    "January", "February", "March", "April", "May", "June",
    "July", "August", "September", "October", "November", "December",
)


def _month_name(ym: str) -> str:
    """"2021-08" -> "August"; empty/uparseable -> ""."""
    parts = ym.split("-")
    if len(parts) < 2 or not parts[1].isdigit():
        return ""
    m = int(parts[1])
    return _MONTHS[m - 1] if 1 <= m <= 12 else ""


class Experience(BaseModel):
    model_config = ConfigDict(extra="forbid")
    company: str
    title: str = ""
    location: str = ""
    start: str = ""
    end: str = ""
    summary: str = ""


class Project(BaseModel):
    model_config = ConfigDict(extra="forbid")
    name: str
    blurb: str = ""


class Documents(BaseModel):
    model_config = ConfigDict(extra="forbid")
    resume: str = ""

    def resume_path(self, base: Path) -> Path | None:
        """Resolve the resume path relative to ``base`` (repo root). None if unset."""
        if not self.resume:
            return None
        p = Path(self.resume)
        return p if p.is_absolute() else (base / p).resolve()


class EEO(BaseModel):
    """Voluntary self-identification. Defaults decline everything (SPEC.md §4)."""

    model_config = ConfigDict(extra="forbid")
    gender: str = "decline"
    race: str = "decline"
    veteran: str = "I am not a protected veteran"
    disability: str = "I do not want to answer"


class Answers(BaseModel):
    """Canonical answers to standard screener questions."""

    model_config = ConfigDict(extra="allow")  # tolerate extra screener keys added to yaml
    work_authorization_us: str = "Yes"
    require_sponsorship: str = "No"
    require_sponsorship_future: str = "No"
    over_18: str = "Yes"
    willing_to_relocate: str = "Yes"
    remote_ok: str = "Yes"
    start_date: str = "Immediately"
    salary_expectation: str = ""
    how_did_you_hear: str = ""
    previously_employed_here: str = "No"
    criminal_record_disclosures: str = "decline_unless_required"
    security_clearance: str = "No"
    eeo: EEO = Field(default_factory=EEO)


class Profile(BaseModel):
    """Complete applicant profile loaded from ``profile.yaml``."""

    model_config = ConfigDict(extra="forbid")
    identity: Identity
    education: list[Education] = Field(default_factory=list)
    experience: list[Experience] = Field(default_factory=list)
    projects: list[Project] = Field(default_factory=list)
    skills: list[str] = Field(default_factory=list)
    documents: Documents = Field(default_factory=Documents)
    answers: Answers = Field(default_factory=Answers)


def load_profile(path: str | Path) -> Profile:
    """Load and validate ``profile.yaml``. Raises if the file is missing or invalid."""
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"profile not found: {p}")
    raw = yaml.safe_load(p.read_text()) or {}
    return Profile.model_validate(_stringify_dates(raw))


def _stringify_dates(obj: object) -> object:
    """Coerce YAML-parsed dates back to strings so the schema stays str-typed.

    ``2021-08`` in YAML may become a ``datetime.date``; keep profile dates as the
    original free-form strings the forms expect.
    """
    import datetime as _dt

    if isinstance(obj, dict):
        return {k: _stringify_dates(v) for k, v in obj.items()}
    if isinstance(obj, list):
        return [_stringify_dates(v) for v in obj]
    if isinstance(obj, _dt.date):
        return obj.isoformat()
    return obj
