"""Board bookmarks belong to tool access, never applicant identity fields."""

from __future__ import annotations

import json

from autoapply.config import Settings

BOARDS = {
    "yc": "https://www.workatastartup.com/companies",
    "handshake": "https://app.joinhandshake.com/",
    "simplify": "https://github.com/SimplifyJobs/New-Grad-Positions",
}


def bookmarks(settings: Settings) -> dict[str, str]:
    path = settings.home / "board_access.json"
    configured = json.loads(path.read_text()) if path.exists() else {}
    return BOARDS | {key: value for key, value in configured.items() if key in BOARDS}
