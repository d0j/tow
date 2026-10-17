"""Routes: the messenger cards in Settings - save, test and turn off a messenger (``tow.notifiers``)."""

from __future__ import annotations

import copy
from typing import Any
from urllib.parse import quote, urlparse

from fastapi import APIRouter, Request
from fastapi.responses import RedirectResponse, Response
from starlette.concurrency import run_in_threadpool

from tow.config import as_bool, interval_sec_of
from tow.web import edit_base, services, settings_merge
from tow.web.messages import flash_redirect
from tow.web.site_form import internal_host, unresolved_note
from tow.web.site_store import SIGN_IN_PASSWORD, is_sign_in_password, save_secrets_with_undo
from tow.web.text import t

router = APIRouter()


def _notifier_target(kind: str) -> str:
    return f"/settings?open=bots&card={quote(kind)}#notifier-{quote(kind)}"


def _notifier_redirect(message: str, kind: str, flash_kind: str = "ok") -> RedirectResponse:
    return flash_redirect(_notifier_target(kind), message, flash_kind)


@router.post("/settings/notifier/{kind}")
async def settings_notifier_save(request: Request, kind: str) -> Response:
    form = {key: value for key, value in (await request.form()).items() if isinstance(value, str)}
    return await run_in_threadpool(_save_notifier, kind, form)


def _raw_notifier(secrets: dict[str, Any], kind: str) -> dict[str, Any] | None:
    """Partly filled settings still keep their saved secrets when the form leaves them empty.
    A block that is not a mapping reads as no saved settings."""
    from tow import notifiers

    raw: Any = secrets
    for name in notifiers.storage_scope(kind):
        raw = raw.get(name) if isinstance(raw, dict) else None
    return raw if isinstance(raw, dict) else None


def _server_fields(kind: str) -> tuple[str, ...]:
    """The fields that name a server TOW posts messages to by itself (a messenger's
    ``SERVER_FIELDS``). An address inside the home network or on this PC would let the form make
    TOW post to it (a blind request into the LAN), as for tracker hosts. A self-hosted server at
    home is allowed with allow_private_notifier_hosts: true in config.yaml."""
    from tow import notifiers

    return tuple(getattr(notifiers.kinds()[kind], "SERVER_FIELDS", ()))


def _private_server_problem(kind: str, values: dict[str, Any] | None) -> str | None:
    if not values or as_bool(services.load_config().get("allow_private_notifier_hosts")):
        return None
    for field in _server_fields(kind):
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
    spec = settings_merge.notifier_spec(kind)
    if is_sign_in_password(*(str(form.get(name) or "") for name in sorted(spec.secret))):
        return _notifier_redirect(t(SIGN_IN_PASSWORD), kind, "err")
    s = services.load_secrets()
    title = notifiers.kinds()[kind].TITLE
    # The card's fields are one group (a token belongs to its chat): changed here and on another
    # device since the card was rendered is a conflict; unchanged here keeps what is saved now.
    now = settings_merge.notifier_now(kind, s)
    got = edit_base.read(spec, kind, form.get("base", ""), now)
    if got is None:
        return edit_base.reload_redirect(_notifier_target(kind))
    wanted = settings_merge.notifier_wanted(kind, form)
    resolutions = {key.removeprefix("resolve_"): value for key, value in form.items() if key.startswith("resolve_")}
    merged = edit_base.merge(spec, got, now, wanted, resolutions)
    if merged.conflicts:
        return edit_base.conflict_redirect(
            spec,
            kind,
            _notifier_target(kind),
            now=now,
            merged=merged,
            draft=settings_merge.notifier_draft(kind, form),
            rows=settings_merge.card_rows(spec, notifiers.title(notifiers.kinds()[kind]), now, wanted),
            retype=tuple(name for name in spec.secret if str(form.get(name) or "").strip()),
        )
    if not merged.changed and _raw_notifier(s, kind) is not None:  # the card as saved now: nothing to write
        return _notifier_redirect(t("web.common.no_changes"), kind)
    values, errors = notifiers.validate(kind, form, _raw_notifier(s, kind))
    if not errors and (problem := _private_server_problem(kind, values)):
        errors.append(problem)
    if errors:
        return _notifier_redirect(
            t("web.settings.notifier_not_saved", title=title, errors="; ".join(errors)), kind, "err"
        )
    undo_secrets = copy.deepcopy(s)
    old_sec = interval_sec_of(services.load_config())
    notifiers.store(s, kind, values)
    if refused := save_secrets_with_undo(
        _notifier_target(kind), undo_secrets, s, old_sec, notifiers.storage_scope(kind)
    ):
        return refused
    services.log_event("settings_notifier", integration_id=kind, how="manual")
    servers = [str(values.get(field) or "") for field in _server_fields(kind)]
    if note := unresolved_note([server for server in servers if server]):
        return _notifier_redirect(f"{t('web.settings.notifier_saved', title=title)}. {note}", kind, "warn")
    return _notifier_redirect(t("web.settings.notifier_saved", title=title), kind)


@router.post("/settings/notifier/{kind}/test")
def settings_notifier_test(kind: str) -> Response:
    from tow import notifiers

    if kind not in notifiers.kinds():
        return _unknown_notifier()
    secrets = services.load_secrets()
    if problem := _private_server_problem(kind, _raw_notifier(secrets, kind)):  # saved by an older version
        return _notifier_redirect(problem, kind, "err")
    ok, message = services.test_notifier(secrets, kind)
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
    if refused := save_secrets_with_undo(
        _notifier_target(kind), undo_secrets, s, old_sec, notifiers.storage_scope(kind)
    ):
        return refused
    services.log_event("settings_notifier_removed", integration_id=kind, how="manual")
    return _notifier_redirect(t("web.settings.notifier_off", title=notifiers.kinds()[kind].TITLE), kind)
