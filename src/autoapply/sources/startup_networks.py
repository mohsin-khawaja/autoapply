"""Requested startup networks: public discovery and explicit access status.

Company lists identify employers, not vacancies. Only current public ATS API
postings enter the jobs table. No sign-in, subscriptions, or messages are sent.
"""
from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass
from urllib.parse import urlsplit

import httpx

from autoapply.config import Settings
from autoapply.sources import startup_boards as boards


@dataclass(frozen=True)
class Source:
    name: str
    url: str
    mode: str
    note: str


CATALOG = (
    Source("ycombinator", "https://www.ycombinator.com/jobs", "existing",
           "Public feed; founder messages stay manual."),
    Source("lennys", "https://www.lennysjobs.com/lenny100", "companies",
           "Lenny 100 public company list; personalized account features need sign-in."),
    Source("breakout", "https://breakoutlist.com/", "companies",
           "Public company directory to linked careers pages and ATS boards."),
    Source("hackernews", "https://hn.algolia.com/api", "hn",
           "Latest official Who is hiring thread; top-level comments only."),
    Source("foundersysk", "https://newsletter.foundersysk.com/feed", "links",
           "Public newsletter links; invite-only events and intros stay manual."),
    Source("ramp", "https://ramp.com/vendors", "research",
           "Vendor rankings are research leads, not job vacancies; add verified company URLs."),
    Source("a16zbuild", "https://a16zjobs.substack.com/feed", "links",
           "Public Build newsletter and a16z portfolio board."),
    Source("nextplay", "https://nextplay.so/lists", "lists",
           "Public startup lists; paid services and community sign-in stay manual."),
    Source("cosign", "https://cosign.co/jobs", "links",
           "Public embedded ATS links; personal endorsements/profile require sign-in."),
)
ACTIVE = tuple(s.name for s in CATALOG if s.mode in {"companies", "links", "lists"})
_SKIP_HOSTS = {
    "x.com", "twitter.com", "linkedin.com", "www.linkedin.com", "youtube.com",
    "www.youtube.com", "schema.org", "fonts.googleapis.com", "fonts.gstatic.com",
    "www.googletagmanager.com", "substack.com", "www.facebook.com",
}


def company_links(text: str, origin: str) -> list[str]:
    """Only external root links, not social accounts, assets, or arbitrary paths."""
    origin_host = urlsplit(origin).hostname
    return [u for u in boards.links(text, origin)
            if urlsplit(u).hostname not in _SKIP_HOSTS | {origin_host}
            and urlsplit(u).path in {"", "/"} and not urlsplit(u).query
            and not urlsplit(u).hostname.endswith(".substack.com")]


def seeds(client: httpx.Client, source: Source) -> list[str]:
    response = boards.get(client, source.url)
    text = response.text
    if source.name == "lennys":
        match = re.search(r'<script id="__NEXT_DATA__" type="application/json">(.*?)</script>',
                          text, re.S)
        if not match:
            raise ValueError("Lenny 100 public data missing")
        companies = json.loads(match[1])["props"]["pageProps"]["tagData"]["companies"]
        return ["https://" + c["normalized_domain"] for c in companies
                if c.get("normalized_domain")]
    if source.mode == "companies":
        return company_links(text, str(response.url))
    urls = boards.links(text, str(response.url))
    if source.mode == "lists":
        lists = [u for u in urls if urlsplit(u).hostname == urlsplit(source.url).hostname
                 and urlsplit(u).path.startswith("/lists/")]
        if not lists:
            raise ValueError("Next Play list links missing")
        out = []
        for url in lists[:10]:
            response = boards.get(client, url)
            out.extend(company_links(response.text, str(response.url)))
        return list(dict.fromkeys(out))
    if source.name == "a16zbuild":
        response = boards.get(client, "https://jobs.a16z.com/")
        urls.extend(boards.links(response.text, str(response.url)))
    direct = [u for u in urls if boards.board_from_url(u)]
    if source.name == "foundersysk":
        direct.extend(company_links(text, str(response.url)))
    return list(dict.fromkeys(direct))


def scan(settings: Settings, source: str, urls: list[str], *, client: httpx.Client,
         max_companies: int = 20) -> tuple[int, int]:
    """Rotate a bounded company batch; retain a report of unresolved leads."""
    if not urls:
        raise ValueError(f"{source}: no public company or ATS links found")
    report_path = settings.home / "startup-sources" / f"{source}.json"
    old = json.loads(report_path.read_text()) if report_path.exists() else {}
    offset = int(old.get("next_offset", 0)) % len(urls)
    selected = (urls[offset:] + urls[:offset])[:max_companies]
    found: set[boards.Board] = set()
    errors: list[dict[str, str]] = []
    unresolved = []
    for url in selected:
        try:
            resolved = boards.resolve_boards(client, url)
            found.update(resolved)
            if not resolved:
                unresolved.append(url)
        except (httpx.HTTPError, ValueError) as exc:
            errors.append({"url": url, "error": str(exc)[:180]})
    postings = []
    successful: list[boards.Board] = []
    for board in sorted(found, key=lambda b: (b.ats, b.slug)):
        try:
            postings.extend(boards.fetch_board(client, board))
            successful.append(board)
        except (httpx.HTTPError, ValueError, KeyError, TypeError) as exc:
            errors.append({"url": f"{board.ats}/{board.slug}", "error": str(exc)[:180]})
    fetched, new = boards.store(settings, postings, source, refreshed_boards=successful)
    report_path.parent.mkdir(parents=True, exist_ok=True)
    report_path.write_text(json.dumps({
        "source": source, "seed_count": len(urls), "scanned": len(selected),
        "next_offset": (offset + len(selected)) % len(urls),
        "boards": [asdict(b) for b in sorted(found, key=lambda b: (b.ats, b.slug))],
        "boards_fetched": len(successful), "fetched": fetched, "new": new,
        "unresolved_company_urls": unresolved, "errors": errors,
    }, indent=2) + "\n")
    if not successful:
        raise ValueError(f"{source}: no ATS board fetched; see {report_path}")
    return fetched, new


def sync(settings: Settings, name: str) -> tuple[int, int]:
    source = next(s for s in CATALOG if s.name == name)
    with httpx.Client(timeout=12.0, headers={"user-agent": "autoapply/0.1"}) as client:
        urls = seeds(client, source)
        # Optional explicit company URLs, useful for vendor reports or newsletters.
        extra = settings.repo_root / "startup_companies.json"
        if extra.exists():
            urls.extend(json.loads(extra.read_text()).get(name, []))
        return scan(settings, name, list(dict.fromkeys(urls)), client=client)


def print_access(settings: Settings) -> None:
    """Account setup inventory without reading browser cookie values."""
    from autoapply.profile import load_profile

    profile = load_profile(settings.profile_path) if settings.profile_path.exists() else None
    print(f"Profile: {settings.profile_path}")
    print(f"Persistent browser: {settings.browser_data_dir}")
    print("Profile links:")
    if profile is None:
        print("  profile.yaml not supplied")
    else:
        for key, value in profile.identity.links.model_dump().items():
            print(f"  {key}: {value or 'not supplied'}")
    from autoapply.board_access import bookmarks

    print("Board access (not profile URLs):")
    for name, url in bookmarks(settings).items():
        print(f"  {name}: {url}")
    print("Sign in with: autoapply board-open <name> (stop away first)")
    print("YC founder messages and Handshake applications remain manual.")
    print("Startup sources:")
    for source in CATALOG:
        print(f"  {source.name} [{source.mode}] {source.url}\n    {source.note}")
    print("Portal sign-in: autoapply open <job-id> in this persistent browser.")
    print("Workday accounts are employer-specific. Cookie presence is not proof of login.")


if __name__ == "__main__":
    from autoapply.config import load_settings

    print_access(load_settings())
