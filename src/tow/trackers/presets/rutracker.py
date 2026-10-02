"""RuTracker: a TorrentPier forum; the .torrent is at /forum/dl.php?t=<topic>."""

from __future__ import annotations

import re
from typing import Any

from tow.trackers.presets import SitePreset, UrlParts


def guess(parts: UrlParts) -> dict[str, Any] | None:
    if "rutracker" not in parts.host or not re.search(r"viewtopic\.php$", parts.path, re.IGNORECASE):
        return None
    if not parts.query.get("t"):
        return None
    return parts.phpbb(parts.prefix, dl=f"{parts.prefix}/dl.php?t={{id}}", page=False)


PRESET = SitePreset(
    name="rutracker",
    label="RuTracker",
    brands=("rutracker",),
    search_path="/forum/tracker.php?nm={q}",
    spec={
        "title": "RuTracker",
        "url_regex": r"^https?://(?:www\.)?rutracker\.(?:org|net)/forum/viewtopic\.php\?t=(\d+)",
        "login_hosts": ["https://rutracker.org"],
        "fetch_hosts": ["https://rutracker.org", "https://rutracker.net"],
        "login_path": "/forum/login.php",
        "login_form": {"user_field": "login_username", "pw_field": "login_password", "extra": {"login": "Вход"}},
        "download_path": "/forum/dl.php?t={id}",
        "cookie_names": [],
        "fail_threshold": 3,
        "cooldown_sec": 3600,
    },
    guess=guess,
)
