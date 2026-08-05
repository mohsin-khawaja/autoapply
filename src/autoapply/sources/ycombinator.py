"""Y Combinator jobs source — new-grad-filtered startup roles.

The public jobs board at ``ycombinator.com/jobs/role/<role>`` ships its listing
data as an Inertia.js payload in a ``data-page`` attribute, so no HTML scraping
heuristics are needed. That payload carries ``minExperience`` verbatim ("Any
(new grads ok)", "3+ years", ...), which is a far better entry-level signal than
guessing from a title — it is the field this module filters on.

``/jobs`` is allowed by ycombinator.com/robots.txt (only ``/companies?*``,
``/library?*`` and ``/verify/*`` are disallowed); requests are paced.

Applying is NOT automatable: every ``applyUrl`` redirects through
``account.ycombinator.com`` into Work at a Startup, and the application itself
is a message to the founder. These rows are stored with ``ats="yc"`` so the
runner routes them to the manual tier for a human to send.
"""

from __future__ import annotations

import datetime as dt
import html
import json
import re
import time
from dataclasses import dataclass

import httpx

from autoapply import db
from autoapply.config import Settings

JOBS_URL = "https://www.ycombinator.com/jobs/role/{role}"

#: Engineering-adjacent roles worth pulling for this profile.
DEFAULT_ROLES = ("software-engineer", "science")

#: ``minExperience`` values a ~1-year-experience applicant can realistically win.
NEW_GRAD_EXPERIENCE = frozenset({"Any (new grads ok)", "1+ years"})

_DATA_PAGE = re.compile(r'data-page="(.*?)"\s*>', re.S)

#: YC writes locations as slash-separated tokens: "San Francisco, CA, US",
#: "CO / Remote (CO)", "US / GB / AU / Remote (US; GB; AU)". The country code is
#: the reliable signal — a bare "CA" means Canada, while California always
#: appears alongside "US".
_US_CITIES = (
    "san francisco", "bay area", "new york", "nyc", "seattle", "los angeles",
    "austin", "boston", "palo alto", "mountain view", "san jose", "berkeley",
    "oakland", "san diego", "chicago", "denver", "miami", "washington",
    "sunnyvale", "menlo park", "santa clara", "cupertino", "brooklyn",
)
_NON_US_CODES = frozenset(
    "IN CO GB UK AU ES FR MX DK SE IT PL CZ PT BE NL FI EE BR DE JP CN IL SG AR "
    "CL PH ID NG KE UA RU CH AT IE NO NZ ZA TR VN TH MY KR HK TW EG PK BD RO GR "
    "HU BG HR RS LT LV SK SI PE UY EC CR PA DO GT CA".split()
)
_TOKENS = re.compile(r"[/,;()]| - ")


@dataclass(slots=True)
class Posting:
    """A YC job posting, normalized to the fields this tool scores on."""

    id: str
    company_name: str
    title: str
    url: str
    location: str
    min_experience: str
    role_type: str
    job_type: str
    batch: str

    @classmethod
    def from_payload(cls, d: dict) -> Posting:
        url = str(d.get("url", ""))
        if url.startswith("/"):
            url = f"https://www.ycombinator.com{url}"
        return cls(
            id=f"yc-{d.get('id')}",
            company_name=str(d.get("companyName", "")),
            title=str(d.get("title", "")),
            url=url,
            location=str(d.get("location", "")),
            min_experience=str(d.get("minExperience") or ""),
            role_type=str(d.get("roleSpecificType") or ""),
            job_type=str(d.get("type") or ""),
            batch=str(d.get("companyBatchName") or ""),
        )


def fetch_postings(
    roles: tuple[str, ...] = DEFAULT_ROLES,
    *,
    client: httpx.Client | None = None,
    pace_seconds: float = 2.0,
) -> list[Posting]:
    """Fetch and parse YC job postings for ``roles``. Never raises on one bad role."""
    owns = client is None
    c = client or httpx.Client(
        timeout=30.0, follow_redirects=True, headers={"user-agent": "autoapply/0.1"}
    )
    out: list[Posting] = []
    try:
        for i, role in enumerate(roles):
            if i and pace_seconds:
                time.sleep(pace_seconds)
            try:
                resp = c.get(JOBS_URL.format(role=role))
                resp.raise_for_status()
                out.extend(parse_postings(resp.text))
            except (httpx.HTTPError, ValueError):
                continue  # one unavailable role must not lose the others
    finally:
        if owns:
            c.close()
    # The role pages overlap; keep one row per posting id.
    return list({p.id: p for p in out}.values())


def parse_postings(page_html: str) -> list[Posting]:
    """Extract postings from a jobs-page HTML document. Raises ValueError if absent."""
    m = _DATA_PAGE.search(page_html)
    if not m:
        raise ValueError("no data-page payload — YC changed their page shape")
    payload = json.loads(html.unescape(m.group(1)))
    listings = payload.get("props", {}).get("jobPostings") or []
    return [Posting.from_payload(d) for d in listings if d.get("id")]


def is_new_grad(p: Posting) -> bool:
    """True when the posting's own experience bar is within reach."""
    return p.min_experience in NEW_GRAD_EXPERIENCE


def is_us(p: Posting) -> bool:
    """True when the posting is US-based. Requires positive evidence.

    YC's board is global, so an unrecognized location is treated as not-US
    rather than assumed — a wrong "yes" costs a wasted application slot.
    """
    loc = p.location.strip()
    if not loc:
        return False
    tokens = {t.strip().upper() for t in _TOKENS.split(loc) if t.strip()}
    low = loc.lower()
    if {"US", "USA", "UNITED STATES"} & tokens:
        return True  # an explicit US listing wins even in a multi-country post
    # Checked before the country codes: "San Francisco, CA" carries no "US"
    # token, and a bare "CA" would otherwise read as Canada.
    if any(c in low for c in _US_CITIES):
        return True
    if tokens & _NON_US_CODES or any(
        c in low for c in ("india", "canada", "united kingdom", "germany")
    ):
        return False
    # Bare "Remote" with no country anywhere: YC startups default to US.
    return low.replace("remote", "").strip(" ()/") == ""


def score_posting(p: Posting) -> int:
    """Score 0-100. Hard-excludes anything not US, not full-time, or above level."""
    if not is_new_grad(p) or not is_us(p):
        return 0
    if p.job_type and p.job_type.lower() != "full-time":
        return 0

    score = 50  # cleared the entry-level and location bars
    if p.min_experience == "Any (new grads ok)":
        score += 20
    role = p.role_type.lower()
    if role in ("machine learning", "data science"):
        score += 25
    elif role in ("backend", "full stack", "frontend"):
        score += 15
    title = p.title.lower()
    if any(t in title for t in ("machine learning", "ml ", "ai ", "new grad", "junior")):
        score += 5
    return max(0, min(100, score))


@dataclass(slots=True)
class YCSyncResult:
    fetched: int
    new_grad: int
    upserted: int
    new: int


def sync(settings: Settings, *, roles: tuple[str, ...] = DEFAULT_ROLES) -> YCSyncResult:
    """Pull YC postings, keep the new-grad ones, upsert into the jobs table."""
    postings = fetch_postings(roles)
    keep = [p for p in postings if score_posting(p) > 0]
    now = dt.datetime.now(dt.UTC).isoformat()
    conn = db.connect(settings.db_path)
    new = 0
    try:
        with db.transaction(conn):
            for p in keep:
                status = db.upsert_job(
                    conn,
                    id=p.id,
                    company_name=p.company_name,
                    title=p.title,
                    url=p.url,
                    now_iso=now,
                    # Applying goes through a YC login and sends a founder
                    # message, so this never enters the auto-submit path.
                    ats="yc",
                    category=p.role_type,
                    locations_json=json.dumps([p.location]),
                    score=score_posting(p),
                )
                if status == "new":
                    new += 1
    finally:
        conn.close()
    return YCSyncResult(
        fetched=len(postings), new_grad=len(keep), upserted=len(keep), new=new
    )
