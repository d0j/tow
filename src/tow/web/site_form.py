"""What the site forms accept: mirror hosts (never an address inside the home network unless
allowed), the site name, paths and regular expressions - and a new or edited site's
configuration built from them."""

from __future__ import annotations

import ipaddress
import re
import socket
import string
from collections.abc import Container
from dataclasses import asdict, dataclass
from typing import Any
from urllib.parse import urlparse

from tow.config import as_bool
from tow.mirrors import origin_key
from tow.net_guard import is_public
from tow.trackers.generic import validate_tracker_regex
from tow.web import services
from tow.web.text import t


def split_lines(value: str) -> list[str]:
    return [x.strip() for x in (value or "").splitlines() if x.strip()]


def _resolve_addresses(name: str) -> list[str]:
    """Addresses a host name resolves to ([] when it does not resolve)."""

    try:
        return sorted({str(info[4][0]) for info in socket.getaddrinfo(name, None)})
    except OSError, UnicodeError, ValueError:
        return []


def internal_host(hostname: str) -> bool:
    """Loopback, private, link-local, CGNAT/Tailscale and other non-public addresses.

    A name is judged by what it resolves to: ``127.0.0.1.nip.io`` or a rebinding name must not
    turn a server-side fetch into a probe of this PC or the home network. A name that does not
    resolve now (a typo, or a site the provider's DNS blocks) is not internal: every fetch
    checks the DNS answer again and connects only to a public address (``tow.net_guard``), so
    it cannot become a private one after it was saved; ``unresolved_hosts`` names it instead.
    """
    name = hostname.strip("[]").lower()
    if name == "localhost" or name.endswith((".localhost", ".local", ".internal", ".lan", ".home.arpa")):
        return True
    try:
        return not is_public(name)
    except ValueError:
        pass
    for address in _resolve_addresses(name):
        try:
            if not is_public(address):
                return True
        except ValueError:
            continue
    return False


def unresolved_hosts(addresses: list[str]) -> list[str]:
    """Host names among these addresses that do not resolve now: saved, but not reachable yet."""
    names = []
    for address in addresses:
        try:
            name = (urlparse(address).hostname or "").strip("[]").lower()
        except ValueError:
            continue
        if name and not _is_ip(name) and not _resolve_addresses(name):
            names.append(name)
    return list(dict.fromkeys(names))


def _is_ip(name: str) -> bool:
    try:
        ipaddress.ip_address(name)
    except ValueError:
        return False
    return True


def unresolved_note(addresses: list[str]) -> str:
    """The sentence a save appends for addresses that do not resolve ("" when all do)."""
    names = unresolved_hosts(addresses)
    return t("web.site_form.unresolved", hosts=", ".join(names)) if names else ""


def valid_site_hosts(hosts: list[str]) -> list[str]:
    if not hosts:
        raise ValueError(t("web.site_form.mirrors_empty"))
    if len(hosts) > 16:
        raise ValueError(t("web.site_form.mirrors_too_many", limit=16))
    # Tracker hosts are fetched server-side; an internal address would let the site form
    # probe the owner's network. Opt in with allow_private_tracker_hosts: true.
    allow_internal = as_bool(services.load_config().get("allow_private_tracker_hosts"))
    clean = []
    origins = set()
    for host in hosts:
        try:
            parsed = urlparse(host)
            valid = (
                parsed.scheme in {"http", "https"}
                and parsed.hostname is not None
                and parsed.port != 0
                and parsed.username is None
                and parsed.password is None
                and parsed.path in {"", "/"}
                and not parsed.params
                and not parsed.query
                and not parsed.fragment
            )
        except ValueError:
            valid = False
        if not valid:
            raise ValueError(t("web.site_form.mirror_invalid"))
        if not allow_internal and internal_host(str(parsed.hostname)):
            raise ValueError(t("web.site_form.internal_off"))
        value = host.rstrip("/")
        origin = origin_key(value)
        if origin in origins:
            raise ValueError(t("web.site_form.mirrors_repeat"))
        origins.add(origin)
        clean.append(value)
    return clean


def valid_site_name(value: str) -> str:
    name = value.strip().lower()
    if not re.fullmatch(r"[a-z0-9_]{1,64}", name) or name in {"new", "guess"}:
        raise ValueError(t("web.site_form.name_invalid"))
    return name


def valid_tracker_path(value: str, *, label: str, needs_id: bool = False) -> str:
    """A path on the site; the download and topic paths (``needs_id``) carry the number as
    ``{id}`` and no other ``{field}``, which a fetch could not fill."""
    path = value.strip()
    if not path:
        return ""
    parsed = urlparse(path)
    if (
        not path.startswith("/")
        or path.startswith("//")
        or "\\" in path
        or parsed.scheme
        or parsed.netloc
        or parsed.fragment
    ):
        raise ValueError(t("web.site_form.path_invalid", label=label))
    if needs_id:
        try:
            fields = {field for _text, field, _spec, _conv in string.Formatter().parse(path) if field is not None}
        except ValueError:
            fields = set()
        if fields != {"id"}:
            raise ValueError(t("web.site_form.path_no_id", label=label))
    return path


def validate_tracker_regexes(url_regex: str, download_href_regex: str) -> None:
    for label, value in (("url", url_regex), ("download", download_href_regex)):
        # Same rules as at fetch time: compiles, bounded length, no catastrophic backtracking;
        # and a group in brackets, where the topic (or download) number is taken from.
        if value and validate_tracker_regex(value, label=label).groups < 1:
            what = t("tracker.regex_label_download" if label == "download" else "tracker.regex_label_url")
            raise ValueError(t("web.site_form.regex_no_group", what=what))


class FieldRefused(Exception):
    """A value of the site form is refused: ``reason`` (an error or a text) and the field it is about."""

    def __init__(self, reason: object, field: str) -> None:
        super().__init__(str(reason))
        self.reason, self.field = reason, field


@dataclass(frozen=True)
class NewSite:
    """The add-site form as typed (``from_url``: a topic link the empty fields are guessed from)."""

    name: str = ""
    url_regex: str = ""
    fetch_hosts: str = ""
    download_path: str = ""
    from_url: str = ""
    username: str = ""
    password: str = ""
    login_path: str = ""
    page_download: str = ""
    topic_path: str = ""
    download_href_regex: str = ""

    def draft(self) -> dict[str, str]:
        """What a refused add brings back into the form: never the password."""
        return {name: value for name, value in asdict(self).items() if name != "password"}

    def typed_login(self) -> bool:
        return bool(self.username.strip() or self.password.strip())


def new_site_spec(key: str, form: NewSite, guessed: dict[str, Any]) -> tuple[list[str], dict[str, Any]]:
    """The mirrors and the configuration of a new site ``key``: what was typed, else what the link
    gave; ``FieldRefused`` names the field that cannot be saved."""
    field = "fetch_hosts"
    try:
        hosts = valid_site_hosts(split_lines(form.fetch_hosts) or split_lines(str(guessed.get("fetch_hosts") or "")))
        regex = form.url_regex.strip() or str(guessed.get("url_regex") or "")
        if not regex:
            raise FieldRefused(t("web.sites.regex_missing"), "url_regex")
        paths = {}
        for field, typed, default, label in (
            ("download_path", form.download_path, "/download/{id}", "sites.download_path"),
            ("login_path", form.login_path, "", "sites.login_path"),
            ("topic_path", form.topic_path, "", "sites.topic_path"),
        ):
            paths[field] = valid_tracker_path(
                typed or str(guessed.get(field) or default), label=t(label), needs_id=field != "login_path"
            )
        href = form.download_href_regex.strip() or str(guessed.get("download_href_regex") or "")
        field = "url_regex"
        validate_tracker_regexes(regex, "")
        field = "download_href_regex"
        validate_tracker_regexes("", href)
    except ValueError as exc:  # the field being checked when it was refused
        raise FieldRefused(exc, field) from exc
    spec: dict[str, Any] = {
        "title": key,
        "url_regex": regex,
        "login_hosts": list(hosts),
        "fetch_hosts": hosts,
        "login_path": paths["login_path"],
        "download_path": paths["download_path"],
        "cookie_names": [],
        "fail_threshold": 3,
        "cooldown_sec": 3600,
    }
    if form.page_download in ("1", "true", "on") or bool(guessed.get("page_download")):
        spec["page_download"] = True
    if paths["topic_path"]:
        spec["topic_path"] = paths["topic_path"]
    if href:
        spec["download_href_regex"] = href
    return hosts, spec


@dataclass(frozen=True)
class SiteEdit:
    """A site's card as saved. ``login_hosts`` is None when the field was not sent: an emptied
    list arrives as no field at all, and ``login_hosts_present`` says it was there."""

    fetch_hosts: str = ""
    login_hosts: str | None = None
    login_hosts_present: str = ""
    url_regex: str = ""
    download_path: str = ""
    login_path: str = ""
    topic_path: str = ""
    page_download: str = ""
    download_href_regex: str = ""
    new_name: str = ""
    username: str = ""
    password: str = ""

    def chosen_login_hosts(self, mirrors: list[str]) -> list[str] | None:
        """The sign-in mirrors as typed (None: the form had no such field); each must be a mirror."""
        value = self.login_hosts if self.login_hosts is not None else "" if self.login_hosts_present == "1" else None
        if value is None:
            return None
        chosen = valid_site_hosts(split_lines(value)) if value.strip() else []
        if any(host not in mirrors for host in chosen):
            raise ValueError(t("web.sites.login_host_not_mirror"))
        return chosen


def edit_site_hosts(name: str, spec: dict[str, Any], form: SiteEdit, taken: Container[str]) -> tuple[str, list[str]]:
    """The edit's new name and mirrors, applied to ``spec`` (a copy of site ``name``'s); returns
    the name and the sign-in mirrors the edit added. A ValueError says why it cannot be saved."""
    validate_tracker_regexes(
        form.url_regex.strip() or str(spec.get("url_regex") or ""), form.download_href_regex.strip()
    )
    key = valid_site_name(form.new_name) if form.new_name.strip() else name
    new_hosts = valid_site_hosts(split_lines(form.fetch_hosts))
    chosen_login_hosts = form.chosen_login_hosts(new_hosts)
    if key != name and key in taken:
        raise SiteNameTaken
    old_login_hosts = list(spec.get("login_hosts") or [])
    spec["fetch_hosts"] = new_hosts
    # Removed mirrors must never receive credentials; new mirrors need explicit login setup.
    spec["login_hosts"] = (
        chosen_login_hosts
        if chosen_login_hosts is not None
        else [host for host in old_login_hosts if host in new_hosts]
    )
    return key, sorted(set(spec["login_hosts"]) - set(old_login_hosts))


class SiteNameTaken(Exception):
    """The new name of a site is another site's."""


def edit_site_paths(spec: dict[str, Any], form: SiteEdit) -> None:
    """The edit's pattern, paths and download settings, applied to ``spec`` (ValueError: refused)."""
    if form.url_regex.strip():
        spec["url_regex"] = form.url_regex.strip()
    new_download_path = (
        valid_tracker_path(form.download_path, label=t("sites.download_path"), needs_id=True)
        if form.download_path.strip()
        else ""
    )
    new_login_path = valid_tracker_path(form.login_path, label=t("sites.login_path"))
    new_topic_path = valid_tracker_path(form.topic_path, label=t("sites.topic_path"), needs_id=True)
    if new_download_path:
        spec["download_path"] = new_download_path
    spec["login_path"] = new_login_path
    if new_topic_path:
        spec["topic_path"] = new_topic_path
    else:
        spec.pop("topic_path", None)
    if form.page_download in ("1", "true", "on"):
        spec["page_download"] = True
    else:
        spec.pop("page_download", None)
    if form.download_href_regex.strip():
        spec["download_href_regex"] = form.download_href_regex.strip()
    else:
        spec.pop("download_href_regex", None)
