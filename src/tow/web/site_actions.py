"""What the site forms do, for ``routes_sites``: add a site (or only its login, for one that
exists), save a site's card, and the store writes they share with the other site actions.

The configuration comes from ``tow.web.site_form``; every store is reached through
``tow.web.services`` and written as one transaction (``tow.web.site_store``).
"""

from __future__ import annotations

import copy
from typing import Any

from fastapi.responses import RedirectResponse
from starlette.background import BackgroundTask

from tow import store_transaction, undo
from tow.guess import GuessError, guess_from_url
from tow.jsonish import as_dict
from tow.log import error_class
from tow.store_transaction import StoreTransaction
from tow.undo.snapshots import site_snapshot
from tow.web import services
from tow.web.site_form import (
    FieldRefused,
    NewSite,
    SiteEdit,
    SiteNameTaken,
    edit_site_hosts,
    edit_site_paths,
    new_site_spec,
    unresolved_note,
    valid_site_name,
)
from tow.web.site_store import commit_site_stores, save_together, with_login
from tow.web.text import t
from tow.web.views import add_refused_redirect, flash_location, flash_redirect


def save_site(config: dict[str, Any] | None = None, secrets: dict[str, Any] | None = None) -> RedirectResponse | None:
    """A site's config and login as one transaction, which also ends an older undo: a later,
    independent change must not leave it actionable. None when saved, else the refusal."""
    state = services.load_state()

    def write(txn: StoreTransaction) -> None:
        if config is not None:
            txn.save_config(config)
        if secrets is not None:
            txn.save_secrets(secrets)
        if undo.invalidate(state, txn):
            txn.save_state(state)

    return save_together("/sites", write)


def commit_site(
    config: dict[str, Any],
    state: dict[str, Any],
    secrets: dict[str, Any],
    undo_fields: dict[str, Any],
    snapshot: dict[str, Any],
) -> RedirectResponse | None:
    """One transaction for the site's config, mirrors, logins and undo; a refusal if it failed."""
    try:
        commit_site_stores(config, state, secrets, undo_fields=undo_fields, secret_undo_snapshot=snapshot)
    except store_transaction.RollbackError as exc:
        services.log_event("site_save_fail", status="rollback_failed", error=error_class(exc), how="manual")
        return flash_redirect("/sites", "web.common.rollback_incomplete", "err")
    except store_transaction.TransactionError as exc:
        services.log_event("site_save_fail", status="restored", error=error_class(exc.__cause__ or exc), how="manual")
        return flash_redirect("/sites", "web.sites.not_saved", "err")
    return None


def _saved_location(message: str, hosts: list[str]) -> str:
    """Back to Sites after a save; a warning names the mirrors whose names do not resolve now."""
    if note := unresolved_note(hosts):
        return flash_location("/sites", f"{t(message)}. {note}", "warn")
    return flash_location("/sites", message)


def _refused(form: NewSite, refusal: FieldRefused) -> RedirectResponse:
    """D2 for sites: the form comes back open with what was typed (never the password), the
    reason above it and the cursor in the field it is about."""
    return add_refused_redirect(str(refusal.reason), form.draft(), kind=refusal.field, page="/sites")


def add_site(form: NewSite) -> RedirectResponse:
    """A new site (its mirrors are probed after the answer); for a site that exists, only the
    login typed with it is saved (the caller holds the persistence lock)."""
    try:
        guessed = _guessed(form.from_url)
        try:
            key = valid_site_name(form.name or str(guessed.get("name") or ""))
        except ValueError as exc:
            raise FieldRefused(exc, "name") from exc
        cfg = services.load_config()
        trackers = cfg.setdefault("trackers", {})
        if key in trackers:
            if not form.typed_login():
                raise FieldRefused(t("web.sites.exists", name=key), "name")
            if refused_save := save_site(
                secrets=with_login(services.load_secrets(), key, form.username, form.password)
            ):
                return refused_save
            return flash_redirect("/sites", "web.sites.login_saved", "ok")
        hosts, spec = new_site_spec(key, form, guessed)
    except FieldRefused as refusal:
        return _refused(form, refusal)
    trackers[key] = spec
    secrets = with_login(services.load_secrets(), key, form.username, form.password) if form.typed_login() else None
    if not_saved := save_site(config=cfg, secrets=secrets):
        return not_saved
    services.log_event("site_add", tracker=key, how="manual")
    return RedirectResponse(
        _saved_location("web.sites.added", hosts),
        status_code=303,
        background=BackgroundTask(services.doctor_report, probe=True, names=[key]),
    )


def _guessed(from_url: str) -> dict[str, Any]:
    """What a topic link of the site says about it ({} without a link)."""
    if not from_url.strip():
        return {}
    try:
        return guess_from_url(from_url)
    except GuessError as e:
        raise FieldRefused(t(e.key), "from_url") from e


def edit_site(name: str, form: SiteEdit) -> RedirectResponse:
    """Site ``name``'s card as saved - renamed, its mirrors, sign-in mirrors, paths and login -
    with an undo, as one transaction (the caller holds the persistence lock)."""
    new_config = copy.deepcopy(services.load_config())
    trackers = new_config.setdefault("trackers", {})
    spec = trackers.get(name)
    if not isinstance(spec, dict):
        return flash_redirect("/sites", "web.sites.no_site", "err")
    previous_spec = copy.deepcopy(spec)
    try:
        key, added_login_hosts = edit_site_hosts(name, spec, form, trackers)
    except ValueError as exc:
        return flash_redirect("/sites", exc, "err")
    except SiteNameTaken:
        return flash_redirect("/sites", "web.sites.name_taken", "err")
    stored_password = ((services.load_secrets().get("trackers") or {}).get(name) or {}).get("password")
    if added_login_hosts and stored_password and not form.password.strip():
        # A stored tracker password must never be posted to a host it was not entered for.
        return flash_redirect("/sites", "web.sites.password_again", "warn")
    try:
        edit_site_paths(spec, form)
    except ValueError as exc:
        return flash_redirect("/sites", exc, "err")
    old_state = services.load_state()
    new_state = copy.deepcopy(old_state)
    old_mirrors = as_dict(old_state.get("mirrors"))
    mirror_present = name in old_mirrors
    mirror = copy.deepcopy(old_mirrors.get(name))
    mirrors = new_state.setdefault("mirrors", {})
    if key != name:
        trackers[key] = trackers.pop(name)
        trackers[key]["title"] = key
        if mirror_present:
            mirrors[key] = mirrors.pop(name)
        else:
            mirrors.pop(name, None)
    old_secrets = services.load_secrets()
    new_secrets = _renamed_login(old_secrets, name, key, form)
    undo_fields = {
        "name": name,
        "spec": previous_spec,
        "mirror": mirror,
        "mirror_present": mirror_present,
        "renamed_to": key if key != name else "",
    }
    if refused := commit_site(new_config, new_state, new_secrets, undo_fields, site_snapshot(old_secrets, name)):
        return refused
    services.log_event("site_save", tracker=key, how="manual")
    return RedirectResponse(_saved_location("web.common.saved", list(spec["fetch_hosts"])), status_code=303)


def delete_site(name: str) -> RedirectResponse:
    """Site ``name`` gone from the config, its mirrors' state and its login, with an undo, as one
    transaction (the caller holds the persistence lock)."""
    new_config = copy.deepcopy(services.load_config())
    spec = (new_config.get("trackers") or {}).pop(name, None)
    if spec is None:
        return flash_redirect("/sites", "web.sites.no_site", "err")
    new_state = copy.deepcopy(services.load_state())
    mirrors = new_state.setdefault("mirrors", {})
    mirror_present = name in mirrors
    mirror = copy.deepcopy(mirrors.pop(name, None))
    old_secrets = services.load_secrets()
    new_secrets = copy.deepcopy(old_secrets)
    tracker_secrets = new_secrets.get("trackers")
    if isinstance(tracker_secrets, dict):
        tracker_secrets.pop(name, None)
    undo_fields = {"name": name, "spec": spec, "mirror": mirror, "mirror_present": mirror_present}
    if refused := commit_site(new_config, new_state, new_secrets, undo_fields, site_snapshot(old_secrets, name)):
        return refused
    services.log_event("site_delete", tracker=name, how="manual")
    return flash_redirect("/sites", "web.sites.deleted", "ok")


def _renamed_login(secrets: dict[str, Any], name: str, key: str, form: SiteEdit) -> dict[str, Any]:
    """A copy of ``secrets`` with the site's login under its new name ``key`` and as typed."""
    new_secrets = copy.deepcopy(secrets)
    tracker_secrets = new_secrets.setdefault("trackers", {})
    if key != name and name in tracker_secrets:
        tracker_secrets[key] = tracker_secrets.pop(name)
    if form.username.strip() or form.password.strip():
        entry = tracker_secrets.setdefault(key, {})
        if form.username.strip():
            entry["username"] = form.username.strip()
        if form.password.strip():
            entry["password"] = form.password.strip()
    return new_secrets
