"""Big-tech career-site sources: Workday CXS tenants + IBM careers search.

Both are public JSON APIs the career sites themselves call — no HTML scraping,
no auth, no bot-wall evasion (SPEC.md §10). Listings are scored for
*interview fit* against profile.yaml (skills/title/level match), then upserted
into the same ``jobs`` table the SimplifyJobs feed uses, so queue/run/dashboard
work unchanged.
"""

from __future__ import annotations

import datetime as dt
import json
import re
from dataclasses import dataclass

import httpx

from autoapply.config import Settings
from autoapply.db import connect, transaction, upsert_job
from autoapply.profile import Profile, load_profile
from autoapply.sources.simplify import (
    HIGH_LOCATIONS,
    MEDIUM_LOCATIONS,
    Listing,
    heuristic_score,
)

#: Verified Workday CXS tenants (host prefix, tenant, site). All return public
#: JSON from POST /wday/cxs/{tenant}/{site}/jobs.
WORKDAY_SITES: dict[str, tuple[str, str, str]] = {
    "NVIDIA": ("nvidia.wd5", "nvidia", "NVIDIAExternalCareerSite"),
    "Salesforce": ("salesforce.wd12", "salesforce", "External_Career_Site"),
    "Adobe": ("adobe.wd5", "adobe", "external_experienced"),
    "Intel": ("intel.wd1", "intel", "External"),
    "Dell": ("dell.wd1", "dell", "External"),
    "PayPal": ("paypal.wd1", "paypal", "jobs"),
    "Workday": ("workday.wd5", "workday", "Workday"),
}

IBM_SEARCH_URL = "https://www-api.ibm.com/search/api/v2"

#: Searches run per site — derived from the targeting profile (SPEC.md §3).
DEFAULT_SEARCHES = (
    "machine learning engineer new grad",
    "ai engineer early career",
    "software engineer new grad",
)

_ENTRY_HINTS = ("new grad", "entry level", "early career", "university", "graduate", "intern")

#: Seniority variants the shared excludes miss ("Sr", "Dir", "Engineer 4").
_SENIOR_HINTS = ("sr ", "sr.", "sr,", "dir,", "dir ", "director", "manager", "lead ", "head of")
_SENIOR_RE = re.compile(r"\b(?:[3-9]|iii|iv|v|vi)\b\s*$")
_US_TOKEN_RE = re.compile(r"\bus\b")


def _is_us(job: BigTechJob) -> bool:
    """Strict US gate — big-tech sites are global, unlike the new-grad feed.

    The shared ``_has_us_location`` treats ", ca" as California, which
    false-positives on "Calgary, CA"; match explicit US signals instead.
    """
    text = " ".join(job.locations).lower()
    if not text:
        return True
    if "united states" in text or "remote" in text or _US_TOKEN_RE.search(text):
        return True
    return any(city in text for city in HIGH_LOCATIONS + MEDIUM_LOCATIONS)


@dataclass(slots=True)
class BigTechJob:
    """One listing normalized from a big-tech source."""

    id: str
    company: str
    title: str
    url: str
    locations: list[str]
    level: str | None
    posted: str | None  # ISO date if known

    def to_listing(self) -> Listing:
        return Listing(
            id=self.id,
            company_name=self.company,
            title=self.title,
            url=self.url,
            locations=self.locations,
            sponsorship="Other",
            category="",
            active=True,
            is_visible=True,
            date_posted=_epoch(self.posted),
        )


def _epoch(iso_date: str | None) -> int | None:
    if not iso_date:
        return None
    try:
        return int(dt.datetime.fromisoformat(iso_date).replace(tzinfo=dt.UTC).timestamp())
    except ValueError:
        return None


def fit_score(job: BigTechJob, profile: Profile) -> int:
    """0-100 interview-fit score: base heuristics + entry-level + skill match.

    Grounded in profile.yaml only — skills that appear in the title add
    confidence that a recruiter screen matches the resume.
    """
    base = heuristic_score(job.to_listing())
    if base == 0:  # hard-excluded (senior/clearance/non-US)
        return 0
    title = job.title.lower()
    if any(h in title for h in _SENIOR_HINTS) or _SENIOR_RE.search(title):
        return 0
    if not _is_us(job):
        return 0
    bonus = 0
    level = (job.level or "").lower()
    if any(h in title for h in _ENTRY_HINTS) or "entry" in level:
        bonus += 15
    matched = sum(1 for s in profile.skills if s.lower() in title)
    bonus += min(10, matched * 5)
    return min(100, base + bonus)


def fetch_workday(
    company: str, *, client: httpx.Client, searches: tuple[str, ...] = DEFAULT_SEARCHES,
    per_search: int = 20,
) -> list[BigTechJob]:
    """Fetch listings from one Workday tenant via its public CXS jobs API."""
    host, tenant, site = WORKDAY_SITES[company]
    out: dict[str, BigTechJob] = {}
    for text in searches:
        r = client.post(
            f"https://{host}.myworkdayjobs.com/wday/cxs/{tenant}/{site}/jobs",
            json={"appliedFacets": {}, "limit": per_search, "offset": 0, "searchText": text},
            timeout=20,
        )
        r.raise_for_status()
        for p in r.json().get("jobPostings", []):
            path = p.get("externalPath") or ""
            req = (p.get("bulletFields") or [path])[0]
            jid = f"bt-{tenant}-{req}"
            out[jid] = BigTechJob(
                id=jid,
                company=company,
                title=p.get("title", ""),
                url=f"https://{host}.myworkdayjobs.com/en-US/{site}{path}",
                locations=[p.get("locationsText", "")],
                level=None,
                posted=None,
            )
    return list(out.values())


def fetch_ibm(
    *, client: httpx.Client, searches: tuple[str, ...] = DEFAULT_SEARCHES, per_search: int = 20
) -> list[BigTechJob]:
    """Fetch listings from IBM's public careers search API."""
    out: dict[str, BigTechJob] = {}
    for text in searches:
        r = client.post(
            IBM_SEARCH_URL,
            json={
                "appId": "careers",
                "scopes": ["careers2"],
                "query": {
                    "bool": {
                        "must": [{"query_string": {"query": text, "fields": ["title"]}}]
                    }
                },
                "size": per_search,
                "_source": [
                    "title", "url", "dcdate",
                    "field_keyword_05",  # country
                    "field_keyword_18",  # level
                    "field_keyword_19",  # city
                ],
            },
            timeout=20,
        )
        r.raise_for_status()
        for hit in r.json().get("hits", {}).get("hits", []):
            src = hit.get("_source", {})
            jid = f"bt-ibm-{hit['_id'][:16]}"
            city = src.get("field_keyword_19") or ""
            country = src.get("field_keyword_05") or ""
            out[jid] = BigTechJob(
                id=jid,
                company="IBM",
                title=src.get("title", ""),
                url=src.get("url", ""),
                locations=[loc for loc in (city, country) if loc],
                level=src.get("field_keyword_18"),
                posted=src.get("dcdate"),
            )
    return list(out.values())


@dataclass(slots=True)
class ScoutResult:
    fetched: int
    upserted: int
    scored_ge_70: int
    errors: list[str]


def scout(settings: Settings, *, min_score: int = 0) -> ScoutResult:
    """Fetch all big-tech sources, fit-score against the profile, upsert."""
    profile = load_profile(settings.profile_path)
    now_iso = dt.datetime.now(dt.UTC).isoformat()
    jobs: list[BigTechJob] = []
    errors: list[str] = []
    with httpx.Client(headers={"User-Agent": "autoapply/0.1 (personal job search)"}) as client:
        for company in WORKDAY_SITES:
            try:
                jobs.extend(fetch_workday(company, client=client))
            except Exception as e:  # noqa: BLE001 - one dead tenant must not kill the scout
                errors.append(f"{company}: {e}")
        try:
            jobs.extend(fetch_ibm(client=client))
        except Exception as e:  # noqa: BLE001
            errors.append(f"IBM: {e}")

    conn = connect(settings.db_path)
    upserted = hi = 0
    with transaction(conn):
        for job in jobs:
            score = fit_score(job, profile)
            if score < min_score:
                continue
            ats = "workday" if "myworkdayjobs.com" in job.url else "generic"
            upsert_job(
                conn,
                id=job.id,
                company_name=job.company,
                title=job.title,
                url=job.url,
                now_iso=now_iso,
                final_url=job.url,
                ats=ats,
                locations_json=json.dumps(job.locations),
                score=score,
                date_posted=_epoch(job.posted),
            )
            upserted += 1
            if score >= 70:
                hi += 1
    conn.close()
    return ScoutResult(fetched=len(jobs), upserted=upserted, scored_ge_70=hi, errors=errors)
