"""rutor.info: open (no login), mirrors at rutor.is and new-rutor.org, a CDN at d.rutor.info."""

from __future__ import annotations

import re
from typing import Any

from tow.trackers.presets import SitePreset, UrlParts

_HOST = re.compile(r"^(?:www\.)?(?:d\.)?(?:rutor\.(?:info|is)|new-rutor\.org)$", re.IGNORECASE)
_DOWNLOAD = re.compile(
    r"^https?://(?:www\.)?(?:d\.)?(?:rutor\.(?:info|is)|new-rutor\.org)/download/(\d+)(?:/.*)?$",
    re.IGNORECASE,
)
URL_REGEX = r"^https?://(?:www\.)?(?:(?:d\.)?rutor\.(?:info|is)|new-rutor\.org)/(?:torrent|download)/(\d+)(?:/.*)?$"


def canonical_url(url: str) -> str | None:
    """CDN /download/{id} -> the topic page; d.rutor.info is not a new site."""
    match = _DOWNLOAD.match((url or "").strip())
    return f"http://rutor.info/torrent/{match.group(1)}" if match else None


def guess(parts: UrlParts) -> dict[str, Any] | None:
    if not _HOST.match(parts.host.removeprefix("www.")) or not re.search(r"/(?:torrent|download)/(\d+)", parts.path):
        return None
    return parts.spec(
        name="rutor",
        title="Rutor",
        fetch_hosts="http://rutor.info",
        login_hosts="",
        url_regex=URL_REGEX,
        download_path="/download/{id}",
    )


PRESET = SitePreset(
    name="rutor",
    label="Rutor",
    brands=("rutor", "new-rutor"),
    search_path="/search/0/0/100/0/{q}",
    spec={
        "title": "Rutor",
        "url_regex": URL_REGEX,
        "login_hosts": [],
        "fetch_hosts": ["http://d.rutor.info", "http://rutor.info", "https://new-rutor.org", "http://rutor.is"],
        "login_path": "",
        "download_path": "/download/{id}",
        "cookie_names": [],
        "fail_threshold": 3,
        "cooldown_sec": 1800,
    },
    guess=guess,
    canonical_url=canonical_url,
)
