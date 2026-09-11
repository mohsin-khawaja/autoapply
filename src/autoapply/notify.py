"""Failure notification for unattended runs.

Three independent channels, each best-effort and isolated: a broken notifier
must never fail the run it is reporting on. :func:`notify` returns the channels
that actually delivered so the caller can log what reached the user.

Channel notes:
- ``osascript`` banner: local, but only visible when you are at the Mac.
- ``ntfy.sh``: the only channel that reaches a phone. OFF unless
  ``AUTOAPPLY_NTFY_TOPIC`` is set, because it sends the alert text to a
  third-party server. Keep messages free of profile data — counts and error
  types only, never job or applicant details.
- local ``mail``: delivers to /var/mail/$USER. Readable on the Mac only;
  without an SMTP relay configured it does not reach an external inbox.
"""

from __future__ import annotations

import shutil
import subprocess

import httpx

from autoapply.config import Settings

NTFY_HOST = "https://ntfy.sh"


def notify(title: str, message: str, settings: Settings) -> list[str]:
    """Send ``message`` on every configured channel. Returns those that worked."""
    delivered: list[str] = []
    for name, fn in (
        ("macos", _macos),
        ("ntfy", _ntfy),
        ("mail", _mail),
    ):
        try:
            if fn(title, message, settings):
                delivered.append(name)
        except Exception:  # noqa: BLE001 - a notifier must never break the caller
            continue
    return delivered


def _macos(title: str, message: str, _settings: Settings) -> bool:
    if not shutil.which("osascript"):
        return False
    # Quotes are stripped rather than escaped: this string is interpolated into
    # AppleScript, and the inputs are our own error strings, not user content.
    safe_t = title.replace('"', "").replace("\\", "")
    safe_m = message.replace('"', "").replace("\\", "")
    subprocess.run(
        ["osascript", "-e", f'display notification "{safe_m}" with title "{safe_t}"'],
        check=True, capture_output=True, timeout=10,
    )
    return True


def _ntfy(title: str, message: str, settings: Settings) -> bool:
    topic = (settings.ntfy_topic or "").strip()
    if not topic:
        return False  # opt-in: nothing leaves the machine without a topic
    resp = httpx.post(
        f"{NTFY_HOST}/{topic}",
        content=message.encode(),
        headers={"Title": title, "Priority": "high", "Tags": "warning"},
        timeout=10.0,
    )
    resp.raise_for_status()
    return True


def _mail(title: str, message: str, _settings: Settings) -> bool:
    mail = shutil.which("mail")
    if not mail:
        return False
    user = _local_user()
    if not user:
        return False
    subprocess.run(
        [mail, "-s", title, user],
        input=message.encode(), check=True, capture_output=True, timeout=15,
    )
    return True


def _local_user() -> str:
    import getpass

    try:
        return getpass.getuser()
    except Exception:  # noqa: BLE001 - no controlling terminal under launchd
        return ""
