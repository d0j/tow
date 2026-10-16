"""A site's card as the three-way merge sees it (``tow.web.edit_base``): the values the card
showed, what a save asks for, the card ``site_actions`` applies - the owner's changes on the site
as it is now - and the rows a conflict shows.

Groups: the name; the mirrors with the sign-in mirrors (a sign-in mirror must be a mirror: one
value); the sign-in (browser sign-in, user name and the password's comparison token: one value);
and each path, pattern and switch on its own. The password is never in the page: the base holds
an opaque token of the saved one, a save asks to keep, replace or remove it.
"""

from __future__ import annotations

import dataclasses
from collections.abc import Mapping
from typing import Any

from tow.web.edit_base import Merge, Row, Spec, secret_intent, secret_token
from tow.web.site_form import SiteEdit, split_lines
from tow.web.text import t

SITE = Spec(
    "site",
    {
        "name": ("new_name",),
        "mirrors": ("fetch_hosts", "login_hosts"),
        "login": ("browser_auth", "username", "password"),
        "url_regex": ("url_regex",),
        "download_path": ("download_path",),
        "login_path": ("login_path",),
        "topic_path": ("topic_path",),
        "page_download": ("page_download",),
        "download_href_regex": ("download_href_regex",),
        "protection": ("protection",),
        "infohash_path": ("infohash_path",),
    },
    secret=frozenset({"password"}),
)

# The card's fields of each group: a refused save's draft keeps those of the groups it changed.
FORM_FIELDS = {
    "name": ("new_name",),
    "mirrors": ("fetch_hosts", "login_hosts"),
    "login": ("browser_auth", "username"),
    "url_regex": ("url_regex",),
    "download_path": ("download_path",),
    "login_path": ("login_path",),
    "topic_path": ("topic_path",),
    "page_download": ("page_download",),
    "download_href_regex": ("download_href_regex",),
    "protection": ("protection",),
    "infohash_path": ("infohash_path",),
}

_LABELS = {
    "name": "sites.name_label",
    "mirrors": "sites.mirrors",
    "login": "sites.col_login",
    "url_regex": "sites.url_regex",
    "download_path": "sites.download_path",
    "login_path": "sites.login_path",
    "topic_path": "sites.topic_path",
    "page_download": "sites.page_download",
    "download_href_regex": "sites.href_regex",
    "protection": "sites.protection",
    "infohash_path": "sites.infohash_path",
}


def _hosts(values: object) -> tuple[str, ...]:
    return (
        tuple(str(host).rstrip("/") for host in values or [] if str(host).strip()) if isinstance(values, list) else ()
    )


def _text(value: object) -> str:
    return str(value or "").strip()


def current(name: str, spec: Mapping[str, Any], login: Mapping[str, Any]) -> dict[str, Any]:
    """The card's values of site ``name`` as saved now (its login from the secrets, the password
    as a token only)."""
    return {
        "new_name": name,
        "fetch_hosts": _hosts(spec.get("fetch_hosts")),
        "login_hosts": _hosts(spec.get("login_hosts")),
        "browser_auth": bool(spec.get("browser_auth")),
        "username": _text(login.get("username")),
        "password": secret_token("site", name, "password", login.get("password")),
        "url_regex": _text(spec.get("url_regex")),
        "download_path": _text(spec.get("download_path")),
        "login_path": _text(spec.get("login_path")),
        "topic_path": _text(spec.get("topic_path")),
        "page_download": bool(spec.get("page_download")),
        "download_href_regex": _text(spec.get("download_href_regex")),
        "protection": "off" if spec.get("protection") == "off" else "auto",
        "infohash_path": _text(spec.get("infohash_path")),
    }


def _on(value: str) -> bool:
    return value in {"1", "true", "on"}


def submitted(name: str, form: SiteEdit, intent: tuple[str, str]) -> dict[str, Any]:
    """What the save asks for, as the card's values (a field the card did not send, or one whose
    empty value keeps what is saved, is absent)."""
    out: dict[str, Any] = {
        "new_name": form.new_name.strip().lower() or name,
        "fetch_hosts": tuple(host.rstrip("/") for host in split_lines(form.fetch_hosts)),
        "login_path": form.login_path.strip(),
        "topic_path": form.topic_path.strip(),
        "page_download": _on(form.page_download),
        "download_href_regex": form.download_href_regex.strip(),
    }
    login_hosts = form.login_hosts if form.login_hosts is not None else "" if form.login_hosts_present == "1" else None
    if login_hosts is not None:
        out["login_hosts"] = tuple(host.rstrip("/") for host in split_lines(login_hosts))
    if form.browser_auth_present == "1":
        out["browser_auth"] = form.browser_sign_in()
    if not form.browser_sign_in():  # browser sign-in: the user name and password are not sent
        if form.username.strip():
            out["username"] = form.username.strip()
        kind, typed = intent
        if kind != "keep":
            out["password"] = secret_token("site", name, "password", typed if kind == "replace" else None)
    for key in ("url_regex", "download_path"):  # empty keeps what is saved
        if value := getattr(form, key).strip():
            out[key] = value
    if form.protection:
        out["protection"] = form.protection
    if form.infohash_path is not None:
        out["infohash_path"] = form.infohash_path.strip()
    return out


def password_intent(fields: Mapping[str, str]) -> tuple[str, str]:
    return secret_intent(fields, "password")


def effective_form(
    name: str, form: SiteEdit, now: Mapping[str, Any], wanted: Mapping[str, Any], merged: Merge
) -> SiteEdit:
    """The card ``site_actions`` applies: the owner's values for the groups they changed, the
    site's own for every other (so a save puts back nothing another device changed)."""
    changed = merged.changed
    keep: dict[str, Any] = {}
    if "name" not in changed:
        keep["new_name"] = name
    if "mirrors" not in changed:
        keep.update(
            fetch_hosts="\n".join(now["fetch_hosts"]),
            login_hosts="\n".join(now["login_hosts"]),
            login_hosts_present="1",
        )
    if "login" not in changed:
        # The password typed again as saved (new sign-in mirrors ask for it) is no change, but it
        # still proves the owner knows it.
        retyped = form.password if wanted.get("password") == now["password"] else ""
        keep.update(
            browser_auth="1" if now["browser_auth"] else "", browser_auth_present="1", username="", password=retyped
        )
    for key in ("url_regex", "login_path", "topic_path", "download_href_regex"):
        if key not in changed:
            keep[key] = now[key]
    if "download_path" not in changed:
        keep["download_path"] = ""  # empty keeps it
    if "page_download" not in changed:
        keep["page_download"] = "1" if now["page_download"] else ""
    if "protection" not in changed:
        keep["protection"] = ""  # absent keeps it
    if "infohash_path" not in changed:
        keep["infohash_path"] = None  # absent keeps it
    return dataclasses.replace(form, **keep)


def draft(form: SiteEdit, groups: frozenset[str] | set[str]) -> dict[str, str]:
    """The fields of the groups the owner changed, as typed (never the password)."""
    values = {key: value for key, value in dataclasses.asdict(form).items() if value is not None}
    out = {name: str(values[name]) for group in groups for name in FORM_FIELDS[group] if name in values}
    if "login" in groups and form.browser_auth_present == "1":
        out["browser_auth"] = "1" if form.browser_sign_in() else ""
    return out


def _shown(group: str, values: Mapping[str, Any]) -> str:
    if group == "mirrors":
        hosts = ", ".join(values["fetch_hosts"]) or t("web.edit.value.empty")
        return hosts + (
            f" ({t('sites.login_hosts')}: {', '.join(values['login_hosts'])})" if values["login_hosts"] else ""
        )
    if group == "login":
        if values["browser_auth"]:
            return t("sites.browser_auth")
        return values["username"] or t("web.edit.value.empty")
    value = values[FORM_FIELDS[group][0]]
    if isinstance(value, bool):
        return t("web.edit.value.on" if value else "web.edit.value.off")
    return str(value) if str(value) else t("web.edit.value.empty")


def rows(groups: tuple[str, ...], now: Mapping[str, Any], wanted: Mapping[str, Any]) -> list[Row]:
    """What a conflict shows for each group: saved now, and the owner's value (never a password)."""
    mine = {**now, **wanted}
    return [Row(group, t(_LABELS[group]), _shown(group, now), _shown(group, mine)) for group in groups]
