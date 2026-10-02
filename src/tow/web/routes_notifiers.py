"""Routes: the messenger cards in Settings - save, test and turn off a messenger (``tow.notifiers``)."""

from __future__ import annotations

import copy
from typing import Any
from urllib.parse import quote, urlparse

from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse, Response
from starlette.concurrency import run_in_threadpool

from tow.config import as_bool, interval_sec_of
from tow.web import services
from tow.web.site_form import internal_host
from tow.web.site_store import save_secrets_with_undo
from tow.web.text import t
from tow.web.views import flash_redirect

router = APIRouter()


def _notifier_target(kind: str) -> str:
    return f"/settings?open=bots&card={quote(kind)}#notifier-{quote(kind)}"


def _notifier_redirect(message: str, kind: str, flash_kind: str = "ok") -> RedirectResponse:
    return flash_redirect(_notifier_target(kind), message, flash_kind)


@router.post("/settings/notifier/{kind}")
async def settings_notifier_save(request: Request, kind: str) -> Response:
    form = {key: value for key, value in (await request.form()).items() if isinstance(value, str)}
    return await run_in_threadpool(_save_notifier, kind, form)


def _notifier_scope(kind: str) -> list[str]:
    from tow import notifiers

    storage = getattr(notifiers.kinds()[kind], "STORAGE", None)
    return [storage] if storage else ["notifiers", kind]


def _raw_notifier(secrets: dict[str, Any], kind: str) -> dict[str, Any] | None:
    """Partly filled settings still keep their saved secrets when the form leaves them empty."""
    scope = _notifier_scope(kind)
    raw = secrets.get(scope[0])
    if len(scope) == 2:
        raw = raw.get(scope[1]) if isinstance(raw, dict) else None
    return raw if isinstance(raw, dict) else None


# Servers TOW posts messages to by itself. An address inside the home network or on this PC
# would let the form make TOW post to it (a blind request into the LAN), as for tracker hosts.
# A self-hosted server at home is allowed with allow_private_notifier_hosts: true in config.yaml.
_SERVER_FIELDS = {"ntfy": ("server",)}


def _private_server_problem(kind: str, values: dict[str, Any] | None) -> str | None:
    if not values or as_bool(services.load_config().get("allow_private_notifier_hosts")):
        return None
    for field in _SERVER_FIELDS.get(kind, ()):
        value = str(values.get(field) or "")
        if not value:
            continue
        try:
            host = urlparse(value).hostname
        except ValueError:
            host = None
        if not host or internal_host(host):
            return t("web.settings.notifier_private_host")
    return None


def _unknown_notifier() -> RedirectResponse:
    return flash_redirect("/settings?open=bots", "web.settings.unknown_messenger", "err")


@services.locked_state_mutation
def _save_notifier(kind: str, form: dict[str, str]) -> RedirectResponse:
    from tow import notifiers

    if kind not in notifiers.kinds():
        return _unknown_notifier()
    s = services.load_secrets()
    values, errors = notifiers.validate(kind, form, _raw_notifier(s, kind))
    title = notifiers.kinds()[kind].TITLE
    if not errors and (problem := _private_server_problem(kind, values)):
        errors.append(problem)
    if errors:
        return _notifier_redirect(
            t("web.settings.notifier_not_saved", title=title, errors="; ".join(errors)), kind, "err"
        )
    undo_secrets = copy.deepcopy(s)
    old_sec = interval_sec_of(services.load_config())
    notifiers.store(s, kind, values)
    if refused := save_secrets_with_undo(_notifier_target(kind), undo_secrets, s, old_sec, _notifier_scope(kind)):
        return refused
    services.log_event("settings_notifier", integration_id=kind, how="manual")
    return _notifier_redirect(t("web.settings.notifier_saved", title=title), kind)


@router.post("/settings/notifier/{kind}/test")
def settings_notifier_test(kind: str) -> Response:
    from tow import notifiers

    if kind not in notifiers.kinds():
        return _unknown_notifier()
    secrets = services.load_secrets()
    if problem := _private_server_problem(kind, _raw_notifier(secrets, kind)):  # saved by an older version
        return _notifier_redirect(problem, kind, "err")
    ok, message = notifiers.test(secrets, kind)
    services.log_event("notifier_test", integration_id=kind, ok=ok, error=None if ok else message, how="manual")
    return _notifier_redirect(message, kind, "ok" if ok else "err")


@router.post("/settings/notifier/{kind}/remove")
@services.locked_state_mutation
def settings_notifier_remove(kind: str) -> Response:
    from tow import notifiers

    if kind not in notifiers.kinds():
        return _unknown_notifier()
    s = services.load_secrets()
    undo_secrets = copy.deepcopy(s)
    old_sec = interval_sec_of(services.load_config())
    notifiers.remove(s, kind)
    if refused := save_secrets_with_undo(_notifier_target(kind), undo_secrets, s, old_sec, _notifier_scope(kind)):
        return refused
    services.log_event("settings_notifier_removed", integration_id=kind, how="manual")
    return _notifier_redirect(t("web.settings.notifier_off", title=notifiers.kinds()[kind].TITLE), kind)
