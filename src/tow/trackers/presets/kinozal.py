"""Kinozal: password login, mirrors and a daily torrent-file download limit."""

from __future__ import annotations

import re
from typing import Any

from tow.trackers.presets import SitePreset, UrlParts

_DOWNLOAD = "/download.php?id={id}"


def _is_kinozal(parts: UrlParts) -> bool:
    # Legacy mirror links under /.dl./ remain recognizable.
    return "kinozal" in parts.host or "/.dl." in parts.path


def guess(parts: UrlParts) -> dict[str, Any] | None:
    if not _is_kinozal(parts):
        return None
    query = parts.query
    key = "id" if query.get("id") else "t"
    values = query.get(key) or []
    if len(values) != 1 or not re.fullmatch(r"[0-9]+", values[0]):
        return None
    query_prefix = r"\?(?:[^#\r\n]*&)?"
    topic_query = query_prefix + r"id=(\d+)(?=&|$)"
    if re.search(r"/(?:download|get)\.php$", parts.path, re.IGNORECASE):
        return parts.spec(
            url_regex=parts.rx(
                r"/(?:details\.php" + query_prefix + "id=|"
                r"(?:\.dl\./)?(?:download|get)\.php" + query_prefix + key + r"=)(\d+)(?=&|$)"
            ),
            download_path=_DOWNLOAD,
            login_path="/takelogin.php",
            need_login=True,
        )
    if re.search(r"details\.php$", parts.path, re.IGNORECASE) and query.get("id"):
        return parts.spec(
            url_regex=parts.rx(r"/details\.php" + topic_query),
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
    infohash_path="/get_srv_details.php?id={id}&action=2",
    public_probe_path="/login.php",
    download_path_aliases={"/.dl./download.php?id={id}": _DOWNLOAD},
    maintenance_markers=("ведутся технические работы", "technical works in progress"),
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
