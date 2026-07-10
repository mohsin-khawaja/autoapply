"""ATS classification + heuristic scoring (SPEC.md §3)."""

from __future__ import annotations

from autoapply.ats.base import ATSKind
from autoapply.sources import simplify


def test_classify_ats_hosts():
    cases = {
        "https://jobs.ashbyhq.com/acme/abc": ATSKind.ASHBY,
        "https://boards.greenhouse.io/globex/jobs/1": ATSKind.GREENHOUSE,
        "https://job-boards.greenhouse.io/x/jobs/1": ATSKind.GREENHOUSE,
        "https://jobs.lever.co/initech/xyz": ATSKind.LEVER,
        "https://jobs.smartrecruiters.com/x/1": ATSKind.SMARTRECRUITERS,
        "https://app.rippling.com/x": ATSKind.RIPPLING,
        "https://foo.wd1.myworkdayjobs.com/x": ATSKind.WORKDAY,
        "https://careers.icims.com/x": ATSKind.ICIMS,
        "https://example.com/careers/1": ATSKind.GENERIC,
    }
    for url, kind in cases.items():
        assert simplify.classify_ats(url) is kind, url


def test_workday_is_manual_tier():
    assert simplify.tier_for(ATSKind.WORKDAY) == "manual"
    assert simplify.tier_for(ATSKind.ASHBY) == "auto"


def test_score_high_title_high_location(feed_listings):
    ml = simplify.Listing.from_feed(feed_listings[0])  # ML Eng, SF
    score = simplify.heuristic_score(ml)
    assert score >= 70, score


def test_score_medium_title_medium_location(feed_listings):
    swe = simplify.Listing.from_feed(feed_listings[1])  # SWE I, NYC
    score = simplify.heuristic_score(swe)
    assert 40 <= score < 80, score


def test_score_excludes_senior(feed_listings):
    senior = simplify.Listing.from_feed(feed_listings[2])  # Senior Staff
    assert simplify.heuristic_score(senior) == 0


def test_score_excludes_non_us(feed_listings):
    london = simplify.Listing.from_feed(feed_listings[3])  # London, UK
    assert simplify.heuristic_score(london) == 0


def test_score_inactive_is_zero(feed_listings):
    inactive = simplify.Listing.from_feed(feed_listings[4])
    assert simplify.heuristic_score(inactive) == 0


def test_resolve_redirect_noop_for_direct_url():
    url = "https://jobs.ashbyhq.com/acme/abc"
    assert simplify.resolve_redirect(url) == url
