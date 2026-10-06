"""Routes: the settings page and its cards - network access, language, torrent clients, check
interval and help. The messengers are in ``routes_notifiers``, copies of the data in
``routes_backup``, the service in ``routes_service``, the password in ``routes_password``."""

from __future__ import annotations

import copy
import ipaddress
import re
from collections.abc import Callable
from typing import Any
from urllib.parse import urlparse

from fastapi import APIRouter, Form, Request
from fastapi.responses import HTMLResponse, RedirectResponse, Response

from tow import access, i18n, store_transaction, undo
from tow.auth import MAX_HINT_LENGTH, password_hint
from tow.clock import format_ui_timestamp
from tow.config import INTERVAL_MAX_MINUTES, INTERVAL_MIN_MINUTES, as_bool, flash_ttl, interval_sec_of
from tow.log import error_class
from tow.notifiers import cards as notifier_cards
from tow.restore_points import RestorePointError
from tow.store import SecretStoreError
from tow.store_transaction import StoreTransaction
from tow.web import _context, services
from tow.web.routes_service import service_view
from tow.web.site_store import save_secrets_with_undo, save_together, settings_undo
from tow.web.templating import TEMPLATES
from tow.web.text import format_bytes, t
from tow.web.views import backup_view, flash_redirect, flash_ttl_sec, request_flash

router = APIRouter()


@router.get("/settings", response_class=HTMLResponse)
def settings_page(request: Request) -> Response:
    from tow.clients.factory import client_configurations, client_secret_block, default_client_id
    from tow.clients.factory import ready as ready_clients
    from tow.clients.spec import get as client_get
    from tow.log import format_event, index_event_titles, read_events

    cfg = _context.config()  # read-only snapshots of this request (tow.web._context)
    secrets = _context.secrets_or_none()
    secret_store_error = secrets is None
    s = secrets or {}
    state = _context.state()
    clients = []
    configurations = client_configurations(cfg)
    default_id = default_client_id(cfg)
    topics = state.get("topics") or []
    title_index = index_event_titles(topics)
    for index, configuration in enumerate(configurations):
        spec = client_get(str(configuration.get("kind") or "").lower())
        if spec is None or not spec.ready:
            continue
        q = client_secret_block(cfg, s, str(configuration["id"]))
        client_id = str(configuration["id"])
        clients.append(
            {
                "id": client_id,
                "form_id": f"client-{index}",
                "kind": spec.kind,
                "title": str(configuration.get("title") or spec.title),
                "host": q.get("host") or "",
                "port": q.get("port") or spec.default_port,
                "user": q.get("username") or "",
                "pass_set": bool(q.get("password")),
                "fields": [field.name for field in spec.fields],
                "labels": {field.name: field.label for field in spec.fields},
                "steps": list(spec.steps),
                "note": spec.note,
                "default": client_id == default_id,
                "multi": len(configurations) > 1,
                "used_by": _topics_using(topics, client_id, default_id),
            }
        )
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
            "interval_min": max(1, interval_sec_of(cfg) // 60),
            "interval_label": _interval_label(interval_sec_of(cfg)),
            "flash_ttl_min": max(1, flash_ttl_sec() // 60),
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
            "log_rows": [format_event(e, title_index=title_index) for e in read_events(limit=200)],
        },
    )


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
def settings_language(auto: str = Form(""), language: str = Form("")) -> Response:
    """One list: "auto" (the browser's language) or a language. ``auto=1`` (the older form's
    checkbox) still means automatic."""

    cfg = services.load_config()
    chosen = i18n.canonical(language)
    value = i18n.AUTO if as_bool(auto) or chosen is None else chosen
    cfg["language"] = value
    services.save_config(cfg)
    if value != i18n.AUTO:
        i18n.use(value)
    services.log_event("settings_language", language=value, how="manual")
    return flash_redirect("/settings?open=language", "settings.language.saved", "ok")


def _topics_using(topics: list[dict[str, Any]], client_id: str, default_id: str) -> int:
    return sum(1 for topic in topics if str(topic.get("client_id") or default_id) == client_id)


def _clients_redirect(message: Any, kind: str = "ok", /, **params: Any) -> RedirectResponse:
    return flash_redirect("/settings?open=clients", message, kind, **params)


def _change_clients(
    change: Callable[[dict[str, Any], dict[str, Any]], object], message: str, event: str, **fields: Any
) -> RedirectResponse:
    """Apply ``change(cfg, secrets)`` to the client list; the owner can undo it."""
    cfg = services.load_config()
    s = services.load_secrets()
    before = {key: copy.deepcopy(cfg.get(key)) for key in ("client", "clients")}
    secrets_before = copy.deepcopy(s)
    try:
        change(cfg, s)
    except ValueError as exc:
        return _clients_redirect(exc, "err")
    state = services.load_state()

    def write(txn: StoreTransaction) -> None:
        undo.stamp(
            state, "settings_clients", txn=txn, snapshot=secrets_before, config_before=before, secret_scope=["clients"]
        )
        txn.save_secrets(s)
        txn.save_config(cfg)
        txn.save_state(state)

    if refused := save_together("/settings?open=clients", write):
        return refused
    services.log_event(event, **fields, how="manual")
    return _clients_redirect(message)


@router.post("/settings/client/add")
@services.locked_state_mutation
def settings_client_add(kind: str = Form("")) -> Response:
    from tow.clients.factory import add_client
    from tow.clients.spec import get as client_get

    spec = client_get(kind.strip().lower())
    if spec is None or not spec.ready:
        return _clients_redirect("web.settings.no_such_client", "err")
    added: list[str] = []
    return _change_clients(
        lambda cfg, s: added.append(add_client(cfg, s, spec.kind)),
        t("web.settings.client_added", title=spec.title),
        "settings_client_add",
        client_kind=spec.kind,
    )


@router.post("/settings/client/remove")
@services.locked_state_mutation
def settings_client_remove(client_id: str = Form("")) -> Response:
    from tow.clients.factory import default_client_id, remove_client

    cfg = services.load_config()
    used_by = _topics_using(services.load_state().get("topics") or [], client_id, default_client_id(cfg))
    return _change_clients(
        lambda cfg, s: remove_client(cfg, s, client_id, used_by),
        t("web.settings.client_removed"),
        "settings_client_remove",
        client_id=client_id,
    )


@router.post("/settings/client/default")
@services.locked_state_mutation
def settings_client_default(client_id: str = Form("")) -> Response:
    from tow.clients.factory import set_default_client

    return _change_clients(
        lambda cfg, _s: set_default_client(cfg, client_id),
        t("web.settings.default_client"),
        "settings_client_default",
        client_id=client_id,
    )


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
    cfg["bind"] = "0.0.0.0" if enabled else "127.0.0.1"
    state = services.load_state()

    def write(txn: StoreTransaction) -> None:
        undo.stamp(state, "settings_access", txn=txn, old_bind=old_bind, old_allow_lan=old_allow_lan)
        txn.save_config(cfg)
        txn.save_state(state)

    if refused := save_together("/settings?open=access", write):
        return refused
    services.log_event("settings_access", allow_lan=enabled, bind=cfg["bind"], how="manual")
    return flash_redirect("/settings?open=access", "web.settings.access_saved", "ok")


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


@router.post("/settings/client")
@services.locked_state_mutation
def settings_client(
    client_id: str = Form(""),
    kind: str = Form("qbittorrent"),
    host: str = Form(""),
    port: str = Form("8080"),
    username: str = Form(""),
    password: str = Form(""),
) -> Response:
    from tow.clients.factory import client_configuration, client_secret_block
    from tow.clients.spec import get as client_get

    cfg = services.load_config()
    try:
        configuration = client_configuration(cfg, client_id or None)
    except (RuntimeError, ValueError) as e:
        return flash_redirect("/settings?open=clients", e, "err")
    selected_kind = str(configuration.get("kind") or kind).lower()
    spec = client_get(selected_kind)
    if spec is None or not spec.ready or (kind.strip() and kind.strip().lower() != selected_kind):
        return flash_redirect("/settings?open=clients", "web.settings.no_such_client", "err")
    s = services.load_secrets()
    undo_secrets = copy.deepcopy(s)
    old_sec = interval_sec_of(cfg)
    try:
        port_number = int(port.strip() or spec.default_port)
    except ValueError:
        port_number = 0
    if not 1 <= port_number <= 65535:
        return flash_redirect("/settings?open=clients", "web.settings.bad_port", "err")
    if host.strip() and not _client_host_ok(host.strip()):
        return flash_redirect("/settings?open=clients", "web.settings.bad_client_host", "err")
    q = client_secret_block(cfg, s, str(configuration["id"]), ensure=True)
    endpoint_changed = (str(q.get("host") or "").strip(), q.get("port"), str(q.get("username") or "").strip()) != (
        host.strip(),
        port_number,
        username.strip(),
    )
    if endpoint_changed and q.get("password") and not password.strip():
        # A stored password must never be sent to a new (possibly mistyped) address.
        return flash_redirect("/settings?open=clients", "web.settings.password_again", "warn")
    q["host"] = host.strip()
    q["port"] = port_number
    q["username"] = username.strip()
    if password.strip():
        q["password"] = password.strip()
    secret_scope = (
        ["clients", str(configuration.get("secrets_ref") or configuration["id"])]
        if cfg.get("clients")
        else [spec.secrets_key]
    )
    if refused := save_secrets_with_undo("/settings?open=clients", undo_secrets, s, old_sec, secret_scope):
        return refused
    services.log_event("settings_client", client_id=configuration["id"], client_kind=selected_kind, how="manual")
    return flash_redirect("/settings?open=clients", "web.common.saved", "ok")


_HOST_LABEL = re.compile(r"(?!-)[a-z0-9_-]{1,63}(?<!-)")
_CLIENT_PATH = re.compile(r"(/[A-Za-z0-9._~%+-]+)*/?")


def _client_host_ok(value: str) -> bool:
    """A client's Web UI address: a computer name or an IP address, optionally with http(s)://,
    a port and a path (a client behind a proxy) - never markup, spaces or ``..``."""
    try:
        parsed = urlparse(value if "://" in value else f"http://{value}")
        name = parsed.hostname or ""
        _ = parsed.port  # a port that is not a number raises
        ascii_name = name.encode("idna").decode("ascii")
    except UnicodeError, ValueError:
        return False
    if parsed.scheme not in {"http", "https"} or parsed.username or parsed.password or parsed.query:
        return False
    if parsed.fragment or ".." in parsed.path.split("/") or not _CLIENT_PATH.fullmatch(parsed.path):
        return False
    try:
        ipaddress.ip_address(name)
    except ValueError:
        labels = ascii_name.rstrip(".").split(".")
        return len(ascii_name) <= 253 and all(_HOST_LABEL.fullmatch(label) for label in labels)
    return True


@router.post("/settings/client/ping")
def settings_client_ping(client_id: str = Form("")) -> Response:
    from tow.clients.factory import from_secrets as client_from_secrets

    try:
        cfg = services.load_config()
        version = client_from_secrets(cfg, services.load_secrets(), client_id or None).ping()
        msg, kind = t("web.settings.ping_ok", version=version), "ok"
    except Exception as e:  # noqa: BLE001 - a client library fails in its own ways: "Check" names the class only
        reason = _ping_reason(e)
        msg = t("web.settings.ping_failed", error=reason) if reason else t("web.settings.ping_silent")
        kind = "err"
        services.log_event("qbit_unreachable", client_id=client_id or None, error=reason, cls="qbit", how="manual")
    return flash_redirect("/settings?open=clients", msg, kind)


def _ping_reason(exc: Exception) -> str:
    """What "Check" says about a client that did not answer.

    TOW's own client errors are plain words written for the owner. Anything else (the
    qBittorrent library, sockets) is reduced to a class: its raw text names hosts, ports and
    socket errors, which turned the button into a probe of which ports are open where.
    """
    from tow.clients.managed import ClientError

    if isinstance(exc, ClientError):
        return str(exc)
    name = type(exc).__name__.casefold()
    if any(word in name for word in ("login", "unauthorized", "forbidden")):
        return t("web.settings.ping_login")
    return t("web.settings.ping_unreachable")


@router.post("/settings/interval")
@services.locked_state_mutation
def settings_interval(interval_min: str = Form("60"), flash_ttl_min: str = Form("1")) -> Response:
    cfg_before = services.load_config()
    old_sec = interval_sec_of(cfg_before)
    old_ttl_sec = flash_ttl(cfg_before.get("flash_ttl_sec"))
    try:
        requested, requested_ttl = int(interval_min.strip()), int(flash_ttl_min.strip())
    except ValueError:
        # Nothing is guessed: "abc" or "1.5" is refused, not silently replaced by a default.
        return flash_redirect("/settings?open=intervals", "web.settings.bad_minutes", "err")
    minutes = max(INTERVAL_MIN_MINUTES, min(INTERVAL_MAX_MINUTES, requested))
    new_sec = minutes * 60
    ttl_min = max(1, min(30, requested_ttl))
    # D3: a clamped value is said, not silently replaced.
    clamped = []
    if minutes != requested:
        clamped.append(
            t("web.settings.clamped_interval", minutes=minutes, low=INTERVAL_MIN_MINUTES, high=INTERVAL_MAX_MINUTES)
        )
    if ttl_min != requested_ttl:
        clamped.append(t("web.settings.clamped_messages", minutes=ttl_min))
    clamp_note = t("web.settings.clamp_note", items=", ".join(clamped)) if clamped else ""
    if new_sec == old_sec and ttl_min * 60 == old_ttl_sec:
        if clamped:  # 5000 minutes is 1440, which it already was: say so, not only "no changes"
            kept = t("web.settings.clamp_kept", items=", ".join(clamped))
            return flash_redirect("/settings?open=intervals", t("web.common.no_changes") + kept, "warn")
        return flash_redirect("/settings?open=intervals", "web.common.no_changes", "ok")
    state = services.load_state()
    undo_secrets = copy.deepcopy(services.load_secrets())

    def write(txn: StoreTransaction) -> None:
        settings_undo(txn, state, undo_secrets, old_sec, [], old_ttl_sec)
        txn.set_flash_ttl_sec(ttl_min * 60)
        if new_sec != old_sec:
            txn.set_interval_sec(new_sec)  # `tow run` reads it from the config within a minute
        txn.save_state(state)

    try:
        with store_transaction.transaction() as txn:
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
