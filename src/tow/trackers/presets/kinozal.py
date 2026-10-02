"""Kinozal: password login, mirrors, the .torrent under /.dl./ and a daily download limit."""

from __future__ import annotations

import re
from typing import Any

from tow.trackers.presets import SitePreset, UrlParts

_DOWNLOAD = "/.dl./download.php?id={id}"


def _is_kinozal(parts: UrlParts) -> bool:
    # A mirror under another name still serves the .torrent from its /.dl./ path.
    return "kinozal" in parts.host or "/.dl." in parts.path


def guess(parts: UrlParts) -> dict[str, Any] | None:
    if not _is_kinozal(parts):
        return None
    query = parts.query
    if re.search(r"/(?:download|get)\.php$", parts.path, re.IGNORECASE) and (query.get("id") or query.get("t")):
        return parts.spec(
            url_regex=parts.rx(r"/(?:details\.php|\.dl\./download\.php|download\.php)\?id=(\d+)"),
            download_path=_DOWNLOAD,
            login_path="/takelogin.php",
            need_login=True,
        )
    if re.search(r"details\.php$", parts.path, re.IGNORECASE) and query.get("id"):
        return parts.spec(
            url_regex=parts.rx(r"/details\.php\?id=(\d+)"),
            download_path=_DOWNLOAD,
            login_path="/takelogin.php",
            need_login=True,
        )
    return None


PRESET = SitePreset(
    name="kinozal",
    label="Kinozal",
    brands=("kinozal",),
    search_path="/browse.php?s={q}",
    daily_limit=True,
    spec={
        "title": "Kinozal",
        "url_regex": (
            r"^https?://(?:kinozal\.(?:tv|me|guru)|kinozal\.jumpingcrab\.com|kinozal\.cloudns\.nz)"
            r"/details\.php\?id=(\d+)$"
        ),
        "login_hosts": ["https://kinozal.guru", "https://kinozal.jumpingcrab.com", "https://kinozal.cloudns.nz"],
        "fetch_hosts": ["https://kinozal.guru", "https://kinozal.jumpingcrab.com", "https://kinozal.cloudns.nz"],
        "login_path": "/takelogin.php",
        "download_path": _DOWNLOAD,
        "cookie_names": ["uid", "pass"],
        "fail_threshold": 3,
        "cooldown_sec": 3600,
    },
    guess=guess,
)
