"""Public company-to-ATS discovery. GET only; no account or applicant data."""
from __future__ import annotations

import datetime as dt
import hashlib
import html
import ipaddress
import json
import re
from dataclasses import dataclass
from html.parser import HTMLParser
from urllib.parse import parse_qs, quote, unquote, urljoin, urlsplit

import httpx

from autoapply import db
from autoapply.config import Settings
from autoapply.sources.simplify import Listing, heuristic_score

_ATS_HOSTS = {
    "jobs.ashbyhq.com": "ashby", "jobs.lever.co": "lever",
    "boards.greenhouse.io": "greenhouse", "job-boards.greenhouse.io": "greenhouse",
}
_US = re.compile(
    r"\b(?:united states|usa|u\.s\.|us|san francisco|sf|bay area|new york|nyc|seattle|"
    r"san jose|san diego|los angeles|boston|austin|chicago|denver|palo alto|"
    r"mountain view|berkeley|oakland|sunnyvale|santa clara|menlo park|washington dc)\b"
    r"|,\s*(?:CA|NY|WA|TX|MA|CO|IL|VA|GA|FL|NC|PA|OH|NJ|AZ)\b", re.I,
)
_EXPERIENCE = re.compile(
    r"\b(\d+)\s*(?:\+|[-–]\s*\d+)?\s*years?\s+(?:of\s+)?"
    r"(?:[\w-]+\s+){0,6}(?:experience|building|developing|working)", re.I,
)
_OFF_PROFILE = re.compile(
    r"\b(?:counsel|attorney|lawyer|recruiter|paralegal|account executive|"
    r"sales representative|clinical|nurse|mechanical|electrical|hardware|chip design)\b", re.I,
)
_RESTRICTED = re.compile(
    r"(?:requires?|must (?:have|hold|possess))[^.\n]{0,60}"
    r"(?:clearance|ph\.?d)|(?:active|secret|top secret|ts/sci)\s+clearance", re.I,
)
_ATS_URL = re.compile(
    r'https://(?:jobs\.ashbyhq\.com|jobs\.lever\.co|(?:job-)?boards\.greenhouse\.io)'
    r'/[^\s<>"\\]+',
)


@dataclass(frozen=True)
class Board:
    ats: str
    slug: str


@dataclass
class Posting:
    board: Board
    id: str
    title: str
    url: str
    locations: list[str]
    description: str = ""
    posted: int | None = None
    full_time: bool = True

    @property
    def key(self) -> str:
        return f"{self.board.ats}:{self.board.slug.lower()}:{self.id}"


def public_url(url: str) -> bool:
    """Reject credentials, non-web schemes, and literal local addresses."""
    try:
        p = urlsplit(url)
        host = p.hostname or ""
        if p.scheme not in {"https", "http"} or p.username or p.password:
            return False
        if "." not in host or host.endswith((".local", ".localhost", ".internal")):
            return False
        try:
            return ipaddress.ip_address(host).is_global
        except ValueError:
            return True
    except ValueError:
        return False


class Links(HTMLParser):
    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.urls: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag not in {"a", "iframe"}:
            return
        values = dict(attrs)
        value = values.get("href" if tag == "a" else "src")
        if value:
            self.urls.append(value)


def links(text: str, origin: str) -> list[str]:
    parser = Links()
    parser.feed(html.unescape(text.replace("<![CDATA[", "").replace("]]>", "")))
    found = parser.urls + _ATS_URL.findall(html.unescape(text).replace('\\/', '/'))
    return list(dict.fromkeys(urljoin(origin, u) for u in found if public_url(urljoin(origin, u))))


def board_from_url(url: str) -> Board | None:
    p = urlsplit(url)
    ats = _ATS_HOSTS.get(p.hostname or "")
    parts = [unquote(x) for x in p.path.split("/") if x]
    if not ats or not parts:
        return None
    slug = parts[0]
    if ats == "greenhouse" and slug == "embed":
        slug = parse_qs(p.query).get("for", [""])[0]
    if not re.fullmatch(r"[\w. -]+", slug):
        return None
    return Board(ats, slug)


def job_key(url: str) -> str | None:
    board = board_from_url(url)
    if not board:
        return None
    parts = [unquote(x) for x in urlsplit(url).path.split("/") if x]
    if board.ats == "greenhouse":
        jid = parts[2] if len(parts) > 2 and parts[1] == "jobs" else None
        jid = jid or parse_qs(urlsplit(url).query).get("token", [None])[0]
    else:
        jid = parts[1] if len(parts) > 1 else None
    return f"{board.ats}:{board.slug.lower()}:{jid}" if jid else None


def get(client: httpx.Client, url: str) -> httpx.Response:
    """Validate every redirect; use public requests without supplied credentials."""
    for _ in range(6):
        if not public_url(url):
            raise ValueError("not a public web URL")
        response = client.get(url, follow_redirects=False)
        if response.is_redirect:
            url = urljoin(url, response.headers["location"])
            continue
        response.raise_for_status()
        return response
    raise ValueError("too many redirects")


def resolve_boards(client: httpx.Client, url: str) -> set[Board]:
    """Find explicit ATS links on a company page and at most two careers pages."""
    if board := board_from_url(url):
        return {board}
    response = get(client, url)
    found = links(response.text, str(response.url))
    boards = {b for u in found if (b := board_from_url(u))}
    if boards:
        return boards
    host = urlsplit(str(response.url)).hostname
    career_links = [u for u in found if urlsplit(u).hostname == host
                    and re.search(r"/(?:careers?|jobs|join-us|join)(?:/|$)", urlsplit(u).path)]
    for career in list(dict.fromkeys(career_links))[:2]:
        response = get(client, career)
        boards.update(b for u in links(response.text, str(response.url))
                      if (b := board_from_url(u)))
    return boards


def _timestamp(value: str | int | None) -> int | None:
    if isinstance(value, int):
        return value // 1000 if value > 10_000_000_000 else value
    try:
        return int(dt.datetime.fromisoformat(str(value).replace("Z", "+00:00")).timestamp())
    except ValueError:
        return None


def fetch_board(client: httpx.Client, board: Board) -> list[Posting]:
    slug = quote(board.slug, safe="")
    if board.ats == "ashby":
        raw = get(client, f"https://api.ashbyhq.com/posting-api/job-board/{slug}").json()
        if not isinstance(raw.get("jobs"), list):
            raise ValueError("Ashby jobs payload missing")
        out = []
        for job in raw["jobs"]:
            if not job.get("isListed", True):
                continue
            url = job.get("applyUrl") or job.get("jobUrl", "")
            key = job_key(url)
            if not key:
                continue
            locations = [job.get("location", "")]
            address = (job.get("address") or {}).get("postalAddress") or {}
            locations.append(address.get("addressCountry", ""))
            locations.extend(x.get("location", "") for x in (job.get("secondaryLocations") or []))
            out.append(Posting(board, key.rsplit(":", 1)[1], job["title"], url, locations,
                               job.get("descriptionPlain") or "",
                               _timestamp(job.get("publishedAt")),
                               job.get("employmentType", "FullTime") == "FullTime"))
        return out
    if board.ats == "greenhouse":
        raw = get(client, f"https://boards-api.greenhouse.io/v1/boards/{slug}/jobs?content=true").json()
        if not isinstance(raw.get("jobs"), list):
            raise ValueError("Greenhouse jobs payload missing")
        return [Posting(board, str(j["id"]), j["title"],
                        f"https://job-boards.greenhouse.io/{slug}/jobs/{j['id']}",
                        [(j.get("location") or {}).get("name", "")],
                        html.unescape(re.sub(r"<[^>]+>", " ", j.get("content", ""))))
                for j in raw["jobs"]]
    out = []
    for offset in range(0, 5000, 100):
        raw = get(client, f"https://api.lever.co/v0/postings/{slug}?mode=json&limit=100&skip={offset}").json()
        if not isinstance(raw, list):
            raise ValueError("Lever jobs payload must be a list")
        for j in raw:
            category = j.get("categories") or {}
            out.append(Posting(board, j["id"], j["text"], j["applyUrl"],
                               category.get("allLocations") or [category.get("location", "")],
                               (j.get("descriptionPlain") or "") + " " + " ".join(
                                   html.unescape(re.sub(r"<[^>]+>", " ", x.get("content", "")))
                                   for x in (j.get("lists") or [])),
                               _timestamp(j.get("createdAt")),
                               category.get("commitment", "Full-time").lower() == "full-time"))
        if len(raw) < 100:
            return out
    raise ValueError("Lever pagination exceeded 5000 jobs")


def score_posting(posting: Posting) -> int:
    """Use existing targeting plus positive US evidence and description guards."""
    if not posting.full_time or not any(loc and _US.search(loc) for loc in posting.locations):
        return 0
    if _OFF_PROFILE.search(posting.title) or _RESTRICTED.search(posting.description):
        return 0
    if any(int(m[1]) >= 3 for m in _EXPERIENCE.finditer(posting.description)):
        return 0
    return heuristic_score(Listing(posting.key, posting.board.slug, posting.title, posting.url,
                                   posting.locations, "", "", True, True, posting.posted))


def store(settings: Settings, postings: list[Posting], source: str, *,
          refreshed_boards: list[Board] | None = None) -> tuple[int, int]:
    """Deduplicate against every existing ATS URL before inserting new jobs."""
    settings.ensure_dirs()
    conn = db.connect(settings.db_path)
    new = 0
    try:
        rows = conn.execute("SELECT id,company_name,url,final_url FROM jobs").fetchall()
        known = {key: r["id"] for r in rows
                 for u in (r["url"], r["final_url"]) if u and (key := job_key(u))}
        company_names = {key.rsplit(":", 1)[0]: r["company_name"] for r in reversed(rows)
                         for u in (r["url"], r["final_url"]) if u and (key := job_key(u))}
        submitted = {(r[0].casefold(), r[1].strip().casefold()) for r in conn.execute(
            "SELECT j.company_name,j.title FROM jobs j JOIN applications a ON a.job_id=j.id "
            "WHERE a.status='submitted' OR a.submitted_at IS NOT NULL"
        )}
        embedded_ids = {(ats, value) for r in rows for u in (r["url"], r["final_url"])
                        if u for param, ats in (("gh_jid", "greenhouse"), ("ashby_jid", "ashby"))
                        for value in parse_qs(urlsplit(u).query).get(param, [])}
        now = dt.datetime.now(dt.UTC).isoformat()
        with db.transaction(conn):
            refreshed = {f"{b.ats}:{b.slug.lower()}" for b in (refreshed_boards or [])}
            live = {p.key for p in postings}
            for key, jid in known.items():
                if jid.startswith("startup-") and key.rsplit(":", 1)[0] in refreshed:
                    if key not in live:
                        conn.execute("UPDATE jobs SET active=0,status='inactive',last_seen=? "
                                     "WHERE id=?", (now, jid))
            for p in postings:
                company = company_names.get(p.key.rsplit(":", 1)[0], p.board.slug)
                if (company.casefold(), p.title.strip().casefold()) in submitted:
                    continue
                score = score_posting(p)
                jid = "startup-" + hashlib.sha256(p.key.encode()).hexdigest()[:24]
                if p.key in known and known[p.key] != jid:
                    continue
                if p.key not in known and (p.board.ats, p.id) in embedded_ids:
                    continue
                if score <= 0 and p.key not in known:
                    continue
                result = db.upsert_job(conn, id=jid, company_name=company,
                                       title=p.title, url=p.url, final_url=p.url,
                                       ats=p.board.ats, category=f"startup:{source}",
                                       locations_json=json.dumps(p.locations), score=score,
                                       active=score > 0,
                                       date_posted=p.posted, now_iso=now)
                new += result == "new"
                known[p.key] = jid
    finally:
        conn.close()
    return len(postings), new
