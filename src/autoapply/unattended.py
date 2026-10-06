"""Single-process launcher with explicit state, preflight, and submit opt-in."""

from __future__ import annotations

import argparse
import fcntl
import os
import subprocess
from pathlib import Path

from autoapply import llm
from autoapply.ats.base import ATSKind
from autoapply.away import away
from autoapply.config import _load_dotenv, load_settings
from autoapply.profile import load_profile


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--home", type=Path, required=True)
    parser.add_argument("--profile", type=Path, required=True)
    parser.add_argument("--env-file", type=Path)
    parser.add_argument("--auto-submit", action="store_true")
    parser.add_argument(
        "--check", action="store_true", help="Preflight only; no browser or applications"
    )
    args = parser.parse_args()
    os.environ["AUTOAPPLY_HOME"] = str(args.home.expanduser().resolve())
    os.environ["AUTOAPPLY_AUTO_SUBMIT"] = ""  # .env cannot silently opt in
    if args.env_file:
        _load_dotenv(args.env_file.expanduser())
    settings = load_settings()
    settings.profile_path = args.profile.expanduser().resolve()
    settings.auto_submit_allowlist = (
        {ATSKind.GREENHOUSE, ATSKind.LEVER, ATSKind.ASHBY} if args.auto_submit else set()
    )
    settings.ensure_dirs()
    profile = load_profile(settings.profile_path)
    resume = profile.documents.resume_path(settings.repo_root)
    if resume is None or not resume.is_file():
        raise SystemExit("Resume missing; fix profile before running")
    processes = subprocess.check_output(["ps", "-axo", "pid=,command="], text=True)
    for row in processes.splitlines():
        if any(marker in row for marker in ("/bin/autoapply away", "/runs/codex_review_away.py")):
            if " -lc " not in row:
                raise SystemExit("Another application run is active; stop it before starting")
    with (settings.home / "away.lock").open("a") as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise SystemExit("Another supervised run owns this database") from None
        ok, message = llm.make_client(settings).health()
        print("Provider:", message, flush=True)
        if not ok:
            raise SystemExit("No healthy answer provider")
        print("Database:", settings.db_path, flush=True)
        print("Auto-submit:", args.auto_submit, flush=True)
        print(
            "Priority: GTM/sales engineering, then FDE/AI/ML, then other eligible roles", flush=True
        )
        if not args.check:
            away(
                settings, batch=40, min_score=1, interval_seconds=2700, auto_submit=args.auto_submit
            )


if __name__ == "__main__":
    main()
