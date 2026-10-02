"""What TOW knows about particular tracker sites: one module per site, found automatically.

A site module (``tow/trackers/presets/<name>.py``) declares ``PRESET = SitePreset(...)``:
its name (the key under ``trackers:`` in config.yaml), how it is called in messages, the
words that name it in a host or a page title, its search link, whether its accounts have a
daily download limit or need a browser to log in, its default settings, and its own rules
for a pasted link. Adding or fixing a site is that one module (and its ``site.<name>.*``
texts in the language files, when it has any); nothing else changes.

Every site works without a preset: a site configured by hand (Settings → Sites) is a plain
``GenericHttpTracker``. A preset only adds defaults - the site's own settings always win.
"""

from __future__ import annotations

import copy
import functools
import importlib
import logging
import pkgutil
import re
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

_LOG = logging.getLogger("tow.trackers")
# Site modules that failed to load: (module name, reason). The others work without them.
SKIPPED: list[tuple[str, str]] = []


@dataclass(frozen=True)
class UrlParts:
    """A pasted topic link, split for the site rules (``SitePreset.guess``)."""

    host: str  # lower case, without the port
    origin: str  # scheme://host[:port]
    path: str
    query: Mapping[str, list[str]]  # parsed
    raw_query: str  # as written
    name: str  # the site name guessed from the host
    host_rx: str  # a regex of scheme-less host[:port], "www." optional

    @property
    def prefix(self) -> str:
        """The path's folder ("/forum" for /forum/viewtopic.php)."""
        return self.path.rsplit("/", 1)[0]

    def rx(self, suffix: str) -> str:
        """A topic-link regex: this host, then ``suffix``."""
        return rf"^https?://{self.host_rx}{suffix}"

    def spec(self, **values: Any) -> dict[str, Any]:
        """New site settings for this host; ``values`` override the plain defaults."""
        spec: dict[str, Any] = {
            "name": self.name,
            "title": self.name,
            "fetch_hosts": self.origin,
            "login_hosts": self.origin,
            "login_path": "",
            "page_download": False,
            "topic_path": "",
            "download_href_regex": "",
            "need_login": False,
        }
        spec.update(values)
        return spec

    def phpbb(self, prefix: str, *, dl: str, page: bool) -> dict[str, Any]:
        """Settings of a phpBB/TorrentPier forum under ``prefix``: topics at viewtopic.php, the
        .torrent at ``dl`` (straight, or found on the topic page when ``page``)."""
        pre = prefix or ""
        login = f"{pre}/login.php" if pre else "/login.php"
        topic = f"{pre}/viewtopic.php?t={{id}}" if pre else "/viewtopic.php?t={id}"
        return self.spec(
            url_regex=self.rx(
                rf"{re.escape(pre)}/(?:viewtopic\.php|dl\.php)\?t=(\d+)"
                if not page
                else rf"{re.escape(pre)}/viewtopic\.php\?t=(\d+)"
            ),
            download_path=dl,
            login_path=login,
            page_download=page,
            topic_path=topic,
            download_href_regex=r"(?:download|dl)\.php\?(?:id|t)=(\d+)" if page else "",
            need_login=True,
        )


@dataclass(frozen=True)
class SitePreset:
    """One tracker site TOW knows by name."""

    name: str  # the key under ``trackers:`` in config.yaml
    # How messages name it ("Kinozal"); "" = the name with a capital letter.
    label: str = ""
    # Regex parts that name the site in a host label or a page title ("nnm-?club").
    brands: tuple[str, ...] = ()
    # Where its search finds a title ({q}); E2: a link only, TOW never fetches results.
    search_path: str = ""
    # Its accounts have a daily .torrent download limit: a preview must not spend it.
    daily_limit: bool = False
    # Its login needs a real browser (a challenge such as Turnstile), not a password POST. Said by
    # its settings (`browser_auth` in `spec`), never forced on a site configured without it.
    browser_login: bool = False
    # The language-file key of what the owner is asked during that browser login.
    browser_login_hint: str = ""
    # The site's settings: a config without a trackers: section gets them (config.defaults).
    spec: Mapping[str, Any] = field(default_factory=dict)
    # The site's own rules for a pasted link: new site settings, or None (not this site's link).
    guess: Callable[[UrlParts], dict[str, Any] | None] | None = None
    # A link of the site that is not its topic page (a CDN download link) -> the topic page.
    canonical_url: Callable[[str], str | None] | None = None

    def defaults(self) -> dict[str, Any]:
        """The settings a configured site of this name gets unless it sets them itself."""
        out: dict[str, Any] = {}
        if self.search_path:
            out["search_path"] = self.search_path
        if self.daily_limit:
            out["download_limit"] = True
        return out


@functools.cache
def _discover() -> tuple[SitePreset, ...]:
    import tow.trackers.presets as package

    found: dict[str, SitePreset] = {}
    SKIPPED.clear()
    for info in pkgutil.iter_modules(package.__path__):
        if info.name.startswith("_"):
            continue
        # One broken site module must not take the others down.
        try:
            preset = importlib.import_module(f"{package.__name__}.{info.name}").PRESET
            if not isinstance(preset, SitePreset):
                raise TypeError("PRESET is not a SitePreset")
        except Exception as exc:  # noqa: BLE001 - a broken plugin is skipped and named, never fatal
            SKIPPED.append((info.name, f"{type(exc).__name__}: {exc}"))
            _LOG.warning("tracker site module %s skipped: %s: %s", info.name, type(exc).__name__, exc)
            continue
        found[preset.name] = preset
    return tuple(found[name] for name in sorted(found))


def presets() -> dict[str, SitePreset]:
    """Every known site by name (scanned once per process)."""
    return {preset.name: preset for preset in _discover()}


def known_sites() -> dict[str, dict[str, Any]]:
    """The settings of every site that has them: what a config without ``trackers:`` uses."""
    return {preset.name: copy.deepcopy(dict(preset.spec)) for preset in _discover() if preset.spec}


def get(name: str) -> SitePreset | None:
    return presets().get(str(name or ""))


def defaults(name: str) -> dict[str, Any]:
    preset = get(name)
    return preset.defaults() if preset else {}


def label(name: str) -> str:
    """How messages name the site ("" when its preset gives no name)."""
    preset = get(name)
    return preset.label if preset else ""


def search_path(name: str) -> str:
    preset = get(name)
    return preset.search_path if preset else ""


def daily_limited(name: str) -> bool:
    preset = get(name)
    return bool(preset and preset.daily_limit)


def browser_login_hint(name: str) -> str:
    """The language-file key of the browser-login hint of the site ("" for none)."""
    preset = get(name)
    return preset.browser_login_hint if preset else ""


def brands() -> tuple[str, ...]:
    """Every regex part that names a known site, longest first (so "new-rutor" before "rutor")."""
    return tuple(sorted({brand for preset in _discover() for brand in preset.brands}, key=lambda b: (-len(b), b)))


def canonical_url(url: str) -> str:
    """``url`` as its site's topic page (a site's CDN link becomes the topic); else as it is."""
    for preset in _discover():
        if preset.canonical_url is not None and (canonical := preset.canonical_url(url)):
            return canonical
    return url


def guess(parts: UrlParts) -> dict[str, Any] | None:
    """Settings for a pasted link from a known site's own rules, None when none applies."""
    for preset in _discover():
        if preset.guess is not None and (spec := preset.guess(parts)) is not None:
            return spec
    return None
