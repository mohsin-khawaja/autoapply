"""Public startup discovery: targeting, source boundaries, dedupe, and failures."""
from __future__ import annotations

import datetime as dt
import json
from pathlib import Path

import httpx
import pytest

from autoapply import db, discovery
from autoapply.config import Settings
from autoapply.sources import hackernews
from autoapply.sources import startup_boards as boards
from autoapply.sources import startup_networks as networks


def settings(tmp_path: Path) -> Settings:
    return Settings(repo_root=tmp_path, home=tmp_path / "data")


def posting(**kwargs: object) -> boards.Posting:
    data = dict(board=boards.Board("ashby", "acme"), id="abc", title="Junior AI Engineer",
                url="https://jobs.ashbyhq.com/acme/abc/application",
                locations=["San Francisco, CA"], description="Build models.")
    data.update(kwargs)
    return boards.Posting(**data)


@pytest.mark.parametrize("url", ["http://127.0.0.1/jobs", "file:///etc/passwd",
                                "https://user:password@example.com/jobs",
                                "http://169.254.169.254/jobs", "http://host.local/jobs"])
def test_untrusted_nonpublic_urls_rejected(url: str) -> None:
    assert not boards.public_url(url)


def test_redirect_to_nonpublic_host_is_not_followed() -> None:
    requests = []

    def handler(request: httpx.Request) -> httpx.Response:
        requests.append(request)
        return httpx.Response(302, headers={"location": "http://127.0.0.1/jobs"})

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        with pytest.raises(ValueError, match="public"):
            boards.get(client, "https://example.com/careers")
    assert len(requests) == 1


def test_ats_domains_exact_and_urls_canonical() -> None:
    assert boards.board_from_url("https://jobs.ashbyhq.com.evil.com/acme") is None
    assert boards.job_key("https://jobs.lever.co/acme/123/apply?source=hn") == "lever:acme:123"
    assert boards.job_key("https://boards.greenhouse.io/acme/jobs/123?gh_src=x") == (
        boards.job_key("https://job-boards.greenhouse.io/acme/jobs/123"))
    assert boards.board_from_url("https://boards.greenhouse.io/embed/job_board?for=acme") == (
        boards.Board("greenhouse", "acme"))


def test_html_entities_and_embedded_ats_links() -> None:
    text = '<a href="https:&#x2F;&#x2F;jobs.lever.co&#x2F;acme">Jobs</a>'
    text += '<script>"https://jobs.ashbyhq.com/acme/abc"</script>'
    assert set(boards.links(text, "https://example.com")) == {
        "https://jobs.lever.co/acme", "https://jobs.ashbyhq.com/acme/abc"}


@pytest.mark.parametrize("changes", [
    {"title": "Senior AI Engineer"}, {"title": "Software Engineer II"},
    {"title": "AI Trainer - Bosnian"}, {"title": "Hardware Test Engineer"},
    {"title": "Research Scientist (PhD)"}, {"locations": ["Remote"]},
    {"locations": ["London, UK"]}, {"description": "Requires active security clearance."},
    {"description": "Requires a PhD."},
    {"description": "3+ years of professional experience required."}, {"full_time": False},
])
def test_targeting_excludes_unsupported_roles(changes: dict) -> None:
    assert boards.score_posting(posting(**changes)) == 0


def test_targeting_uses_existing_whitelist() -> None:
    assert boards.score_posting(posting()) >= 70
    assert boards.score_posting(posting(title="Office Assistant")) == 0


def test_cross_source_dedupe_preserves_application(tmp_path: Path) -> None:
    s = settings(tmp_path)
    s.ensure_dirs()
    conn = db.connect(s.db_path)
    with db.transaction(conn):
        db.upsert_job(conn, id="simplify-original", company_name="Acme", title="Junior AI Engineer",
                      url="https://jobs.ashbyhq.com/acme/abc?embed=true", now_iso="2026-10-01")
        db.record_application(conn, job_id="simplify-original", status="submitted")
    conn.close()
    assert boards.store(s, [posting()], "hackernews") == (1, 0)
    conn = db.connect(s.db_path)
    assert conn.execute("SELECT count(*) FROM jobs").fetchone()[0] == 1
    assert conn.execute("SELECT status FROM applications").fetchone()[0] == "submitted"
    conn.close()


def test_repeated_discovery_is_idempotent_and_reactivates_own_jobs(tmp_path: Path) -> None:
    s = settings(tmp_path)
    assert boards.store(s, [posting(), posting()], "hackernews") == (2, 1)
    conn = db.connect(s.db_path)
    conn.execute("UPDATE jobs SET active=0")
    conn.commit()
    conn.close()
    assert boards.store(s, [posting()], "cosign") == (1, 0)
    conn = db.connect(s.db_path)
    assert conn.execute("SELECT count(*) FROM jobs WHERE active=1").fetchone()[0] == 1
    conn.close()


def test_resolver_follows_careers_only() -> None:
    visited = []

    def handler(request: httpx.Request) -> httpx.Response:
        visited.append(str(request.url))
        if request.url.path == "/":
            return httpx.Response(200, text='<a href="/careers">Join</a>'
                                  '<a href="/login">Login</a><a href="https://other.com/jobs">x</a>')
        return httpx.Response(200, text='<a href="https://jobs.ashbyhq.com/acme">Roles</a>')

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        assert boards.resolve_boards(client, "https://acme.com/") == {boards.Board("ashby", "acme")}
    assert visited == ["https://acme.com/", "https://acme.com/careers"]


def test_hn_uses_official_thread_and_ignores_replies() -> None:
    now = int(dt.datetime.now(dt.UTC).timestamp())
    hits = [{"objectID": "wrong", "title": "Ask HN: Who is hiring? (October 2026)",
             "author": "imposter", "created_at_i": now},
            {"objectID": "42", "title": "Ask HN: Who is hiring? (October 2026)",
             "author": "whoishiring", "created_at_i": now}]
    comment = {"type": "comment", "text": '<a href="https://jobs.lever.co/acme/123">Job</a>',
               "children": [{"type": "comment", "text":
                             '<a href="https://jobs.lever.co/reply/123">Spam</a>'}]}

    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("search_by_date"):
            assert request.url.params["tags"] == "story,author_whoishiring"
            return httpx.Response(200, json={"hits": hits})
        assert request.url.path.endswith("/42")
        return httpx.Response(200, json={"children": [comment]})

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        _, urls = hackernews.fetch_seeds(client)
    assert urls == ["https://jobs.lever.co/acme/123"]


def test_hn_stale_thread_fails() -> None:
    payload = {"hits": [{"objectID": "1", "title": "Ask HN: Who is hiring? (January 2020)",
                          "author": "whoishiring", "created_at_i": 1577836800}]}
    with httpx.Client(transport=httpx.MockTransport(
            lambda r: httpx.Response(200, json=payload))) as client:
        with pytest.raises(ValueError, match="stale"):
            hackernews.fetch_seeds(client)


def test_ashby_hidden_jobs_excluded_and_country_used() -> None:
    visible = {"title": "Junior AI Engineer", "jobUrl": "https://jobs.ashbyhq.com/acme/abc",
               "applyUrl": "https://jobs.ashbyhq.com/acme/abc/application", "location": "Remote",
               "address": {"postalAddress": {"addressCountry": "USA"}},
               "descriptionPlain": "Build software", "employmentType": "FullTime"}
    raw = {"jobs": [visible, {**visible, "isListed": False}]}
    with httpx.Client(transport=httpx.MockTransport(
            lambda r: httpx.Response(200, json=raw))) as client:
        result = boards.fetch_board(client, boards.Board("ashby", "acme"))
    assert len(result) == 1
    assert boards.score_posting(result[0]) > 0


def test_lever_pagination() -> None:
    job = {"id": "id", "text": "Junior Software Engineer",
           "applyUrl": "https://jobs.lever.co/acme/id/apply", "categories": {"location": "NYC"}}
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request.url.params["skip"])
        return httpx.Response(200, json=[job] * 100 if calls == ["0"] else [])

    with httpx.Client(transport=httpx.MockTransport(handler)) as client:
        assert len(boards.fetch_board(client, boards.Board("lever", "acme"))) == 100
    assert calls == ["0", "100"]


def test_source_batch_rotates_and_failures_visible(tmp_path: Path, monkeypatch) -> None:
    s = settings(tmp_path)
    monkeypatch.setattr(boards, "resolve_boards", lambda c, u: set())
    urls = ["https://one.com/", "https://two.com/"]
    with httpx.Client() as client:
        for expected in urls:
            with pytest.raises(ValueError, match="no ATS board fetched"):
                networks.scan(s, "breakout", urls, client=client, max_companies=1)
            report = json.loads((s.home / "startup-sources/breakout.json").read_text())
            assert report["unresolved_company_urls"] == [expected]


def test_all_requested_sources_documented_and_public_sources_registered() -> None:
    assert len(networks.CATALOG) == 9
    for source in networks.CATALOG:
        if source.mode != "research":
            assert source.name in discovery.SOURCES
    assert "ramp" not in discovery.SOURCES


def test_lenny_companies_from_published_payload() -> None:
    payload = {"props": {"pageProps": {"tagData": {"companies": [
        {"normalized_domain": "acme.com"}]}}}}
    text = '<script id="__NEXT_DATA__" type="application/json">'+json.dumps(payload)+'</script>'
    with httpx.Client(transport=httpx.MockTransport(lambda r: httpx.Response(200, text=text))) as c:
        assert networks.seeds(c, next(s for s in networks.CATALOG if s.name == "lennys")) == (
            ["https://acme.com"])


def test_ashby_null_address_is_tolerated() -> None:
    raw = {"jobs": [{"title": "AI Engineer", "jobUrl": "https://jobs.ashbyhq.com/acme/abc",
                     "location": "Remote", "address": None, "secondaryLocations": None,
                     "descriptionPlain": None}]}
    with httpx.Client(transport=httpx.MockTransport(
            lambda r: httpx.Response(200, json=raw))) as client:
        result = boards.fetch_board(client, boards.Board("ashby", "acme"))
    assert len(result) == 1
    assert boards.score_posting(result[0]) == 0


@pytest.mark.parametrize("description", ["5+ years of hands-on Python experience",
                                         "3 years building distributed systems",
                                         "10 years of legal experience"])
def test_experience_requirements_exclude(description: str) -> None:
    assert boards.score_posting(posting(description=description)) == 0


def test_early_career_range_is_not_senior() -> None:
    assert boards.score_posting(posting(description="1-3 years of software experience")) > 0


def test_legal_role_with_ai_keywords_excluded() -> None:
    assert boards.score_posting(posting(title="Associate General Counsel, Machine Learning")) == 0


def test_simplify_does_not_deactivate_startup_jobs(tmp_path: Path, monkeypatch) -> None:
    from autoapply.sources import simplify

    s = settings(tmp_path)
    boards.store(s, [posting()], "hackernews")
    monkeypatch.setattr(simplify, "fetch_listings", lambda: [])
    result = simplify.sync(s)
    assert result.total == 0
    assert result.inactive == 0
    conn = db.connect(s.db_path)
    assert conn.execute("SELECT active FROM jobs").fetchone()[0] == 1
    conn.close()


def test_newsletter_cdata_company_links() -> None:
    text = '<rss><description><![CDATA[<a href="https://acme.com/">Acme</a>]]></description></rss>'
    assert networks.company_links(text, "https://newsletter.example.com/feed") == ["https://acme.com/"]



def test_closed_jobs_retired_only_for_refreshed_boards(tmp_path: Path) -> None:
    s = settings(tmp_path)
    boards.store(s, [posting()], "hackernews")
    boards.store(s, [], "hackernews", refreshed_boards=[boards.Board("lever", "other")])
    conn = db.connect(s.db_path)
    assert conn.execute("SELECT active FROM jobs").fetchone()[0] == 1
    conn.close()
    boards.store(s, [], "hackernews", refreshed_boards=[boards.Board("ashby", "acme")])
    conn = db.connect(s.db_path)
    assert conn.execute("SELECT active FROM jobs").fetchone()[0] == 0
    conn.close()


def test_reposted_submitted_role_not_inserted(tmp_path: Path) -> None:
    s = settings(tmp_path)
    s.ensure_dirs()
    conn = db.connect(s.db_path)
    with db.transaction(conn):
        db.upsert_job(conn, id="old", company_name="ACME Incorporated", title="Junior AI Engineer",
                      url="https://jobs.ashbyhq.com/acme/old-id", now_iso="2026-10-01")
        db.record_application(conn, job_id="old", status="submitted")
    conn.close()
    assert boards.store(s, [posting()], "cosign") == (1, 0)



def test_source_access_cli_without_profile(tmp_path: Path, monkeypatch) -> None:
    from typer.testing import CliRunner

    from autoapply import cli

    s = settings(tmp_path)
    s.profile_path = tmp_path / "missing.yaml"
    monkeypatch.setattr(cli, "load_settings", lambda: s)
    result = CliRunner().invoke(cli.app, ["source-access"])
    assert result.exit_code == 0
    assert "not supplied" in result.stdout
    assert "hackernews" in result.stdout
    assert "employer-specific" in result.stdout


@pytest.mark.parametrize("ats,param", [("ashby", "ashby_jid"), ("greenhouse", "gh_jid")])
def test_custom_career_url_deduped(tmp_path: Path, ats: str, param: str) -> None:
    s = settings(tmp_path)
    s.ensure_dirs()
    conn = db.connect(s.db_path)
    with db.transaction(conn):
        db.upsert_job(conn, id="original", company_name="Acme", title="Junior AI Engineer",
                      url=f"https://acme.com/careers?{param}=abc", now_iso="2026-10-01")
    conn.close()
    p = posting(board=boards.Board(ats, "acme"))
    assert boards.store(s, [p], "cosign") == (1, 0)
