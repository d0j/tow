"""NNM-Club: the login is behind Cloudflare Turnstile, so it is done in a real browser window;
the .torrent link is found on the topic page and served from bulk.nnmclub.to."""

from __future__ import annotations

from tow.trackers.presets import SitePreset

PRESET = SitePreset(
    name="nnmclub",
    brands=("nnm-?club",),
    search_path="/forum/tracker.php?nm={q}",
    browser_login=True,
    browser_login_hint="site.nnmclub.browser_login",
    spec={
        "title": "NNM-Club",
        "browser_auth": True,
        "url_regex": r"^https?://(?:www\.)?nnmclub\.to/forum/viewtopic\.php\?t=(\d+)",
        "login_hosts": ["https://nnmclub.to"],
        "fetch_hosts": ["https://nnmclub.to"],
        "login_path": "/forum/login.php",
        "topic_path": "/forum/viewtopic.php?t={id}",
        "download_path": "/forum/download.php?id={id}",
        "download_redirect_hosts": ["https://bulk.nnmclub.to"],
        "download_href_regex": r"download\.php\?id=(\d+)",
        "page_download": True,
        "cookie_names": [],
        "fail_threshold": 3,
        "cooldown_sec": 3600,
    },
)
