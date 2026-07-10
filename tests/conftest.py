"""Shared fixtures: an in-memory DB and a small slice of realistic feed listings."""

from __future__ import annotations

import pytest

from autoapply import db


@pytest.fixture
def conn():
    c = db.connect(":memory:")
    yield c
    c.close()


@pytest.fixture
def feed_listings() -> list[dict]:
    """Realistic listing dicts mirroring the live feed schema (SPEC.md §3)."""
    return [
        {
            "id": "aaaa1111",
            "company_name": "Acme AI",
            "title": "Machine Learning Engineer, New Grad",
            "url": "https://jobs.ashbyhq.com/acme/abc",
            "locations": ["San Francisco, CA"],
            "sponsorship": "Other",
            "category": "AI/ML",
            "active": True,
            "is_visible": True,
            "date_posted": 1_700_000_000,
        },
        {
            "id": "bbbb2222",
            "company_name": "Globex",
            "title": "Software Engineer I",
            "url": "https://boards.greenhouse.io/globex/jobs/123",
            "locations": ["New York, NY"],
            "sponsorship": "Does Not Offer Sponsorship",
            "category": "Software",
            "active": True,
            "is_visible": True,
            "date_posted": 1_700_000_000,
        },
        {
            "id": "cccc3333",
            "company_name": "Initech",
            "title": "Senior Staff Engineer",
            "url": "https://jobs.lever.co/initech/xyz",
            "locations": ["Austin, TX"],
            "sponsorship": "Other",
            "category": "Software",
            "active": True,
            "is_visible": True,
            "date_posted": 1_700_000_000,
        },
        {
            "id": "dddd4444",
            "company_name": "Umbrella",
            "title": "AI Engineer",
            "url": "https://umbrella.wd1.myworkdayjobs.com/careers/job/999",
            "locations": ["London, UK"],
            "sponsorship": "Other",
            "category": "AI/ML",
            "active": True,
            "is_visible": True,
            "date_posted": 1_700_000_000,
        },
        {
            "id": "eeee5555",
            "company_name": "Hooli",
            "title": "AI Engineer, New Grad",
            "url": "https://hooli.wd5.myworkdayjobs.com/x/job/1",
            "locations": ["Remote"],
            "sponsorship": "Other",
            "category": "AI/ML",
            "active": False,   # inactive -> score 0
            "is_visible": True,
            "date_posted": 1_700_000_000,
        },
    ]
