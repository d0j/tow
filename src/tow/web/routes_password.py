"""Routes: the TOW password - first start, the change in Settings, help on the sign-in page.

A forgotten password is reset on the computer running TOW: there (loopback) a new one is set
without the old one, in Settings or with ``tow password``. Another device needs the current
password to change it, and only this computer turns network access on or off - so nobody on
the network can lock the owner out. The rules themselves live in ``tow.access``.
"""

from __future__ import annotations

import copy
from typing import Any

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from tow import __version__, access, store_transaction, undo
from tow.auth import (
    MAX_HINT_LENGTH,
    AuthConfigurationError,
    lan_password_matches,
    lan_password_session_key,
    with_hint,
)
from tow.config import as_bool
from tow.store import SecretStoreError
from tow.store_transaction import StoreTransaction
from tow.web import _context, services
from tow.web.site_store import save_together
from tow.web.templating import TEMPLATES
from tow.web.text import t
from tow.web.views import flash_redirect

router = APIRouter()


def first_run(cfg: dict[str, Any]) -> bool:
    """No password yet and the owner has not put it off: Home opens the first-start page."""
    if as_bool(cfg.get("setup_done")):
        return False
    secrets = _context.secrets_or_none()
    return secrets is not None and not access.password_is_set(secrets)


def _setup_page(request: Request, error: str = "", status_code: int = 200) -> Response:
    return TEMPLATES.TemplateResponse(
        request,
        "setup.html",
        {"error": error, "hint_max": MAX_HINT_LENGTH, "version": __version__},
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
    if action == "skip":
        cfg["setup_done"] = True
        services.save_config(cfg)
        services.log_event("setup_skipped", how="manual")
        return flash_redirect("/", "setup.skipped", "ok")
    if not first_run(cfg):
        return RedirectResponse("/settings?open=access", status_code=303)
    try:
        record = access.new_record(lan_password, lan_password2, hint)
        secrets = services.load_secrets()
    except AuthConfigurationError as exc:
        return _setup_page(request, str(exc), status_code=400)
    except SecretStoreError:
        return _setup_page(request, t("web.password.store_unavailable"), status_code=400)
    secrets = copy.deepcopy(secrets)
    secrets[access.RECORD_KEY] = record
    cfg["setup_done"] = True
    lan = as_bool(allow_lan)
    if lan:
        cfg.update(allow_lan=True, bind="0.0.0.0")
    try:
        store_transaction.commit(config=cfg, secrets=secrets)  # never a password without its network setting
    except store_transaction.TransactionError:
        return _setup_page(request, t("web.password.store_unavailable"), status_code=503)
    access.sign_out_everywhere()
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
    try:
        if lan_password or lan_password2:
            new_record, changed = access.new_record(lan_password, lan_password2, hint), "password"
        elif record is not None:
            new_record, changed = with_hint(record, hint), "hint"
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
    access.sign_out_everywhere(request, response, session_key=lan_password_session_key(new_record))
    return response


@router.post("/settings/sessions/sign-out")
@services.locked_state_mutation
def settings_sign_out_everywhere(request: Request) -> Response:
    """Every device signed in over the network signs in again; the password stays the same."""
    try:
        session_key: str | None = access.credential(_context.secrets_or_none()).session_key
    except AuthConfigurationError:
        session_key = None
    response = _back("web.password.signed_out")
    access.sign_out_everywhere(request, response, session_key=session_key)
    services.log_event("sessions_signed_out", where="local" if access.is_local(request) else "network", how="manual")
    return response
