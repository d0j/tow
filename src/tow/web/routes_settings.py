"""Routes: the settings page and its cards - network access, language, theme, updates, check
interval and help. The torrent clients are in ``routes_clients``, the messengers with the quiet
hours and the digest in ``routes_notifiers``, copies of the data in ``routes_backup``, the service
in ``routes_service``, the password in ``routes_password``."""

from __future__ import annotations

import contextlib
import copy
from typing import Annotated, Any

from fastapi import APIRouter, Depends, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from tow import access, i18n, store_transaction, undo
from tow.auth import MAX_HINT_LENGTH, AuthConfigurationError, password_hint
from tow.bind import network_bind
from tow.clients.view import cards as client_cards
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
from tow.web import _context, edit_base, page_sync, services, settings_merge
from tow.web.messages import flash_redirect, request_flash
from tow.web.site_store import save_together, settings_undo
from tow.web.templating import TEMPLATES
from tow.web.text import format_bytes, t
from tow.web.views import (
    backup_view,
    client_notes,
    flash_ttl_sec,
    issue_notes,
    log_line,
    repeated_password,
    service_view,
    settings_draft,
)

router = APIRouter()


@router.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request) -> Response:
    # The page knows its revision and its sections' digests (page_sync): it asks for nothing
    # until another device or TOW changes what it shows.
    return page_sync.annotated_response(request, "/settings", settings_view(request, live=False))


def settings_view(request: Request, *, live: bool) -> Response:
    """The Settings page as drawn for ``request``. ``live``: drawn for the poll (``page_sync``),
    without the log, which is no fragment (its own section reads ``/log.json`` while open) and
    whose reading takes the log's lock."""
    from tow.clients.factory import ready as ready_clients
    from tow.log import format_event, index_event_titles, index_site_titles

    cfg = _context.config()  # read-only snapshots of this request (tow.web._context)
    secrets = _context.secrets_or_none()
    secret_store_error = secrets is None
    s = secrets or {}
    state = _context.state()
    topics = state.get("topics") or []
    title_index = index_event_titles(topics)
    site_titles = index_site_titles(cfg)
    clients = client_cards(cfg, s, topics)
    restore_points_error = False
    try:
        restore_points = services.list_restore_points()
    except RestorePointError:
        restore_points = []
        restore_points_error = True
    for point in restore_points:
        point["size_label"] = format_bytes(point["bytes"])
    backups = backup_view(cfg, request)
    service = service_view()
    conflict = edit_base.any_conflict(request)
    return TEMPLATES.TemplateResponse(
        request,
        "settings.html",
        {
            "title": t("web.title.settings"),
            "flash": request_flash(request),
            "secret_store_error": secret_store_error,
            "clients": clients,
            "client_types": [{"kind": spec.kind, "title": spec.title} for spec in ready_clients()],
            "notifiers": notifier_cards(s, state, local=access.is_local(request)),
            "search_words": _search_words(),
            # The card just saved / checked stays open (a messenger not connected yet, a working
            # client), as does the one a "Needs attention" link lands on (?spot=client-<id>).
            "open_card": str(request.query_params.get("card") or ""),
            "spot": str(request.query_params.get("spot") or ""),
            # The section a link opens (?open=clients), without a script too.
            "open_section": str(request.query_params.get("open") or ""),
            # The clients that do not answer (client_down): their card's note and pill.
            "clients_down": client_notes(state, cfg),
            "interval_min": _shown_minutes(interval_sec_of(cfg)),
            "interval_label": _interval_label(interval_sec_of(cfg)),
            "flash_ttl_min": _shown_minutes(flash_ttl_sec()),
            "allow_lan": as_bool(cfg.get("allow_lan")),
            "check_updates": as_bool(cfg.get("check_updates", True)),
            "replace_previous_revision": as_bool(cfg.get("replace_previous_revision")),
            # Notifications → quiet hours and the digest; a refused save's ?draft= brings back
            # what was typed (views.settings_draft).
            "quiet": settings_merge.quiet_view(cfg),
            "quiet_draft": settings_draft(request),
            # A refused save's recovery form draws the connections of its draft (settings_merge).
            "protection": settings_merge.protection_recovery(services.protection_settings_view(cfg), conflict),
            "lan_password_set": access.password_is_set(s),
            "lan_hint": password_hint(s.get("lan_auth")),
            "hint_max": MAX_HINT_LENGTH,
            "access_local": access.is_local(request),
            "service": service,
            "restore_points": restore_points,
            "restore_points_size": format_bytes(sum(point["bytes"] for point in restore_points)),
            "restore_points_error": restore_points_error,
            "backups": backups,
            "language_setting": _language_setting(cfg),
            "theme_setting": cfg.get("theme") if cfg.get("theme") in THEMES else "auto",
            "log_rows": []
            if live
            else [
                log_line(
                    format_event(e, title_index=title_index, site_titles=site_titles, paths=access.is_local(request)),
                    local=access.is_local(request),
                )
                for e in services.read_events(limit=200)
            ],
            # What needs the owner and is said in its card ("Needs attention" lands there).
            "notes": issue_notes(state, cfg, "settings", local=access.is_local(request)),
            # Each form's base (edit_base): what it shows, saved now; a refused save's ?conflict=
            # brings back the owner's draft, both values and the base to save it against.
            "bases": _bases(cfg, s, backup_folders=backups["folders"], service=service),
            "edit_conflict": conflict,
        },
    )


def _bases(
    cfg: dict[str, Any], secrets: dict[str, Any], *, backup_folders: dict[str, Any], service: dict[str, Any]
) -> dict[str, str]:
    """The hidden ``base`` of each Settings form that merges its save (``settings_merge``), and
    the network access card's revision (one guard for the whole card)."""
    from tow.clients.factory import client_configurations, client_secret_block
    from tow.clients.spec import get as client_get
    from tow.notifiers import kinds as notifier_kinds

    bases = {
        "intervals": edit_base.encode(settings_merge.INTERVALS, "intervals", settings_merge.intervals_now(cfg)),
        "language": edit_base.encode(settings_merge.LANGUAGE, "language", settings_merge.language_now(cfg)),
        "updates": edit_base.encode(settings_merge.UPDATES, "updates", settings_merge.updates_now(cfg)),
        "quiet": edit_base.encode(settings_merge.QUIET, "quiet", settings_merge.quiet_now(cfg)),
        "protection": edit_base.encode(
            settings_merge.PROTECTION,
            "protection",
            settings_merge.protection_now(services.validate_protection_settings(cfg.get("protection"))),
        ),
        "backup_automatic": edit_base.encode(
            settings_merge.BACKUP_AUTOMATIC, "backup-automatic", settings_merge.backup_automatic_now(cfg)
        ),
        "backup_retention": edit_base.encode(
            settings_merge.BACKUP_RETENTION, "backup-retention", settings_merge.backup_retention_now(cfg)
        ),
        "autostart": edit_base.encode(settings_merge.AUTOSTART, "autostart", settings_merge.autostart_now(service)),
        "access": edit_base.encode(
            settings_merge.ACCESS, "access", settings_merge.access_now(cfg, access.password_record(secrets))
        ),
    }
    for kind, folder in backup_folders.items():
        now = settings_merge.backup_folder_now(str(folder.get("value") or ""))
        bases[f"folder:{kind}"] = edit_base.encode(settings_merge.BACKUP_FOLDER, kind, now)
    for row in client_configurations(cfg):
        client_id = str(row["id"])
        spec = client_get(str(row.get("kind") or "").lower())
        try:
            block = client_secret_block(cfg, secrets, client_id)
        except RuntimeError, ValueError:
            continue
        now = settings_merge.client_now(client_id, block, spec.default_port if spec else 0)
        bases[f"client:{client_id}"] = edit_base.encode(settings_merge.CLIENT, client_id, now)
    for kind in notifier_kinds():
        spec_n = settings_merge.notifier_spec(kind)
        bases[f"notifier:{kind}"] = edit_base.encode(spec_n, kind, settings_merge.notifier_now(kind, secrets))
    return bases


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


_UPDATES_TARGET = "/settings?open=updates#acc-updates"


@router.post("/settings/updates")
@services.locked_state_mutation
def settings_updates(
    enabled: str = Form(""),
    base: str = Form(""),
    resolutions: dict[str, str] = edit_base.RESOLUTIONS,
) -> Response:
    cfg = services.load_config()
    merged = settings_merge.merged(
        settings_merge.UPDATES,
        _UPDATES_TARGET,
        base,
        settings_merge.updates_now(cfg),
        {"check_updates": as_bool(enabled)},
        resolutions,
        {"updates": t("releases.title")},
        {"check_updates": "1" if as_bool(enabled) else ""},
    )
    if isinstance(merged, RedirectResponse):
        return merged
    cfg["check_updates"] = merged.values["check_updates"]
    services.save_config(cfg)
    return flash_redirect(_UPDATES_TARGET, "releases.preferences_saved", "ok")


@router.post("/settings/language")
@services.locked_state_mutation
def settings_language(
    request: Request,
    auto: str = Form(""),
    language: str = Form(""),
    base: str = Form(""),
    resolutions: dict[str, str] = edit_base.RESOLUTIONS,
) -> Response:
    """One list: "auto" (the browser's language) or a language. ``auto=1`` (the older form's
    checkbox) still means automatic."""

    chosen = i18n.canonical(language)
    if chosen is None and language.strip() and language.strip().lower() != i18n.AUTO and not as_bool(auto):
        # A language TOW does not have (a page from before a language file was removed, a crafted
        # form) was saved as automatic with "language saved": said instead, nothing changed.
        return flash_redirect("/settings?open=language", "settings.language.unknown", "err")
    cfg = services.load_config()
    wanted = settings_merge.language_wanted(auto, language)
    merged = settings_merge.merged(
        settings_merge.LANGUAGE,
        "/settings?open=language",
        base,
        settings_merge.language_now(cfg),
        wanted,
        resolutions,
        {"language": t("settings.language.title")},
        {"language": str(wanted["language"])},
    )
    if isinstance(merged, RedirectResponse):
        return merged
    value = str(merged.values["language"])
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
    # One instant choice, saved in the background as it is made (app.js): the last one made on
    # any device is the theme, never a conflict with the page's own earlier choice.
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
def settings_access(
    allow_lan: Annotated[str, Form()] = "",
    lan_password: Annotated[str, Form()] = "",
    lan_password2: Annotated[str | None, Depends(repeated_password)] = None,
    hint: Annotated[str, Form()] = "",
    base: Annotated[str, Form()] = "",
) -> Response:
    """Network access on or off, on this PC only; the network always needs the password. With a
    password set the box saves alone. Without one, "Save and enable" sends the box with the new
    password and its reminder: both are saved together, with one Undo, or neither (F1). The card
    is indivisible: a form opened before another device changed it saves nothing."""
    cfg = services.load_config()
    if refused := _access_guard(cfg, base):
        return refused
    old_bind = str(cfg.get("bind") or "127.0.0.1")
    old_allow_lan = as_bool(cfg.get("allow_lan"))
    enabled = as_bool(allow_lan)
    secrets: dict[str, Any] | None = None
    record: dict[str, Any] | None = None
    if enabled:
        try:
            secrets = services.load_secrets()
        except SecretStoreError:
            if lan_password:  # never a password written over passwords and tokens that cannot be read
                return flash_redirect("/settings?open=access", "web.password.store_unavailable", "err")
            secrets = {}
        if access.password_is_set(secrets):
            if lan_password:  # a page opened before the password was set (another window)
                return flash_redirect("/settings?open=access", "setup.already_set", "warn")
        elif not lan_password:
            return flash_redirect("/settings?open=access", "web.settings.set_password_first", "warn")
        else:
            try:
                record = access.web_record(lan_password, lan_password2, hint)
            except AuthConfigurationError as exc:
                return flash_redirect("/settings?open=access", exc, "err")
    cfg["allow_lan"] = enabled
    cfg["bind"] = network_bind(old_bind) if enabled else "127.0.0.1"
    if record is not None:
        cfg["setup_done"] = True  # the first-start page is done from now on
    state = services.load_state()

    def write(txn: StoreTransaction) -> None:
        if record is None:
            undo.stamp(state, "settings_access", txn=txn, old_bind=old_bind, old_allow_lan=old_allow_lan)
        else:
            assert secrets is not None
            undo.stamp(
                state,
                "settings_access",
                txn=txn,
                snapshot=secrets,
                old_bind=old_bind,
                old_allow_lan=old_allow_lan,
                secret_scope=[access.RECORD_KEY],
            )
            txn.save_secrets({**copy.deepcopy(secrets), access.RECORD_KEY: record})
        txn.save_config(cfg)
        txn.save_state(state)

    if refused := save_together("/settings?open=access", write):
        return refused
    if record is not None:
        with contextlib.suppress(OSError):  # the first password: no device signed in with an earlier one
            services.sign_out_everywhere()
        services.log_event(
            "settings_password", changed="password", hint=bool(record.get("hint")), where="local", how="manual"
        )
    services.log_event("settings_access", allow_lan=enabled, bind=cfg["bind"], how="manual")
    if record is not None:
        return flash_redirect("/settings?open=access", "web.password.saved_and_enabled", "ok")
    # Off works at once (the middleware refuses the network by the config); on needs a restart.
    return flash_redirect(
        "/settings?open=access", "web.settings.access_saved" if enabled else "web.settings.access_closed", "ok"
    )


def _access_guard(cfg: dict[str, Any], base: str) -> Response | None:
    """The network access card's guard; passwords and tokens that cannot be read count as no
    password (the route then refuses on its own)."""
    try:
        record = access.password_record(services.load_secrets())
    except SecretStoreError:
        record = None
    return settings_merge.access_guard(cfg, record, base)


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
def settings_interval(
    interval_min: str = Form(""),
    flash_ttl_min: str = Form(""),
    replace_previous_revision: str = Form(""),  # a box: sent only when ticked
    base: str = Form(""),
    resolutions: dict[str, str] = edit_base.RESOLUTIONS,
) -> Response:
    cfg_before = services.load_config()
    old_sec = interval_sec_of(cfg_before)
    old_ttl_sec = flash_ttl(cfg_before.get("flash_ttl_sec"))
    old_replace = as_bool(cfg_before.get("replace_previous_revision"))
    # Only what this form changed since it was rendered: a field another device changed meanwhile
    # comes back as this page showed it and must not put the old value back.
    merged = settings_merge.merged(
        settings_merge.INTERVALS,
        "/settings?open=intervals",
        base,
        {
            "interval_min": _shown_minutes(old_sec),
            "flash_ttl_min": _shown_minutes(old_ttl_sec),
            "replace_previous_revision": old_replace,
        },
        settings_merge.intervals_wanted(interval_min, flash_ttl_min, replace_previous_revision),
        resolutions,
        {
            "interval": t("settings.intervals.check_topics"),
            "messages": t("settings.intervals.show_undo"),
            "replace": t("settings.intervals.replace_previous"),
        },
        {
            "interval_min": interval_min,
            "flash_ttl_min": flash_ttl_min,
            "replace_previous_revision": "1" if as_bool(replace_previous_revision) else "",
        },
    )
    if isinstance(merged, RedirectResponse):
        return merged
    interval_min = str(merged.values["interval_min"]).removeprefix("invalid:")
    flash_ttl_min = str(merged.values["flash_ttl_min"]).removeprefix("invalid:")
    new_replace = bool(merged.values["replace_previous_revision"])
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
    if new_sec == old_sec and new_ttl_sec == old_ttl_sec and new_replace == old_replace:
        if clamped:  # 5000 minutes is 1440, which it already was: say so, not only "no changes"
            kept = t("web.settings.clamp_kept", items=", ".join(clamped))
            return flash_redirect("/settings?open=intervals", t("web.common.no_changes") + kept, "warn")
        return flash_redirect("/settings?open=intervals", "web.common.no_changes", "ok")
    state = services.load_state()
    undo_secrets = copy.deepcopy(services.load_secrets())

    def write(txn: StoreTransaction) -> None:
        settings_undo(txn, state, undo_secrets, old_sec, [], old_ttl_sec, old_replace)
        if new_ttl_sec != old_ttl_sec:
            txn.set_flash_ttl_sec(new_ttl_sec)
        if new_replace != old_replace:
            txn.set_replace_previous_revision(new_replace)  # the next check reads it
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
    if new_replace != old_replace:
        services.log_event("settings_replace_previous", enabled=new_replace, how="manual")
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
