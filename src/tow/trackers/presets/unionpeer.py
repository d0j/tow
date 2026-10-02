"""UnionPeer: a TorrentPier forum; the .torrent link is found on the topic page."""

from __future__ import annotations

from tow.trackers.presets import SitePreset

PRESET = SitePreset(
    name="unionpeer",
    brands=("unionpeer",),
    search_path="/tracker.php?nm={q}",
    spec={
        "title": "UnionPeer",
        "url_regex": r"^https?://(?:www\.)?unionpeer\.org/(?:forum/)?viewtopic\.php\?t=(\d+)",
        "login_hosts": ["https://unionpeer.org"],
        "fetch_hosts": ["https://unionpeer.org"],
        "login_path": "/login.php",
        "login_form": {"user_field": "login_username", "pw_field": "login_password", "extra": {"login": "Вход"}},
        "topic_path": "/viewtopic.php?t={id}",
        "download_path": "/download.php?id={id}",
        "page_download": True,
        "cookie_names": [],
        "fail_threshold": 3,
        "cooldown_sec": 3600,
    },
)
