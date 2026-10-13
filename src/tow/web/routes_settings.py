"""Routes: the settings page and its cards - network access, language, theme, updates, check
interval and help. The torrent clients are in ``routes_clients``, the messengers in
``routes_notifiers``, copies of the data in ``routes_backup``, the service in ``routes_service``,
the password in ``routes_password``."""

from __future__ import annotations

import copy
from typing import Any

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from tow import access, i18n, store_transaction, undo
from tow.auth import MAX_HINT_LENGTH, password_hint
from tow.bind import network_bind
from tow.clients.view import cards as client_cards
from tow.clock import format_ui_timestamp
from tow.config import (
    INTERVAL_MAX_MINUTES,
    INTERVAL_MIN_MINUTES,
    THEMES,
    as_bool,
    flash_ttl,
    interval_sec_of,
    whole_number,
)
from tow.log import error_class
from tow.notifiers import cards as notifier_cards
from tow.restore_points import RestorePointError
from tow.store import SecretStoreError
from tow.store_transaction import StoreTransaction
from tow.web import _context, services
from tow.web.site_store import save_together, settings_undo
from tow.web.templating import TEMPLATES
from tow.web.text import format_bytes, t
from tow.web.views import backup_view, flash_redirect, flash_ttl_sec, issue_notes, request_flash, service_view

router = APIRouter()


@router.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request) -> Response:
    from tow.clients.factory import ready as ready_clients
    from tow.log import format_event, index_event_titles

    cfg = _context.config()  # read-only snapshots of this request (tow.web._context)
    secrets = _context.secrets_or_none()
    secret_store_error = secrets is None
    s = secrets or {}
    state = _context.state()
    topics = state.get("topics") or []
    title_index = index_event_titles(topics)
    clients = client_cards(cfg, s, topics)
    restore_points_error = False
    try:
        restore_points = services.list_restore_points()
    except RestorePointError:
        restore_points = []
        restore_points_error = True
    for point in restore_points:
        point["created_label"] = format_ui_timestamp(str(point["created_at"]))
        point["size_label"] = format_bytes(point["bytes"])
    return TEMPLATES.TemplateResponse(
        request,
        "settings.html",
        {
            "title": t("web.title.settings"),
            "flash": request_flash(request),
            "secret_store_error": secret_store_error,
            "clients": clients,
            "client_types": [{"kind": spec.kind, "title": spec.title} for spec in ready_clients()],
            "notifiers": notifier_cards(s, state),
            "search_words": _search_words(),
            # The messenger just saved / checked stays open even when it is not connected yet.
            "open_card": str(request.query_params.get("card") or ""),
            # The section a link opens (?open=clients), without a script too.
            "open_section": str(request.query_params.get("open") or ""),
            "clients_down": _clients_down(state, cfg),
            "interval_min": _shown_minutes(interval_sec_of(cfg)),
            "interval_label": _interval_label(interval_sec_of(cfg)),
            "flash_ttl_min": _shown_minutes(flash_ttl_sec()),
            "allow_lan": as_bool(cfg.get("allow_lan")),
            "check_updates": as_bool(cfg.get("check_updates", True)),
            "lan_password_set": access.password_is_set(s),
            "lan_hint": password_hint(s.get("lan_auth")),
            "hint_max": MAX_HINT_LENGTH,
            "access_local": access.is_local(request),
            "service": service_view(),
            "restore_points": restore_points,
            "restore_points_size": format_bytes(sum(point["bytes"] for point in restore_points)),
            "restore_points_error": restore_points_error,
            "backups": backup_view(cfg, request),
            "language_setting": _language_setting(cfg),
            "theme_setting": cfg.get("theme") if cfg.get("theme") in THEMES else "auto",
            "log_rows": [format_event(e, title_index=title_index) for e in services.read_events(limit=200)],
            # What needs the owner and is said in its card ("Needs attention" lands there).
            "notes": issue_notes(state, cfg, "settings"),
        },
    )


def _clients_down(state: dict[str, Any], cfg: dict[str, Any]) -> set[str]:
    """The clients that did not answer: the last check's word on each (the main one's is
    ``qbit_ok``), or Settings' "Check" since then (``ping_failed``)."""
    from tow.clients.factory import default_client_id
    from tow.jsonish import as_dict

    health = as_dict(state.get("health"))
    down = {str(client) for client, ok in as_dict(health.get("clients_ok")).items() if ok is False}
    down |= {str(client) for client in as_dict(health.get("ping_failed"))}
    if health.get("qbit_ok") is False:
        down.add(default_client_id(cfg))
    return down


def _search_words() -> dict[str, str]:
    """What the settings search finds a card by: its words plus every plugin's name, so a new
    client or messenger is found without editing a language file."""
    from tow.clients.factory import ready as ready_clients
    from tow.notifiers import kinds as notifier_kinds
    from tow.notifiers import title as notifier_title

    clients = [word for spec in ready_clients() for word in (spec.kind, spec.title, spec.short)]
    messengers = [
        word for kind, module in notifier_kinds().items() for word in (kind, str(module.TITLE), notifier_title(module))
    ]
    out = {}
    for section, names in (("clients", clients), ("notify", messengers)):
        words = [*t(f"settings.{section}.q").split(), *" ".join(names).split()]
        out[section] = " ".join(dict.fromkeys(word.casefold() for word in words))
    return out


def _language_setting(cfg: dict[str, Any]) -> dict[str, Any]:

    chosen = i18n.setting(cfg)
    names = {code: native for code, _name, native in i18n.available()}
    # Found by "language" in every language TOW has, and by each language's own name.
    words = [i18n.translate("settings.language.title", code) for code in names]
    words += [word for code, name, native in i18n.available() for word in (code, name, native)]
    return {
        "auto": chosen == i18n.AUTO,
        "chosen": chosen,
        "current": i18n.current(),
        "current_name": names.get(i18n.current(), i18n.current()),
        "search": " ".join(dict.fromkeys(word.casefold() for word in ["language", *words])),
    }


@router.post("/settings/updates")
@services.locked_state_mutation
def settings_updates(enabled: str = Form("")) -> Response:
    cfg = services.load_config()
    cfg["check_updates"] = as_bool(enabled)
    services.save_config(cfg)
    return flash_redirect("/settings?open=updates#acc-updates", "releases.preferences_saved", "ok")


@router.post("/settings/language")
@services.locked_state_mutation
def settings_language(request: Request, auto: str = Form(""), language: str = Form("")) -> Response:
    """One list: "auto" (the browser's language) or a language. ``auto=1`` (the older form's
    checkbox) still means automatic."""

    chosen = i18n.canonical(language)
    if chosen is None and language.strip() and language.strip().lower() != i18n.AUTO and not as_bool(auto):
        # A language TOW does not have (a page from before a language file was removed, a crafted
        # form) was saved as automatic with "language saved": said instead, nothing changed.
        return flash_redirect("/settings?open=language", "settings.language.unknown", "err")
    cfg = services.load_config()
    value = i18n.AUTO if as_bool(auto) or chosen is None else chosen
    cfg["language"] = value
    services.save_config(cfg)
    # The message is in the language the next page has: "Auto" chosen on a Russian page of an
    # English browser said "Язык сохранён" above an English page.
    i18n.use(i18n.for_request(cfg, request.headers.get("accept-language")))
    services.log_event("settings_language", language=value, how="manual")
    return flash_redirect("/settings?open=language", "settings.language.saved", "ok")


@router.post("/settings/theme")
@services.locked_state_mutation
def settings_theme(theme: str = Form("")) -> Response:
    """Settings → Theme: the pages' colours for every device - as the system, light or dark."""
    if theme not in THEMES:  # was saved as "auto" with "theme saved"; the page sends only these
        return flash_redirect("/settings?open=theme", "settings.theme.unknown", "err")
    cfg = services.load_config()
    cfg["theme"] = theme
    services.save_config(cfg)
    services.log_event("settings_theme", theme=cfg["theme"], how="manual")
    return flash_redirect("/settings?open=theme", "settings.theme.saved", "ok")


# Otherwise anyone on the network could close it and lock the owner's devices out.
@router.post(
    "/settings/access",
    dependencies=[access.require_local("/settings?open=access", "web.settings.access_local_only")],
)
@services.locked_state_mutation
def settings_access(allow_lan: str = Form("")) -> Response:
    """Network access on or off, on this PC only; the network always needs the password. The
    password itself is set on the password card (/settings/password, with its reminder)."""
    cfg = services.load_config()
    old_bind = str(cfg.get("bind") or "127.0.0.1")
    old_allow_lan = as_bool(cfg.get("allow_lan"))
    enabled = as_bool(allow_lan)
    if enabled:
        try:
            existing_secrets = services.load_secrets()
        except SecretStoreError:
            existing_secrets = {}
        if not access.password_is_set(existing_secrets):
            return flash_redirect("/settings?open=access", "web.settings.set_password_first", "warn")
    cfg["allow_lan"] = enabled
    cfg["bind"] = network_bind(old_bind) if enabled else "127.0.0.1"
    state = services.load_state()

    def write(txn: StoreTransaction) -> None:
        undo.stamp(state, "settings_access", txn=txn, old_bind=old_bind, old_allow_lan=old_allow_lan)
        txn.save_config(cfg)
        txn.save_state(state)

    if refused := save_together("/settings?open=access", write):
        return refused
    services.log_event("settings_access", allow_lan=enabled, bind=cfg["bind"], how="manual")
    # Off works at once (the middleware refuses the network by the config); on needs a restart.
    return flash_redirect(
        "/settings?open=access", "web.settings.access_saved" if enabled else "web.settings.access_closed", "ok"
    )


def _interval_label(seconds: int) -> str:
    minutes = max(1, seconds // 60)
    if minutes % 60 == 0:
        hours = minutes // 60
        return t("web.settings.hours", count=hours)
    return t("web.settings.minutes", count=minutes)


def _log_files() -> int:
    from tow.log import BACKUPS

    return BACKUPS + 1


def _log_bytes() -> int:
    from tow.log import MAX_BYTES

    return MAX_BYTES


@router.get("/settings/help", response_class=HTMLResponse)
def settings_help(request: Request) -> Response:
    return TEMPLATES.TemplateResponse(
        request,
        "settings_help.html",
        {
            "title": t("web.title.help"),
            "flash": request_flash(request),
            "interval_label": _interval_label(interval_sec_of(_context.config())),
            "log_files": _log_files(),
            "log_size": format_bytes(_log_bytes()),
        },
    )


def _shown_minutes(seconds: int) -> int:
    """What the Checks form shows for a setting kept in seconds (whole minutes, at least 1)."""
    return max(1, seconds // 60)


# Empty defaults: a field sent empty is refused below, not taken as FastAPI's default value.
@router.post("/settings/interval")
@services.locked_state_mutation
def settings_interval(interval_min: str = Form(""), flash_ttl_min: str = Form("")) -> Response:
    cfg_before = services.load_config()
    old_sec = interval_sec_of(cfg_before)
    old_ttl_sec = flash_ttl(cfg_before.get("flash_ttl_sec"))
    requested, requested_ttl = whole_number(interval_min), whole_number(flash_ttl_min)
    if requested is None or requested_ttl is None:
        # Nothing is guessed: "", "abc", "1.5" or "٦٠" is refused, not replaced by a default.
        return flash_redirect("/settings?open=intervals", "web.settings.bad_minutes", "err")
    # A field sent back as the page showed it keeps its value: config.yaml may hold one the
    # form cannot show (45 s, 10 min), and changing the other field must not round or clamp it.
    same, same_ttl = requested == _shown_minutes(old_sec), requested_ttl == _shown_minutes(old_ttl_sec)
    minutes = requested if same else max(INTERVAL_MIN_MINUTES, min(INTERVAL_MAX_MINUTES, requested))
    ttl_min = requested_ttl if same_ttl else max(1, min(30, requested_ttl))
    new_sec = old_sec if same else minutes * 60
    new_ttl_sec = old_ttl_sec if same_ttl else ttl_min * 60
    # D3: a clamped value is said, not silently replaced.
    clamped = []
    if minutes != requested:
        clamped.append(
            t("web.settings.clamped_interval", minutes=minutes, low=INTERVAL_MIN_MINUTES, high=INTERVAL_MAX_MINUTES)
        )
    if ttl_min != requested_ttl:
        clamped.append(t("web.settings.clamped_messages", minutes=ttl_min))
    clamp_note = t("web.settings.clamp_note", items=", ".join(clamped)) if clamped else ""
    if new_sec == old_sec and new_ttl_sec == old_ttl_sec:
        if clamped:  # 5000 minutes is 1440, which it already was: say so, not only "no changes"
            kept = t("web.settings.clamp_kept", items=", ".join(clamped))
            return flash_redirect("/settings?open=intervals", t("web.common.no_changes") + kept, "warn")
        return flash_redirect("/settings?open=intervals", "web.common.no_changes", "ok")
    state = services.load_state()
    undo_secrets = copy.deepcopy(services.load_secrets())

    def write(txn: StoreTransaction) -> None:
        settings_undo(txn, state, undo_secrets, old_sec, [], old_ttl_sec)
        if new_ttl_sec != old_ttl_sec:
            txn.set_flash_ttl_sec(new_ttl_sec)
        if new_sec != old_sec:
            txn.set_interval_sec(new_sec)  # `tow run` reads it from the config within a minute
        txn.save_state(state)

    try:
        with services.store_transaction() as txn:
            write(txn)
    except store_transaction.TransactionError as exc:
        return _interval_not_saved(exc, new_sec)
    if new_sec != old_sec:
        services.log_event("settings_interval", interval_sec=new_sec, how="manual")
    return flash_redirect(
        "/settings?open=intervals", t("web.common.saved") + clamp_note, "warn" if clamp_note else "ok"
    )


def _interval_not_saved(exc: store_transaction.TransactionError, new_sec: int) -> RedirectResponse:
    """The stores are back as they were (or say they could not be put back)."""
    if isinstance(exc, store_transaction.RollbackError):
        services.log_event("settings_rollback_fail", error=error_class(exc), how="manual")
        return flash_redirect("/settings?open=intervals", "web.common.rollback_incomplete", "err")
    cause = exc.__cause__ or exc
    services.log_event("settings_interval_fail", requested_sec=new_sec, error=str(cause), how="manual")
    return flash_redirect("/settings?open=intervals", "web.settings.not_saved", "err")
