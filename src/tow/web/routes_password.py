"""Routes: the TOW password - first start, the change in Settings, help on the sign-in page.

A forgotten password is reset on the computer running TOW: there (loopback) a new one is set
without the old one, in Settings or with ``tow password``. Another device needs the current
password to change it, and only this computer turns network access on or off - so nobody on
the network can lock the owner out. The rules themselves live in ``tow.access``.
"""

from __future__ import annotations

import contextlib
import copy
from typing import Any

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from tow import __version__, access, undo
from tow.auth import (
    MAX_HINT_LENGTH,
    AuthConfigurationError,
    lan_password_matches,
    lan_password_session_key,
    with_hint,
)
from tow.bind import network_bind
from tow.config import as_bool
from tow.store import SecretStoreError
from tow.store_transaction import StoreTransaction, TransactionError
from tow.web import _context, services
from tow.web.messages import flash_redirect
from tow.web.site_store import save_together
from tow.web.templating import TEMPLATES
from tow.web.text import t
from tow.web.views import first_run

router = APIRouter()


# The field a refused first start is about, by its reason's key (the reminder, a password).
_SETUP_FIELDS = {
    "auth.hint_too_long": "lan-hint",
    "auth.hint_needs_password": "lan-hint",
    "auth.hint_reveals_password": "lan-hint",
    "auth.password_too_short": "lan-password",
    "web.password.differ": "lan-password2",
}


def _setup_page(
    request: Request,
    error: Any = "",
    status_code: int = 200,
    *,
    hint: str = "",
    allow_lan: bool = False,
) -> Response:
    """The first-start page; a refused one keeps what was typed but the passwords (the reminder,
    the box) and marks the field its reason is about."""
    return TEMPLATES.TemplateResponse(
        request,
        "setup.html",
        {
            "error": str(error),
            "error_field": _SETUP_FIELDS.get(str(getattr(error, "code", "")), ""),
            "hint": hint,
            "allow_lan": allow_lan,
            "hint_max": MAX_HINT_LENGTH,
            "version": __version__,
        },
        status_code=status_code,
    )


@router.get("/setup", response_class=HTMLResponse, dependencies=[access.require_local("/")])
def setup_page(request: Request) -> Response:
    if not first_run(_context.config()):
        return RedirectResponse("/", status_code=303)
    return _setup_page(request)


@router.post("/setup", dependencies=[access.require_local("/")])
@services.locked_state_mutation
def setup_save(
    request: Request,
    action: str = Form("save"),
    lan_password: str = Form(""),
    lan_password2: str = Form(""),
    hint: str = Form(""),
    allow_lan: str = Form(""),
) -> Response:
    cfg = services.load_config()
    lan = as_bool(allow_lan)
    # The page asks "open TOW from other devices?" first: Start with the box clear is "later - this
    # computer only", whatever a hidden password field still sent (a browser without app.js).
    if action == "skip" or not lan:
        cfg["setup_done"] = True
        services.save_config(cfg)
        services.log_event("setup_skipped", how="manual")
        return flash_redirect("/", "setup.skipped", "ok")
    if not first_run(cfg):
        if access.password_is_set(_context.secrets_or_none()):
            # Two windows set up at once: the first password stays; this one is said to be dropped.
            return flash_redirect("/settings?open=access", "setup.already_set", "warn")
        return RedirectResponse("/settings?open=access", status_code=303)
    try:
        record = access.new_record(lan_password, lan_password2, hint)
        secrets = services.load_secrets()
    except AuthConfigurationError as exc:
        return _setup_page(request, exc, status_code=400, hint=hint, allow_lan=lan)
    except SecretStoreError:
        return _setup_page(request, t("web.password.store_unavailable"), status_code=400, hint=hint, allow_lan=lan)
    secrets = copy.deepcopy(secrets)
    secrets[access.RECORD_KEY] = record
    cfg["setup_done"] = True
    if lan:
        cfg.update(allow_lan=True, bind=network_bind(cfg.get("bind")))
    try:
        services.commit_stores(config=cfg, secrets=secrets)  # never a password without its network setting
    except TransactionError:
        return _setup_page(request, t("web.password.store_unavailable"), status_code=503, hint=hint, allow_lan=lan)
    with contextlib.suppress(OSError):  # the first password: no device was signed in with an earlier one
        services.sign_out_everywhere()
    services.log_event("setup_password", allow_lan=lan, hint=bool(record.get("hint")), how="manual")
    return flash_redirect("/", "setup.saved_lan" if lan else "setup.saved")


def _back(message: Any, kind: str = "ok", /, **params: Any) -> RedirectResponse:
    return flash_redirect("/settings?open=access", message, kind, **params)


@router.post("/settings/password")
@services.locked_state_mutation
def settings_password(
    request: Request,
    current_password: str = Form(""),
    lan_password: str = Form(""),
    lan_password2: str = Form(""),
    hint: str = Form(""),
) -> Response:
    local = access.is_local(request)
    try:
        secrets = services.load_secrets()
    except SecretStoreError:
        return _back("web.password.store_unavailable", "err")
    record = access.password_record(secrets)
    # The current password, checked: from the network always; on this computer it is typed
    # only to change the reminder alone, which is compared with it.
    current = ""
    if not local:
        if record is None:
            return _back("web.settings.access_local_only", "err")
        peer = request.client.host if request.client else "unknown"
        if wait := services.login_throttle.attempt(peer):  # counted before the check, as at sign-in
            return _back("web.login.too_many", "err", sec=wait)
        with services.login_throttle.verification:
            valid = lan_password_matches(current_password, record)
        if not valid:
            return _back("web.password.current_wrong", "err")
        services.login_throttle.success(peer)
        current = current_password
    elif current_password and record is not None and not (lan_password or lan_password2):
        if not lan_password_matches(current_password, record):
            return _back("web.password.current_wrong", "err")
        current = current_password
    try:
        if lan_password or lan_password2:
            new_record, changed = access.new_record(lan_password, lan_password2, hint), "password"
        elif record is not None:
            new_record, changed = with_hint(record, hint, password=current), "hint"
        else:
            return _back("web.settings.set_password_first", "warn")
    except AuthConfigurationError as exc:
        return _back(exc, "err")
    if new_record == record:
        return _back("web.common.no_changes")  # the prefilled reminder sent back as it was
    cfg = services.load_config()
    state = services.load_state()
    updated = copy.deepcopy(secrets)
    updated[access.RECORD_KEY] = new_record

    def write(txn: StoreTransaction) -> None:
        undo.stamp(
            state,
            "settings_access",
            txn=txn,
            snapshot=secrets,
            old_bind=str(cfg.get("bind") or "127.0.0.1"),
            old_allow_lan=as_bool(cfg.get("allow_lan")),
            secret_scope=[access.RECORD_KEY],
        )
        txn.save_secrets(updated)
        txn.save_state(state)
        if not as_bool(cfg.get("setup_done")):  # the first-start page is done from now on
            cfg["setup_done"] = True
            txn.save_config(cfg)

    if refused := save_together("/settings?open=access", write):
        return refused
    services.log_event(
        "settings_password",
        changed=changed,
        hint=bool(new_record.get("hint")),
        where="local" if local else "network",
        how="manual",
    )
    if changed == "hint":
        return _back("web.password.hint_saved")
    # A new password signs every device out (its key changes), and so does a later undo of it:
    # the old sessions stay out even if the old password comes back. This device stays in.
    response = _back("web.password.saved")
    try:
        services.sign_out_everywhere(request, response, session_key=lan_password_session_key(new_record))
    except AuthConfigurationError, OSError:
        # data/sessions.json could not be written (or read for this device's new session): the
        # password is saved and the old sessions no longer match it, so only this device has to
        # sign in again - said, instead of a server error after the change was made.
        return _back("web.password.saved_sign_in_again", "warn")
    return response


@router.post("/settings/sessions/sign-out")
@services.locked_state_mutation
def settings_sign_out_everywhere(request: Request) -> Response:
    """Every device signed in over the network signs in again; the password stays the same."""
    try:
        session_key: str | None = services.network_credential(_context.secrets_or_none()).session_key
    except AuthConfigurationError:
        session_key = None
    response = _back("web.password.signed_out")
    try:
        services.sign_out_everywhere(request, response, session_key=session_key)
    except AuthConfigurationError, OSError:
        return _back("web.password.sign_out_failed", "err")  # nobody was signed out: said so
    services.log_event("sessions_signed_out", where="local" if access.is_local(request) else "network", how="manual")
    return response
