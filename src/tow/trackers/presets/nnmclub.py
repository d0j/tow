"""NNM-Club: the login is behind Cloudflare Turnstile, so it is done in a real browser window;
the .torrent link is found on the topic page and served from bulk.nnmclub.to."""

from __future__ import annotations

import re
from typing import Any

from tow.trackers.presets import SitePreset, UrlParts

_DOWNLOAD_HOSTS = ["https://bulk.nnmclub.to"]
_DOWNLOAD_HREF = r"download\.php\?id=(\d+)"


def guess(parts: UrlParts) -> dict[str, Any] | None:
    """A pasted NNM-Club link: a forum whose login needs a browser window and whose .torrent
    comes from bulk.nnmclub.to - a plain forum guess gave a site that could do neither."""
    if not re.search(r"(?:^|\.)nnm-?club\.", parts.host) or not re.search(
        r"/(?:viewtopic|download)\.php$", parts.path, re.IGNORECASE
    ):
        return None
    if not (parts.query.get("t") or parts.query.get("id")):
        return None
    spec = parts.phpbb(parts.prefix, dl=f"{parts.prefix}/download.php?id={{id}}", page=True)
    spec.update(
        name="nnmclub",
        browser_auth=True,
        download_redirect_hosts=list(_DOWNLOAD_HOSTS),
        download_href_regex=_DOWNLOAD_HREF,
    )
    return spec


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
        "download_redirect_hosts": list(_DOWNLOAD_HOSTS),
        "download_href_regex": _DOWNLOAD_HREF,
        "page_download": True,
        "cookie_names": [],
        "fail_threshold": 3,
        "cooldown_sec": 3600,
    },
    guess=guess,
)
