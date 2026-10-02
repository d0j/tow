"""fast-torrent.ru: open, the topic link is the .torrent link itself."""

from __future__ import annotations

from tow.trackers.presets import SitePreset

PRESET = SitePreset(
    name="fast_torrent",
    brands=("fast-?torrent",),
    spec={
        "title": "fast_torrent",
        "url_regex": r"^https?://(?:www\.)?fast\-torrent\.ru/download/torrent/(\d+)(?:/.*)?",
        "login_hosts": ["http://fast-torrent.ru"],
        "fetch_hosts": ["http://fast-torrent.ru"],
        "login_path": "",
        "download_path": "/download/torrent/{id}",
        "cookie_names": [],
        "fail_threshold": 3,
        "cooldown_sec": 3600,
    },
)
