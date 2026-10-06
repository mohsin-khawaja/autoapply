"""HN Who is hiring: official monthly thread -> public employer ATS boards."""
from __future__ import annotations

import datetime as dt
import re
from urllib.parse import urlsplit

import httpx

from autoapply.config import Settings
from autoapply.sources import startup_boards as boards
from autoapply.sources.startup_networks import scan

API = "https://hn.algolia.com/api/v1"
_TITLE = re.compile(r"^Ask HN: Who is hiring\? \([A-Za-z]+ \d{4}\)$")


def fetch_seeds(client: httpx.Client) -> tuple[str, list[str]]:
    response = client.get(API + "/search_by_date", params={
        "tags": "story,author_whoishiring", "query": "Ask HN: Who is hiring?",
        "hitsPerPage": 12,
    })
    response.raise_for_status()
    hits = response.json()["hits"]
    thread = next((h for h in hits if _TITLE.fullmatch(h.get("title", ""))
                   and h.get("author") == "whoishiring"), None)
    if thread is None:
        raise ValueError("official HN hiring thread missing")
    age = dt.datetime.now(dt.UTC).timestamp() - thread["created_at_i"]
    if age > 62 * 86400 or age < -86400:
        raise ValueError("HN hiring thread is stale or future-dated")
    response = client.get(API + "/items/" + thread["objectID"])
    response.raise_for_status()
    payload = response.json()
    # /items includes nested replies. Only direct children are employer posts.
    direct, careers = [], []
    for comment in payload.get("children", []):
        if comment.get("type") != "comment" or not comment.get("text"):
            continue
        for url in boards.links(comment["text"], "https://news.ycombinator.com/"):
            if boards.board_from_url(url):
                direct.append(url)
            elif re.search(r"/(?:careers?|jobs|join-us)(?:/|$)", urlsplit(url).path):
                careers.append(url)
    return thread["title"], list(dict.fromkeys(direct + careers))


def sync(settings: Settings) -> tuple[int, int]:
    with httpx.Client(timeout=15.0, headers={"user-agent": "autoapply/0.1"}) as client:
        _, urls = fetch_seeds(client)
        return scan(settings, "hackernews", urls, client=client, max_companies=30)
