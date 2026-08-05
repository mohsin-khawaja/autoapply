"""Runtime settings: paths, thresholds, allowlists, rate limits.

All tunables live here so adapters and the mapper stay free of magic numbers.
Paths default to repo-local so the tool is self-contained; override the data
directory with ``AUTOAPPLY_HOME``.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path

from autoapply.ats.base import ATSKind

# Repo root = two levels up from this file (src/autoapply/config.py -> repo/).
REPO_ROOT = Path(__file__).resolve().parents[2]

LISTINGS_URL = (
    "https://raw.githubusercontent.com/SimplifyJobs/New-Grad-Positions/"
    "dev/.github/scripts/listings.json"
)


@dataclass(slots=True)
class Settings:
    """Central configuration. Construct via :func:`load_settings`."""

    repo_root: Path = REPO_ROOT
    home: Path = field(default_factory=lambda: _home())
    profile_path: Path = field(default_factory=lambda: REPO_ROOT / "profile.yaml")

    # Ollama
    ollama_host: str = "http://localhost:11434"
    ollama_model: str = "qwen2.5:7b-instruct"
    ollama_fallback_model: str = "llama3.1:8b"

    # Fuzzy matching (rapidfuzz score 0..100) for dropdown/option selection.
    fuzzy_threshold: int = 82

    # Rate limiting for run (SPEC §10): jittered seconds between applications.
    rate_min_seconds: float = 45.0
    rate_max_seconds: float = 90.0
    max_per_run: int = 15

    # Hard wall-clock cap per application. Past this, the job is abandoned (cached
    # to the dashboard as needs_input) and the run moves on — throughput over a
    # single slow form. 0 disables the cap.
    per_job_seconds: float = 120.0

    # Auto-submit is OFF by default; only ATSs in this set may be auto-submitted.
    auto_submit_allowlist: set[ATSKind] = field(default_factory=set)

    @property
    def db_path(self) -> Path:
        return self.home / "autoapply.db"

    @property
    def browser_data_dir(self) -> Path:
        return self.home / "browser_data"

    @property
    def runs_dir(self) -> Path:
        return self.repo_root / "runs"

    def ensure_dirs(self) -> None:
        """Create the data/browser/runs directories if missing."""
        for d in (self.home, self.browser_data_dir, self.runs_dir):
            d.mkdir(parents=True, exist_ok=True)


def _home() -> Path:
    env = os.environ.get("AUTOAPPLY_HOME")
    return Path(env).expanduser().resolve() if env else (REPO_ROOT / ".autoapply")


def load_settings() -> Settings:
    """Build settings from defaults + environment. Cheap; call freely."""
    s = Settings()
    if model := os.environ.get("AUTOAPPLY_MODEL"):
        s.ollama_model = model
    if host := os.environ.get("AUTOAPPLY_OLLAMA_HOST"):
        s.ollama_host = host
    if allow := os.environ.get("AUTOAPPLY_AUTO_SUBMIT"):
        # comma-separated ATS kinds, e.g. "greenhouse,lever" (SPEC §10 allowlist)
        s.auto_submit_allowlist = {
            ATSKind(k.strip().lower()) for k in allow.split(",") if k.strip()
        }
    return s
