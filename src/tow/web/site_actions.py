"""What the site forms do, for ``routes_sites``: add a site (or only its login, for one that
exists), save a site's card, and the store writes they share with the other site actions.

The configuration comes from ``tow.web.site_form``; every store is reached through
``tow.web.services`` and written as one transaction (``tow.web.site_store``).
"""

from __future__ import annotations

import copy
import dataclasses
from typing import Any
from urllib.parse import urlencode

from fastapi.responses import RedirectResponse
from starlette.background import BackgroundTask

from tow import store_transaction, undo
from tow.guess import GuessError, guess_from_url
from tow.jsonish import as_dict
from tow.log import error_class
from tow.store_transaction import StoreTransaction
from tow.undo.snapshots import site_snapshot
from tow.web import edit_base, services, site_merge
from tow.web.messages import flash_location, flash_redirect, use_subject
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
from tow.web.views import add_refused_redirect, keep_site_edit


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
    use_subject(str(spec.get("title") or key))  # the message is about the site just added
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


def _edit_refused(
    name: str, form: SiteEdit, reason: object, kind: str = "err", *, field: str = "", base_raw: str = ""
) -> RedirectResponse:
    """A refused save of site ``name``'s card: its row opens again at the refused field
    (``?open=&field=``; the page marks and focuses it, and opens Advanced when it is there) with
    what was typed in it (``&edit=``, never the password: ``site_edit_draft``)."""
    field = field or str(getattr(reason, "field", "") or "")
    draft = {key: value for key, value in dataclasses.asdict(form).items() if value is not None and key != "password"}
    if base_raw:  # the card comes back on the base it was rendered with, never on a newer one
        draft["base"] = base_raw
    query = urlencode({"open": name, **({"field": field} if field else {}), "edit": keep_site_edit(name, draft)})
    return flash_redirect(f"/sites?{query}", reason, kind)


def _site_now(name: str, spec: dict[str, Any]) -> dict[str, Any]:
    """Site ``name``'s card as saved now, as its form shows it (the preset's defaults included)."""
    login = as_dict(as_dict(services.load_secrets().get("trackers")).get(name))
    return site_merge.current(name, {**services.tracker_site_defaults(name), **spec}, login)


def site_edit_base(name: str, spec: dict[str, Any], login: dict[str, Any]) -> str:
    """The hidden ``base`` of site ``name``'s card as the page shows it (``spec`` with its
    preset's defaults, ``login`` from the secrets)."""
    return edit_base.encode(site_merge.SITE, name, site_merge.current(name, spec, login))


def _site_conflict(
    name: str, typed: SiteEdit, now: dict[str, Any], wanted: dict[str, Any], merged: edit_base.Merge, retype: bool
) -> RedirectResponse:
    """A save refused for a conflict: the card opens again with the owner's draft (never the
    password), both values of each conflicting group and a new base."""
    groups = merged.changed | set(merged.conflicts)
    token = edit_base.keep_conflict(
        site_merge.SITE,
        name,
        base=edit_base.encode(site_merge.SITE, name, now, merged.conflicts),
        draft=site_merge.draft(typed, groups),
        rows=site_merge.rows(merged.conflicts, now, wanted),
        retype=("password",) if retype else (),
    )
    return flash_redirect(f"/sites?{urlencode({'open': name, 'conflict': token})}", "web.edit.conflict", "warn")


def edit_site(
    name: str,
    form: SiteEdit,
    base_raw: str = "",
    resolutions: dict[str, str] | None = None,
    intent: tuple[str, str] = ("keep", ""),
) -> RedirectResponse:
    """Site ``name``'s card as saved - renamed, its mirrors, sign-in mirrors, paths and login -
    with an undo, as one transaction (the caller holds the persistence lock). Only what the
    owner changed since the card was rendered (``base_raw``) is saved, on the site as it is now;
    a group another device changed to something else meanwhile is a conflict, nothing is saved."""
    new_config = copy.deepcopy(services.load_config())
    trackers = new_config.setdefault("trackers", {})
    spec = trackers.get(name)
    if not isinstance(spec, dict):
        return flash_redirect("/sites", "web.sites.no_site", "err")
    now = _site_now(name, spec)
    base = edit_base.read(site_merge.SITE, name, base_raw, now)
    if base is None:
        return flash_redirect(f"/sites?{urlencode({'open': name})}", "web.edit.reload", "err")
    wanted = site_merge.submitted(name, form, intent)
    merged = edit_base.merge(site_merge.SITE, base, now, wanted, resolutions)
    if merged.conflicts:
        return _site_conflict(name, form, now, wanted, merged, intent[0] == "replace")
    typed = form
    form = site_merge.effective_form(name, form, now, wanted, merged)
    remove_password = "login" in merged.changed and intent[0] == "remove" and not form.browser_sign_in()
    if form.browser_sign_in():
        # The fields browser sign-in hides are never a new login: a typed or autofilled password
        # sent with them must not replace the saved one, which stays for a return to passwords.
        form = dataclasses.replace(form, username="", password="")
    previous_spec = copy.deepcopy(spec)
    try:
        key, added_login_hosts = edit_site_hosts(name, spec, form, trackers)
    except ValueError as exc:
        return _edit_refused(name, typed, exc, base_raw=base_raw)
    except SiteNameTaken:
        return _edit_refused(name, typed, "web.sites.name_taken", field="new_name", base_raw=base_raw)
    stored_password = ((services.load_secrets().get("trackers") or {}).get(name) or {}).get("password")
    if remove_password:
        stored_password = None
    forget_password = False
    if added_login_hosts and stored_password and not form.password.strip():
        # A stored tracker password must never be posted to a host it was not entered for. With
        # password sign-in the field is in view: typed again, or refused. Browser sign-in hides
        # it and does not use the password: it is forgotten instead (the user name stays).
        if not typed.browser_sign_in():
            return _edit_refused(name, typed, "web.sites.password_again", "warn", field="password", base_raw=base_raw)
        forget_password = True
    try:
        edit_site_paths(spec, form)
    except ValueError as exc:
        return _edit_refused(name, typed, exc, base_raw=base_raw)
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
        # A daily download limit reached today stays reached under the new name.
        limits = new_state.get("daily_limit")
        if isinstance(limits, dict) and name in limits:
            limits[key] = limits.pop(name)
    old_secrets = services.load_secrets()
    new_secrets = _renamed_login(old_secrets, name, key, form)
    if forget_password or remove_password:
        entry = (new_secrets.get("trackers") or {}).get(key)
        if isinstance(entry, dict):
            entry.pop("password", None)
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
    said = "web.sites.saved_password_removed" if forget_password else "web.common.saved"
    return RedirectResponse(_saved_location(said, list(spec["fetch_hosts"])), status_code=303)


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
