"""YC Work at a Startup (WaaS) sourcing + auth (waas-addendum.md E1).

WaaS roles are messaged, not form-filled, but they live in the same ``jobs`` table
(``ats="waas"``) and reuse the SimplifyJobs scoring so ``list``/``queue`` work
unchanged.

SAFETY: login is manual-only. ``login()`` opens the persistent browser and waits
for the human to sign in — this module never reads, types, stores, or logs a
password (waas-addendum.md E1). On logged-out detection mid-run the caller pauses
and surfaces the browser.

``parse_role_page`` is a pure function so it is unit-testable against saved HTML
fixtures without a browser. Selectors target ``data-testid`` first with heading
fallbacks; refine against real captured pages in ``tests/fixtures/``.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from html.parser import HTMLParser
from pathlib import Path

from autoapply.config import Settings
from autoapply.sources import simplify

WAAS_HOST = "workatastartup.com"
WAAS_BASE = f"https://www.{WAAS_HOST}"
WAAS_LOGIN_URL = f"{WAAS_BASE}/login"
WAAS_ATS = "waas"  # stored in jobs.ats (TEXT); no ATSKind enum member needed.

# data-testid keys the parser extracts. Real WaaS DOM will differ — update here
# once real pages are captured; tests pin the contract.
_TESTIDS = {
    "company-name": "company",
    "company-tagline": "one_liner",
    "company-batch": "batch",
    "role-title": "title",
    "tech-stack": "tech_stack",
    "compensation": "compensation",
    "location": "location",
    "job-description": "description",
}


@dataclass(slots=True)
class WaasRole:
    """A scraped WaaS role. ``role_id`` is the message-cache key (per company+role)."""

    role_id: str
    url: str
    company: str
    title: str
    one_liner: str = ""
    batch: str = ""
    tech_stack: str = ""
    compensation: str = ""
    location: str = ""
    description: str = ""
    locations: list[str] = field(default_factory=list)


class _TestIdExtractor(HTMLParser):
    """Capture inner text of elements carrying a targeted ``data-testid``.

    Handles nesting by tracking open-tag depth for the active capture. Good enough
    for rendered SPA HTML saved via ``page.content()``; not a full DOM.
    """

    def __init__(self, wanted: dict[str, str]) -> None:
        super().__init__()
        self._wanted = wanted
        self.found: dict[str, str] = {}
        self._active_field: str | None = None
        self._depth = 0
        self._buf: list[str] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        ad = {k: (v or "") for k, v in attrs}
        testid = ad.get("data-testid", "")
        if self._active_field is None and testid in self._wanted:
            self._active_field = self._wanted[testid]
            self._depth = 1
            self._buf = []
        elif self._active_field is not None:
            self._depth += 1

    def handle_endtag(self, tag: str) -> None:
        if self._active_field is None:
            return
        self._depth -= 1
        if self._depth <= 0:
            text = re.sub(r"\s+", " ", "".join(self._buf)).strip()
            # first occurrence wins (headings appear before repeats)
            self.found.setdefault(self._active_field, text)
            self._active_field = None
            self._buf = []

    def handle_data(self, data: str) -> None:
        if self._active_field is not None:
            self._buf.append(data)


def parse_role_page(html: str, *, url: str = "", role_id: str = "") -> WaasRole:
    """Parse a WaaS role page HTML into a :class:`WaasRole`. Pure; fixture-testable."""
    ex = _TestIdExtractor(_TESTIDS)
    ex.feed(html)
    f = ex.found

    company = f.get("company", "") or _first_heading(html, "h1")
    title = f.get("title", "") or _first_heading(html, "h2")
    location = f.get("location", "")
    rid = role_id or _role_id_from_url(url) or _slug(f"{company}-{title}")

    return WaasRole(
        role_id=rid,
        url=url,
        company=company,
        title=title,
        one_liner=f.get("one_liner", ""),
        batch=f.get("batch", ""),
        tech_stack=f.get("tech_stack", ""),
        compensation=f.get("compensation", ""),
        location=location,
        description=f.get("description", ""),
        locations=[location] if location else [],
    )


def _first_heading(html: str, tag: str) -> str:
    m = re.search(rf"<{tag}[^>]*>(.*?)</{tag}>", html, re.IGNORECASE | re.DOTALL)
    if not m:
        return ""
    return re.sub(r"\s+", " ", re.sub(r"<[^>]+>", "", m.group(1))).strip()


def _role_id_from_url(url: str) -> str:
    m = re.search(r"/(?:jobs|companies)/([\w-]+)", url)
    return m.group(1) if m else ""


def _slug(text: str) -> str:
    return re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")


# ---- scoring reuse --------------------------------------------------------


def score_role(role: WaasRole) -> int:
    """Score a WaaS role by adapting it into a :class:`simplify.Listing`.

    Reuses ``simplify.heuristic_score`` (title/location keyword tiers) so WaaS and
    SimplifyJobs listings rank on the same scale — no scoring logic duplicated.
    """
    listing = simplify.Listing(
        id=role.role_id,
        company_name=role.company,
        title=role.title,
        url=role.url,
        locations=role.locations,
        sponsorship="",
        category=role.batch or "WaaS",
        active=True,
        is_visible=True,
        date_posted=None,
    )
    return simplify.heuristic_score(listing)


# ---- dedupe ---------------------------------------------------------------


def cache_role(conn, role: WaasRole, now_iso: str) -> None:
    """Persist a scraped role (incl. description) so run/dry-run needn't re-scrape."""
    import json
    from dataclasses import asdict

    from autoapply import db

    db.put_answer(
        conn,
        question_hash=f"waasrole:{role.role_id}",
        company=role.company,
        question="waas-role-json",
        answer=json.dumps(asdict(role)),
        now_iso=now_iso,
    )


def load_role(conn, role_id: str) -> WaasRole | None:
    """Load a cached role by id, or None."""
    import json

    row = conn.execute(
        "SELECT answer FROM answers WHERE question_hash = ?", (f"waasrole:{role_id}",)
    ).fetchone()
    return WaasRole(**json.loads(row[0])) if row else None


def messaged_companies(conn) -> set[str]:
    """Companies already messaged (any role) — founders see all messages (E1 dedupe)."""
    rows = conn.execute(
        """SELECT DISTINCT j.company_name
           FROM applications a JOIN jobs j ON j.id = a.job_id
           WHERE j.ats = ? AND a.status IN ('messaged', 'replied')""",
        (WAAS_ATS,),
    ).fetchall()
    return {r[0] for r in rows}


# ---- auth (manual only) ---------------------------------------------------


def is_logged_in(page) -> bool:
    """Best-effort logged-in check: an avatar/account control present, no /login."""
    try:
        if "/login" in (page.url or ""):
            return False
        # WaaS shows an account menu when authed; refine against real DOM.
        return page.locator("[data-testid='account-menu'], a[href*='/messages']").count() > 0
    except Exception:  # noqa: BLE001 - detection must not raise
        return False


def login(settings: Settings) -> bool:
    """Open the persistent browser at the WaaS login page and wait for manual login.

    Never handles credentials. Returns True once ``is_logged_in`` becomes true, or
    False if the human closes the window / times out. Blocks on human input.
    """
    from autoapply.browser import launch_context

    settings.ensure_dirs()
    with launch_context(settings.browser_data_dir, headed=True) as (_ctx, page):
        page.goto(WAAS_LOGIN_URL, wait_until="domcontentloaded")
        print("Log in to Work at a Startup in the browser window. Waiting…")
        try:
            # Poll until authed or the page is closed by the human.
            for _ in range(600):  # ~10 min at 1s
                if is_logged_in(page):
                    print("Detected login. You can close the window.")
                    return True
                page.wait_for_timeout(1000)
        except Exception:  # noqa: BLE001 - window closed / navigation
            return is_logged_in(page)
    return False


# ---- sync -----------------------------------------------------------------

WAAS_FEED_URL = f"{WAAS_BASE}/companies"  # respects the user's saved filters when authed


@dataclass(slots=True)
class WaasSyncResult:
    scraped: int
    upserted: int
    skipped_messaged: int
    scored_ge_70: int


def _collect_role_urls(page) -> list[str]:
    """Return distinct role/company page URLs from the current feed DOM."""
    hrefs = page.eval_on_selector_all(
        "a[href*='/companies/'], a[href*='/jobs/']",
        "els => els.map(e => e.href)",
    )
    seen: list[str] = []
    for h in hrefs:
        if h not in seen and re.search(r"/(companies|jobs)/[\w-]+", h):
            seen.append(h)
    return seen


def sync(settings: Settings, *, max_roles: int = 60) -> WaasSyncResult:
    """Scrape the authed WaaS feed into the ``jobs`` table (waas-addendum.md E1).

    Requires a prior ``login``. Dedupes against already-messaged companies. Live
    only — not unit-tested; verified by the acceptance run.
    """
    import datetime as dt
    import json

    from autoapply import db
    from autoapply.browser import launch_context

    settings.ensure_dirs()
    conn = db.connect(settings.db_path)
    now_iso = dt.datetime.now(dt.UTC).isoformat()
    already = messaged_companies(conn)

    scraped = upserted = skipped = scored_hi = 0
    try:
        with launch_context(settings.browser_data_dir, headed=True) as (_ctx, page):
            page.goto(WAAS_FEED_URL, wait_until="networkidle")
            if not is_logged_in(page):
                raise RuntimeError("not logged in — run `autoapply login waas` first")
            urls = _collect_role_urls(page)[:max_roles]
            with db.transaction(conn):
                for url in urls:
                    page.goto(url, wait_until="networkidle")
                    role = parse_role_page(page.content(), url=url)
                    if not role.company or not role.title:
                        continue
                    scraped += 1
                    if role.company in already:
                        skipped += 1
                        continue
                    score = score_role(role)
                    if score >= 70:
                        scored_hi += 1
                    db.upsert_job(
                        conn,
                        id=role.role_id,
                        company_name=role.company,
                        title=role.title,
                        url=role.url,
                        final_url=role.url,
                        ats=WAAS_ATS,
                        category=role.batch or "WaaS",
                        locations_json=json.dumps(role.locations),
                        sponsorship="",
                        active=True,
                        is_visible=True,
                        score=score,
                        date_posted=None,
                        now_iso=now_iso,
                    )
                    cache_role(conn, role, now_iso)
                    upserted += 1
    finally:
        conn.close()
    return WaasSyncResult(scraped, upserted, skipped, scored_hi)


# ---- capture aid ----------------------------------------------------------


def dump_html(settings: Settings, url: str, out_dir: Path) -> Path:
    """Save a live role page's rendered HTML to ``out_dir`` for fixture capture.

    Requires a prior manual login in the persistent context. Never stores creds.
    """
    from autoapply.browser import launch_context

    out_dir.mkdir(parents=True, exist_ok=True)
    with launch_context(settings.browser_data_dir, headed=True) as (_ctx, page):
        page.goto(url, wait_until="networkidle")
        if not is_logged_in(page):
            raise RuntimeError("not logged in — run `autoapply login waas` first")
        name = (_role_id_from_url(url) or "role") + ".html"
        dest = out_dir / name
        dest.write_text(page.content())
        return dest
