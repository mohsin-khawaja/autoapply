"""Synonym table: form-field labels/attrs -> dotted profile keys (SPEC.md §5.1).

Deterministic mapping is tried before any LLM. Each profile key maps to a list
of lowercase regex-ish substring patterns matched against a field's label,
name, id, aria-label, and autocomplete. First key with a matching pattern wins;
order below is priority order (most specific first).

Adding synonyms is safe and encouraged as adapters meet new forms. The dotted
keys must resolve against :class:`autoapply.profile.Profile` — see
:func:`autoapply.filling.mapper.resolve_profile_value`.
"""

from __future__ import annotations

import re

# Ordered: more specific keys first so e.g. "linkedin" beats a generic "url".
SYNONYMS: dict[str, list[str]] = {
    "identity.full_name": ["full legal name", "full name", "legal name"],
    "identity.first_name": [
        "first name", "given name", "first_name", "given-name", "fname", "preferred name",
    ],
    "identity.last_name": [
        "last name", "family name", "surname", "last_name", "family-name", "lname",
    ],
    "identity.email": ["email", "e-mail", "email address"],
    "identity.phone": ["phone", "mobile", "telephone", "phone number", "tel"],
    "identity.links.linkedin": ["linkedin"],
    "identity.links.github": ["github"],
    "identity.links.website": ["website", "portfolio", "personal site", "personal website"],
    "identity.location.city": [
        "city", "current city", "where are you currently located", "where are you located",
        "current location", "location (city)", "your location", "where are you based",
    ],
    "identity.location.state": ["state", "province", "region"],
    # Screener answers (see profile.answers.*)
    # work-authorization is matched BEFORE location.country so a phrase like
    # "authorization to work in the country where you live" doesn't get stolen
    # by the bare "country" pattern.
    "answers.work_authorization_us": [
        "authorized to work", "work authorization", "legally authorized", "us work authorization",
        "legally eligible to work", "eligible to work", "authorized to work in the united states",
        "authorization to work", "authorised to work",
    ],
    "identity.location.country": ["country"],
    "answers.require_sponsorship": [
        "require sponsorship", "need sponsorship", "visa sponsorship", "sponsorship now",
    ],
    "answers.require_sponsorship_future": [
        "future sponsorship", "sponsorship in the future", "will you require sponsorship",
    ],
    "answers.over_18": ["over 18", "at least 18", "18 years"],
    "answers.willing_to_relocate": ["relocate", "willing to relocate", "relocation"],
    "answers.remote_ok": ["remote", "work remotely", "open to remote"],
    "answers.start_date": [
        "available start", "when can you start", "availability", "earliest start",
    ],
    "answers.salary_expectation": [
        "salary", "compensation expectation", "expected salary", "pay expectation",
    ],
    "answers.how_did_you_hear": ["how did you hear", "referral source", "how did you find"],
    "answers.previously_employed_here": [
        "previously employed", "former employee", "worked here before",
        "worked for", "worked at", "ever worked for", "ever worked at",
        "previously worked",
    ],
    "answers.criminal_record_disclosures": ["criminal", "convicted", "felony"],
    "answers.security_clearance": ["security clearance", "clearance"],
    # EEO / voluntary self-id
    "answers.eeo.gender": ["gender"],
    "answers.eeo.race": [
        "race", "ethnicity", "hispanic or latino", "hispanic/latino", "hispanic",
        "racial", "ethnic background",
    ],
    "answers.eeo.orientation": ["sexual orientation"],
    "answers.eeo.transgender": ["transgender"],
    "answers.eeo.veteran": ["veteran", "protected veteran"],
    "answers.eeo.disability": ["disability", "disabled"],
    # Documents
    "documents.resume": ["resume", "cv", "upload resume", "attach resume"],
    # Education (first entry used by the mapper)
    "experience.0.title": ["current position", "current title", "current role", "job title"],
    "experience.0.company": ["current company", "current employer", "most recent company"],
    "education.0.school": ["school", "university", "college", "institution"],
    "education.0.degree": [
        "degree", "highest level of education", "highest education", "education level",
    ],
    "education.0.major": ["major", "field of study", "discipline"],
    "education.0.gpa": ["gpa", "grade point"],
    "education.0.start_month": ["start date month"],
    "education.0.start_year": ["start date year"],
    "education.0.end_month": ["end date month"],
    "education.0.end_year": ["end date year", "graduation year"],
    "education.0.end": ["graduation date", "anticipated graduation"],
}

# HTML autocomplete tokens -> dotted key (strongest signal, checked first).
AUTOCOMPLETE_MAP: dict[str, str] = {
    "given-name": "identity.first_name",
    "family-name": "identity.last_name",
    "email": "identity.email",
    "tel": "identity.phone",
    "tel-national": "identity.phone",
    "address-level2": "identity.location.city",
    "address-level1": "identity.location.state",
    "country-name": "identity.location.country",
    "url": "identity.links.website",
    "organization-title": None,  # explicitly ignore job-title autofill from browser
}


def match_key(*, label: str, name: str, field_id: str, aria: str, autocomplete: str) -> str | None:
    """Return the best dotted profile key for a field's attributes, or None.

    Autocomplete is authoritative when present. Otherwise the combined text of
    label/name/id/aria is scanned against :data:`SYNONYMS` in priority order.
    """
    if autocomplete:
        ac = autocomplete.strip().lower()
        if ac in AUTOCOMPLETE_MAP:
            return AUTOCOMPLETE_MAP[ac]  # may be None => intentionally unmapped

    haystack = " ".join(t for t in (label, name, field_id, aria) if t).lower()
    if not haystack:
        return None
    for key, patterns in SYNONYMS.items():
        if any(_matches(p, haystack) for p in patterns):
            return key
    return None


def _matches(pattern: str, haystack: str) -> bool:
    """Word-boundary containment so ``tel`` doesn't match ``tell us`` (SPEC.md §5.1)."""
    return re.search(rf"\b{re.escape(pattern.lower())}\b", haystack) is not None


#: Canonical expansions tried (in order) when a constrained field's value is a
#: common abbreviation that fuzzy-matching alone can't bridge ("B.S." vs
#: "Bachelor's Degree"). Same fact, different spelling — never a new claim.
VALUE_ALIASES: dict[str, tuple[str, ...]] = {
    "b.s.": ("Bachelor of Science", "Bachelor's Degree", "Bachelors", "Bachelor's"),
    "bs": ("Bachelor of Science", "Bachelor's Degree", "Bachelors", "Bachelor's"),
    "b.a.": ("Bachelor of Arts", "Bachelor's Degree", "Bachelors"),
    "m.s.": ("Master of Science", "Master's Degree", "Masters"),
    "ms": ("Master of Science", "Master's Degree", "Masters"),
    "m.a.": ("Master of Arts", "Master's Degree", "Masters"),
    "phd": ("Doctor of Philosophy", "Doctorate", "PhD"),
    "ph.d.": ("Doctor of Philosophy", "Doctorate", "PhD"),
    # Discipline taxonomies rarely list this exact major; "Other" is the
    # truthful pick when the real one is absent (tried last).
    "cognitive science: machine learning & neural computation": (
        "Cognitive Science",
        "Other",
    ),
    "cognitive science": ("Cognitive Science", "Other"),
    # "How did you hear" lists rarely include a company-website entry verbatim.
    "company website": ("Company website", "Company Website", "Careers page", "Other"),
    # Self-ID "decline" phrasings differ per form; all mean the same choice.
    "decline": (
        "Decline To Self Identify", "I prefer not to say", "Prefer not to say",
        "I don't wish to answer", "Decline to state",
    ),
    # "Not a protected veteran" == "No" on yes/no veteran questions.
    "i am not a protected veteran": (
        "I am not a protected veteran", "No, I am not a protected veteran", "No",
    ),
    "i do not want to answer": (
        "I do not want to answer", "I prefer not to say", "Prefer not to say",
        "I don't wish to answer", "Decline To Self Identify",
    ),
    # School lists disagree on the UC naming convention; all are the same school.
    "university of california, san diego": (
        "University of California, San Diego",
        "University of California - San Diego",
        "University of California San Diego",
        "UC San Diego",
        "UCSD",
        "San Diego",
    ),
}
