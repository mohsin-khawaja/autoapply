"""SimplifyJobs New-Grad-Positions ingestion + heuristic scoring (SPEC.md §3).

Pulls the JSON feed (not the README), classifies each URL by ATS family, scores
each listing 0-100 against the fixed targeting profile, and upserts into SQLite.
The optional local-LLM relevance pass is deferred (top candidates only) and off
by default in Milestone 0.

Live-verified feed keys: active, category, company_name, company_url,
date_posted, date_updated, degrees, id, is_visible, locations, source,
sponsorship, title, url.
"""

from __future__ import annotations

import datetime as dt
import json
from dataclasses import dataclass
from urllib.parse import urlparse

import httpx

from autoapply import db
from autoapply.ats.base import ATSKind
from autoapply.config import LISTINGS_URL, Settings

# ---- ATS classification ---------------------------------------------------


def classify_ats(url: str) -> ATSKind:
    """Classify a job URL into an :class:`ATSKind` by host/path (SPEC.md §3)."""
    host = (urlparse(url).netloc or "").lower()
    if "ashbyhq.com" in host:
        return ATSKind.ASHBY
    if "greenhouse.io" in host:
        return ATSKind.GREENHOUSE
    if "lever.co" in host:
        return ATSKind.LEVER
    if "smartrecruiters.com" in host:
        return ATSKind.SMARTRECRUITERS
    if "rippling.com" in host:
        return ATSKind.RIPPLING
    if "myworkdayjobs.com" in host or "workday" in host:
        return ATSKind.WORKDAY  # v1 manual tier (SPEC.md §5)
    if "icims.com" in host:
        return ATSKind.ICIMS
    return ATSKind.GENERIC


#: ATS families handled by a headless-fillable adapter vs. left as manual tier.
MANUAL_TIER = frozenset({ATSKind.WORKDAY})


def tier_for(ats: ATSKind) -> str:
    """Return ``"manual"`` for out-of-scope ATSs (Workday), else ``"auto"``."""
    return "manual" if ats in MANUAL_TIER else "auto"


# ---- redirect resolution --------------------------------------------------


def resolve_redirect(url: str, *, client: httpx.Client | None = None) -> str:
    """Follow ``simplify.jobs`` links to the final ATS URL. No-op for direct URLs.

    Most feed URLs are already the final ATS; only ``simplify.jobs/p/...`` links
    need a follow. Best-effort: returns the original URL on any error.
    """
    if "simplify.jobs" not in url:
        return url
    owns = client is None
    c = client or httpx.Client(follow_redirects=True, timeout=15.0)
    try:
        resp = c.head(url)
        final = str(resp.url)
        if final and "simplify.jobs" not in urlparse(final).netloc:
            return final
        # Some hosts don't honor HEAD; fall back to GET.
        resp = c.get(url)
        return str(resp.url)
    except httpx.HTTPError:
        return url
    finally:
        if owns:
            c.close()


# ---- scoring --------------------------------------------------------------

HIGH_TITLES = (
    "ml engineer", "machine learning engineer", "machine learning", "ai engineer",
    "applied ai", "applied ml", "ai solutions", "forward deployed", "solutions engineer",
    "ai/ml", "deep learning", "research engineer",
)
MEDIUM_TITLES = (
    "software engineer", "swe", "backend engineer", "back end engineer", "full stack",
    "data scientist", "data engineer", "new grad", "associate engineer",
)
EXCLUDE_TITLE = ("senior", "staff", "principal", "lead ", "manager", "director", "iii", " iv")

HIGH_LOCATIONS = (
    "san francisco", "sf", "bay area", "san jose", "berkeley", "palo alto", "mountain view",
    "sunnyvale", "menlo park", "cupertino", "santa clara", "oakland", "remote",
)
MEDIUM_LOCATIONS = ("new york", "nyc", "seattle", "san diego")

EXCLUDE_LOCATION_HINTS = (
    "india", "canada", "uk", "united kingdom", "london", "europe", "germany", "singapore",
    "australia", "ireland", "poland", "mexico", "brazil", "japan", "china", "remote - emea",
)
CLEARANCE_HINTS = ("clearance", "ts/sci", "secret clearance", "polygraph")


@dataclass(slots=True)
class Listing:
    """Normalized subset of a feed listing used for scoring/upsert."""

    id: str
    company_name: str
    title: str
    url: str
    locations: list[str]
    sponsorship: str
    category: str
    active: bool
    is_visible: bool
    date_posted: int | None

    @classmethod
    def from_feed(cls, d: dict) -> Listing:
        return cls(
            id=str(d.get("id", "")),
            company_name=str(d.get("company_name", "")),
            title=str(d.get("title", "")),
            url=str(d.get("url", "")),
            locations=[str(x) for x in (d.get("locations") or [])],
            sponsorship=str(d.get("sponsorship", "")),
            category=str(d.get("category", "")),
            active=bool(d.get("active", False)),
            is_visible=bool(d.get("is_visible", False)),
            date_posted=d.get("date_posted") if isinstance(d.get("date_posted"), int) else None,
        )


def heuristic_score(listing: Listing) -> int:
    """Score a listing 0-100 against the fixed targeting profile (SPEC.md §3).

    Cheap keyword/location heuristics only. Sponsorship is NOT a filter (US work
    authorized). Hard excludes (non-US, clearance, senior) return 0.
    """
    if not (listing.active and listing.is_visible):
        return 0

    title = listing.title.lower()
    loc_text = " ".join(listing.locations).lower()

    # Hard excludes.
    if any(h in title for h in EXCLUDE_TITLE):
        return 0
    if any(h in loc_text for h in EXCLUDE_LOCATION_HINTS) and not _has_us_location(listing):
        return 0
    if any(h in title for h in CLEARANCE_HINTS) or any(h in loc_text for h in CLEARANCE_HINTS):
        return 0

    score = 0

    # Title tier (dominant signal).
    if any(t in title for t in HIGH_TITLES):
        score += 55
    elif any(t in title for t in MEDIUM_TITLES):
        score += 35
    else:
        score += 8  # some relevance floor for active new-grad roles

    # Location tier.
    if any(loc in loc_text for loc in HIGH_LOCATIONS) or not loc_text:
        score += 35
    elif any(loc in loc_text for loc in MEDIUM_LOCATIONS):
        score += 20
    elif _has_us_location(listing):
        score += 8

    # Freshness nudge.
    if listing.date_posted:
        age_days = (dt.datetime.now(dt.UTC).timestamp() - listing.date_posted) / 86400
        if age_days <= 7:
            score += 10
        elif age_days <= 30:
            score += 5

    return max(0, min(100, score))


def _has_us_location(listing: Listing) -> bool:
    text = " ".join(listing.locations).lower()
    if not text:
        return True  # unspecified -> assume US new-grad board default
    us_hints = ("united states", "usa", ", ca", ", ny", ", wa", ", tx", ", ma", "remote")
    us_hints += tuple(HIGH_LOCATIONS + MEDIUM_LOCATIONS)
    return any(h in text for h in us_hints)


# ---- sync -----------------------------------------------------------------


@dataclass(slots=True)
class SyncResult:
    total: int
    new: int
    changed: int
    inactive: int
    scored_ge_70: int


def fetch_listings(url: str = LISTINGS_URL, *, client: httpx.Client | None = None) -> list[dict]:
    """Fetch and parse the listings JSON array. Raises on network/parse error."""
    owns = client is None
    c = client or httpx.Client(timeout=60.0, follow_redirects=True)
    try:
        r = c.get(url)
        r.raise_for_status()
        data = json.loads(r.content)
    finally:
        if owns:
            c.close()
    if not isinstance(data, list):
        raise ValueError("listings feed did not return a JSON array")
    return data


def sync(settings: Settings, *, resolve_redirects: bool = False) -> SyncResult:
    """Fetch the feed, score, and upsert into SQLite. Marks new/changed/inactive.

    ``resolve_redirects`` follows the handful of ``simplify.jobs`` links to their
    final ATS (slower; off by default — most URLs are already final).
    """
    raw = fetch_listings()
    now_iso = dt.datetime.now(dt.UTC).isoformat()
    conn = db.connect(settings.db_path)

    counts = {"new": 0, "changed": 0, "unchanged": 0}
    scored_hi = 0
    seen: set[str] = set()

    redir_client = httpx.Client(follow_redirects=True, timeout=15.0) if resolve_redirects else None
    try:
        with db.transaction(conn):
            for d in raw:
                listing = Listing.from_feed(d)
                if not listing.id or not listing.url:
                    continue
                seen.add(listing.id)

                final_url = (
                    resolve_redirect(listing.url, client=redir_client)
                    if resolve_redirects
                    else listing.url
                )
                ats = classify_ats(final_url)
                score = heuristic_score(listing)
                if score >= 70:
                    scored_hi += 1

                status = db.upsert_job(
                    conn,
                    id=listing.id,
                    company_name=listing.company_name,
                    title=listing.title,
                    url=listing.url,
                    final_url=final_url,
                    ats=ats.value,
                    category=listing.category,
                    locations_json=json.dumps(listing.locations),
                    sponsorship=listing.sponsorship,
                    active=listing.active,
                    is_visible=listing.is_visible,
                    score=score,
                    date_posted=listing.date_posted,
                    now_iso=now_iso,
                )
                counts[status] = counts.get(status, 0) + 1

            inactive = db.mark_inactive_absent(conn, seen, now_iso)
    finally:
        if redir_client is not None:
            redir_client.close()
        conn.close()

    return SyncResult(
        total=len(seen),
        new=counts["new"],
        changed=counts["changed"],
        inactive=inactive,
        scored_ge_70=scored_hi,
    )
