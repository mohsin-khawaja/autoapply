"""Referral-company sources: companies where the user has a warm intro.

Unlike :mod:`autoapply.sources.simplify` (new-grad ML/SWE feed), this source is
deliberately BROAD — engineer, analyst, data, product/project/program manager —
because a referral is worth spending on any role the user can actually do.

Public JSON / HTML the career sites serve themselves; no auth, no bot-wall
evasion (SPEC.md §10). LinkedIn is intentionally absent: SPEC.md §10 forbids
scraping it (auth-walled + ToS), so those roles stay a manual search.
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

AMAZON_SEARCH_URL = "https://www.amazon.jobs/en/search.json"
ODOO_JOBS_URL = "https://www.odoo.com/jobs"

#: Role families the user is open to, widest-first. Each becomes one query.
ROLE_SEARCHES = (
    "software engineer",
    "software development engineer",
    "data engineer",
    "data analyst",
    "business analyst",
    "business intelligence engineer",
    "machine learning engineer",
    "product manager",
    "technical program manager",
    "project manager",
    "solutions architect",
    "support engineer",
)

#: Seniority markers that rule a posting out for a recent grad. "manager" is
#: NOT here on purpose — product/project/program manager are target roles; only
#: people-leadership and senior variants are excluded.
_SENIOR_RE = re.compile(
    r"\b(senior|sr\.?|staff|principal|director|vp|head of|lead|architect iii|"
    r"engineering manager|manager, engineering|group manager|"
    r"iii|iv|v|level [3-9]|l[5-9])\b"
)

#: Entry-level signals worth a bonus.
_ENTRY_HINTS = (
    "new grad", "entry level", "entry-level", "early career", "university",
    "graduate", "associate", "i,", " i ", "intern", "apprentice", "junior",
)

#: Role families that fit a CS/cog-sci background, best-fit first.
_ROLE_TIERS: tuple[tuple[int, tuple[str, ...]], ...] = (
    (55, ("machine learning", "ml engineer", "ai engineer", "applied scientist",
          "data scientist", "research engineer")),
    (50, ("software engineer", "software development engineer", "sde",
          "data engineer", "full stack", "backend", "front end", "frontend")),
    (42, ("data analyst", "business intelligence", "analytics engineer",
          "business analyst", "solutions architect", "solutions engineer",
          "support engineer", "systems engineer", "quality assurance")),
    (38, ("product manager", "technical program manager", "program manager",
          "project manager", "product analyst", "operations analyst")),
)

#: Roles that don't use the user's background — kept out so a referral isn't
#: spent on a mismatch. Surfaced only if the user asks for them explicitly.
_OFF_PROFILE = (
    "line cook", "chef", "accountant", "videographer", "recruiter", "attorney",
    "nurse", "driver", "warehouse", "sales representative", "account executive",
    "customer care", "receptionist", "security guard", "maintenance",
)

_US_HINTS = (
    "united states", "usa", ", us", "u.s.", "remote",
    "seattle", "bellevue", "redmond", "san francisco", "san jose", "sunnyvale",
    "santa clara", "palo alto", "mountain view", "cupertino", "new york", "nyc",
    "austin", "boston", "chicago", "denver", "atlanta", "arlington", "san diego",
    "los angeles", "irvine", "portland", "dallas", "houston", "phoenix",
    "washington", "ca", "wa", "ny", "tx", "ma",
)


@dataclass(slots=True)
class ReferralJob:
    """One posting from a referral company."""

    id: str
    company: str
    title: str
    url: str
    location: str
    category: str | None
    posted: str | None  # ISO date when known

    @property
    def locations(self) -> list[str]:
        return [self.location] if self.location else []


def _is_us(location: str) -> bool:
    text = location.lower()
    if not text:
        return True  # unspecified — let the human judge rather than drop it
    return any(h in text for h in _US_HINTS)


def fit_score(job: ReferralJob, profile: Profile) -> int:
    """0-100 fit for a referral role. Broad by design: any role the user can do.

    Hard-excludes senior/leadership titles, non-US locations, and roles that
    don't use the user's background. Otherwise ranks by role family, then
    entry-level signals and profile skill overlap.
    """
    title = job.title.lower()
    if _SENIOR_RE.search(title):
        return 0
    if any(h in title for h in _OFF_PROFILE):
        return 0
    if not _is_us(job.location):
        return 0

    score = 12  # floor: a referral-company role the user is open to
    for points, needles in _ROLE_TIERS:
        if any(n in title for n in needles):
            score += points
            break

    if any(h in title for h in _ENTRY_HINTS):
        score += 18
    matched = sum(1 for s in profile.skills if s.lower() in title)
    score += min(10, matched * 5)
    return min(100, score)


def fetch_amazon(
    *, client: httpx.Client, searches: tuple[str, ...] = ROLE_SEARCHES, per_search: int = 20
) -> list[ReferralJob]:
    """Amazon postings via the public amazon.jobs search endpoint."""
    out: dict[str, ReferralJob] = {}
    for text in searches:
        r = client.get(
            AMAZON_SEARCH_URL,
            params={
                "base_query": text,
                "loc_query": "United States",
                "country": "USA",
                "result_limit": per_search,
                "sort": "recent",
            },
            timeout=25,
        )
        r.raise_for_status()
        for p in r.json().get("jobs", []):
            jid = f"ref-amazon-{p.get('id_icims') or p.get('job_path', '')[-12:]}"
            out[jid] = ReferralJob(
                id=jid,
                company="Amazon",
                title=p.get("title", ""),
                url=f"https://www.amazon.jobs{p.get('job_path', '')}",
                location=p.get("normalized_location") or p.get("location", ""),
                category=p.get("job_category"),
                posted=_amazon_date(p.get("posted_date")),
            )
    return list(out.values())


def _amazon_date(text: str | None) -> str | None:
    """"July 24, 2026" -> "2026-07-24"; None when unparseable."""
    if not text:
        return None
    try:
        return dt.datetime.strptime(text.strip(), "%B %d, %Y").date().isoformat()
    except ValueError:
        return None


#: Odoo renders jobs as plain HTML: an /jobs/<slug>-<id> anchor per card.
_ODOO_CARD_RE = re.compile(
    r'href="(/jobs/(?!page/)[^"]+)".{0,4000}?<h3[^>]*>\s*([^<]{3,120}?)\s*</h3>',
    re.DOTALL,
)
_ODOO_ROW_RE = re.compile(r'href="(/jobs/(?!page/)[^"]+)"')


def fetch_odoo(*, client: httpx.Client, pages: int = 3) -> list[ReferralJob]:
    """Odoo postings scraped from their public jobs listing pages."""
    out: dict[str, ReferralJob] = {}
    for page in range(1, pages + 1):
        url = ODOO_JOBS_URL if page == 1 else f"{ODOO_JOBS_URL}/page/{page}"
        r = client.get(url, timeout=25)
        if r.status_code != 200:
            break
        html = r.text
        titles = re.findall(r'<h3 class="text-900 mb-0">\s*([^<]{3,120}?)\s*</h3>', html)
        paths = _ODOO_ROW_RE.findall(html)
        for path, title in zip(paths, titles, strict=False):
            slug = path.rstrip("/").rsplit("/", 1)[-1]
            jid = f"ref-odoo-{slug}"
            # Odoo puts the office in the card body; the slug often carries it.
            loc = "United States" if re.search(r"-(ny|ca|tx|usa?)\b", slug) else ""
            out[jid] = ReferralJob(
                id=jid,
                company="Odoo",
                title=title,
                url=f"https://www.odoo.com{path}",
                location=loc,
                category=None,
                posted=None,
            )
    return list(out.values())


@dataclass(slots=True)
class ReferralResult:
    fetched: int
    upserted: int
    scored_ge_50: int
    errors: list[str]


def scout(settings: Settings, *, min_score: int = 1) -> ReferralResult:
    """Fetch referral-company postings, fit-score them, and upsert into ``jobs``."""
    profile = load_profile(settings.profile_path)
    now_iso = dt.datetime.now(dt.UTC).isoformat()
    jobs: list[ReferralJob] = []
    errors: list[str] = []
    headers = {"User-Agent": "Mozilla/5.0 (autoapply personal job search)"}
    with httpx.Client(headers=headers, follow_redirects=True) as client:
        for name, fn in (("Amazon", fetch_amazon), ("Odoo", fetch_odoo)):
            try:
                jobs.extend(fn(client=client))
            except Exception as e:  # noqa: BLE001 - one dead source must not stop the rest
                errors.append(f"{name}: {e}")

    conn = connect(settings.db_path)
    upserted = hi = 0
    with transaction(conn):
        for job in jobs:
            score = fit_score(job, profile)
            if score < min_score:
                continue
            posted_epoch = None
            if job.posted:
                try:
                    posted_epoch = int(
                        dt.datetime.fromisoformat(job.posted)
                        .replace(tzinfo=dt.UTC)
                        .timestamp()
                    )
                except ValueError:
                    posted_epoch = None
            upsert_job(
                conn,
                id=job.id,
                company_name=job.company,
                title=job.title,
                url=job.url,
                now_iso=now_iso,
                final_url=job.url,
                ats="generic",
                category=job.category,
                locations_json=json.dumps(job.locations),
                score=score,
                date_posted=posted_epoch,
            )
            upserted += 1
            if score >= 50:
                hi += 1
    conn.close()
    return ReferralResult(
        fetched=len(jobs), upserted=upserted, scored_ge_50=hi, errors=errors
    )
