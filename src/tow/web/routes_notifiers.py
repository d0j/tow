"""Routes: the Notifications section of Settings - save, test and turn off a messenger
(``tow.notifiers``), and its quiet hours and daily digest (``tow.delivery``)."""

from __future__ import annotations

import copy
from typing import Any
from urllib.parse import quote, urlencode, urlparse

from fastapi import APIRouter, Form, Request
from fastapi.responses import RedirectResponse, Response
from starlette.concurrency import run_in_threadpool

from tow.config import as_bool, interval_sec_of
from tow.store_transaction import StoreTransaction
from tow.web import edit_base, services, settings_merge
from tow.web.messages import flash_redirect
from tow.web.site_form import internal_host, unresolved_note
from tow.web.site_store import (
    SIGN_IN_PASSWORD,
    is_sign_in_password,
    save_secrets_with_undo,
    save_together,
    settings_undo,
)
from tow.web.text import t
from tow.web.views import keep_settings_draft

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


# --------------------------------------------------------------------------- quiet hours, digest

QUIET_CARD = "quiet"
_QUIET_TARGET = "/settings?open=bots&card=quiet#quiet-hours"


def _quiet_refused(problem: str, typed: dict[str, str], base: str, field: str) -> RedirectResponse:
    """A refused save of the card: it opens again with what was typed, on the base it was drawn
    with, the refused field marked (``?draft=&field=``), and config.yaml's own words for why."""
    token = keep_settings_draft(QUIET_CARD, {**typed, "base": base})
    query = urlencode({"open": "bots", "card": QUIET_CARD, "draft": token, "field": field})
    return flash_redirect(f"/settings?{query}#quiet-hours", t("settings.quiet.not_saved", problem=problem), "err")


# Empty defaults: an empty field is "off", not FastAPI's missing value.
@router.post("/settings/quiet-hours")
@services.locked_state_mutation
def settings_quiet_hours(
    quiet_from: str = Form(""),
    quiet_to: str = Form(""),
    daily_digest_hour: str = Form(""),
    base: str = Form(""),
    resolutions: dict[str, str] = edit_base.RESOLUTIONS,
) -> Response:
    """Quiet hours ("23-8") and the daily digest's hour, saved in place in config.yaml (the
    owner's comments stay; an empty field takes its key out) with the three-way merge and the
    Undo of the Checks form, and checked by config.yaml's own rules. A device signed in from the
    network may change them, as it may the messengers."""
    cfg = services.load_config()
    now = settings_merge.quiet_now(cfg)
    typed = {"quiet_from": quiet_from, "quiet_to": quiet_to, "daily_digest_hour": daily_digest_hour}
    wanted = settings_merge.quiet_wanted(quiet_from, quiet_to, daily_digest_hour)
    got = edit_base.read(settings_merge.QUIET, QUIET_CARD, base, now)
    if got is None:
        return edit_base.reload_redirect(_QUIET_TARGET)
    merged = edit_base.merge(settings_merge.QUIET, got, now, wanted, resolutions)
    if merged.conflicts:
        return edit_base.conflict_redirect(
            settings_merge.QUIET,
            QUIET_CARD,
            _QUIET_TARGET,
            now=now,
            merged=merged,
            draft=typed,
            rows=settings_merge.quiet_rows(merged.conflicts, now, wanted),
        )
    # Only what this save changes is checked: config.yaml may hold a value the owner left alone.
    if "quiet" in merged.changed and (problem := settings_merge.quiet_problem(quiet_from, quiet_to)):
        return _quiet_refused(problem, typed, base, "quiet_from" if quiet_from.strip() else "quiet_to")
    if "digest" in merged.changed and (problem := settings_merge.digest_problem(daily_digest_hour)):
        return _quiet_refused(problem, typed, base, "daily_digest_hour")
    if not merged.changed:
        return flash_redirect(_QUIET_TARGET, "web.common.no_changes", "ok")
    quiet, digest = settings_merge.quiet_saved(merged.values)
    previous: dict[str, Any] = {}
    if "quiet" in merged.changed:
        previous["quiet_hours"] = cfg.get("quiet_hours")
    if "digest" in merged.changed:
        previous["daily_digest_hour"] = cfg.get("daily_digest_hour")
    state = services.load_state()
    undo_secrets = copy.deepcopy(services.load_secrets())

    def write(txn: StoreTransaction) -> None:
        settings_undo(txn, state, undo_secrets, interval_sec_of(cfg), [], previous=previous)
        if "quiet_hours" in previous:
            txn.set_quiet_hours(quiet)
        if "daily_digest_hour" in previous:
            txn.set_daily_digest_hour(digest)  # the next check reads both from config.yaml
        txn.save_state(state)

    if refused := save_together(_QUIET_TARGET, write):
        return refused
    # What this save set (None: taken out); what it left alone is not said.
    saved = {"quiet_hours": quiet, "daily_digest_hour": digest}
    services.log_event("settings_quiet_hours", **{key: saved[key] for key in previous}, how="manual")
    return flash_redirect(_QUIET_TARGET, "settings.quiet.saved", "ok")
