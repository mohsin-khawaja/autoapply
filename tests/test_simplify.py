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
    """"Software Engineer I" is entry level, so the new-grad boost applies."""
    swe = simplify.Listing.from_feed(feed_listings[1])  # SWE I, NYC
    score = simplify.heuristic_score(swe)
    assert score >= 70, score


def test_entry_level_outranks_an_equally_relevant_mid_level_title():
    """The whole point: a new-grad req must beat a plain mid-level one."""

    def listing(title: str) -> simplify.Listing:
        return simplify.Listing(
            id="x", company_name="Acme", title=title, url="https://x",
            locations=["San Francisco, CA"], sponsorship="", category="eng",
            active=True, is_visible=True, date_posted=None,
        )

    new_grad = simplify.heuristic_score(listing("Machine Learning Engineer, New Grad"))
    mid = simplify.heuristic_score(listing("Machine Learning Engineer"))
    assert new_grad > mid, (new_grad, mid)


def test_score_excludes_out_of_reach_levels():
    """Levels above new grad must be hard zeros, not merely downranked."""

    def score(title: str) -> int:
        return simplify.heuristic_score(
            simplify.Listing(
                id="x", company_name="Acme", title=title, url="https://x",
                locations=["San Francisco, CA"], sponsorship="", category="eng",
                active=True, is_visible=True, date_posted=None,
            )
        )

    for title in (
        "Sr. Software Engineer", "Staff ML Engineer", "Principal Engineer",
        "Software Engineer III", "Software Engineer II", "Head of Engineering",
        "Engineering Manager", "Software Architect", "Distinguished Engineer",
        "ML Engineer (5+ years)", "Software Engineering Intern",
    ):
        assert score(title) == 0, title
    # ...while genuine entry-level titles survive.
    for title in ("Software Engineer I", "New Grad Software Engineer", "Junior ML Engineer"):
        assert score(title) >= 70, title


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
