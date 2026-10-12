"""Routes: the torrent clients in Settings - add, save, check, choose the main one and remove a
client (``tow.clients``; their cards are made by ``tow.clients.view``)."""

from __future__ import annotations

import copy
import ipaddress
import re
from collections.abc import Callable
from typing import Any
from urllib.parse import urlparse

from fastapi import APIRouter, Form
from fastapi.responses import RedirectResponse, Response

from tow import undo
from tow.clients.spec import DEFAULT_KIND
from tow.clients.view import topics_using
from tow.config import interval_sec_of, whole_number
from tow.store_transaction import StoreTransaction
from tow.web import services
from tow.web.site_store import save_secrets_with_undo, save_together
from tow.web.text import t
from tow.web.views import flash_redirect

router = APIRouter()


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
    used_by = topics_using(services.load_state().get("topics") or [], client_id, default_client_id(cfg))
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


@router.post("/settings/client")
@services.locked_state_mutation
def settings_client(
    client_id: str = Form(""),
    kind: str = Form(DEFAULT_KIND),
    host: str = Form(""),
    port: str = Form(""),  # empty: the client's own default port (Transmission 9091, not 8080)
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
    port_number = whole_number(port) if port.strip() else spec.default_port
    if port_number is None or not 1 <= port_number <= 65535:
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
    if password.strip():  # empty keeps the saved one; a typed one is kept as typed, spaces included
        q["password"] = password
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
    try:
        # One quick try first: nothing listening fails in seconds, not after the library's retries.
        if not services.client_answers(client_id or None):
            raise ConnectionError  # answered as the client library's own "no connection"
        cfg = services.load_config()
        version = services.client_from_secrets(cfg, services.load_secrets(), client_id or None).ping()
    except Exception as e:  # noqa: BLE001 - a client library fails in its own ways: "Check" names the class only
        reason = _ping_reason(e)
        msg = t("web.settings.ping_failed", error=reason) if reason else t("web.settings.ping_silent")
        kind = "err"
        services.log_event("qbit_unreachable", client_id=client_id or None, error=reason, cls="qbit", how="manual")
        _client_answered(client_id, answered=False)
    else:
        # Outside the client's "except": a save that fails (a data file held by another
        # program) is the app's "try again", never "no connection" for a client that answered.
        msg, kind = t("web.settings.ping_ok", version=version), "ok"
        _client_answered(client_id)
    return flash_redirect("/settings?open=clients", msg, kind)


def _client_answered(client_id: str, *, answered: bool = True) -> None:
    """The header and Settings show what "Check" found at once, not at the next check: a
    client set up (or repaired) and checked here stayed red or grey until then, and one that
    refused the password here stayed green. An answer is recorded as a check records it; a
    failure only as a mark (``ping_failed``) the next check drops - recorded as the check's
    own "not answering", it would swallow that check's message about the client."""
    import time

    from tow.clients.factory import client_configurations, default_client_id

    with services.persistence_lock():
        state = services.load_state()
        raw = state.get("health")
        health: dict[str, Any] = dict(raw) if isinstance(raw, dict) else {}
        cfg = services.load_config()
        default = default_client_id(cfg)
        wanted = client_id or default
        if wanted not in {str(client["id"]) for client in client_configurations(cfg)}:
            return  # a form naming no configured client: nothing about it to record
        marks = health.get("ping_failed")
        failed = {k: v for k, v in (marks.items() if isinstance(marks, dict) else ()) if k != wanted}
        if answered:
            if wanted == default:
                health["qbit_ok"] = True
            clients = health.get("clients_ok")
            health["clients_ok"] = {**(clients if isinstance(clients, dict) else {}), wanted: True}
        else:
            failed[wanted] = int(time.time())
        if failed:
            health["ping_failed"] = failed
        else:
            health.pop("ping_failed", None)
        state["health"] = health
        services.save_state(state)


def _ping_reason(exc: Exception) -> str:
    """What "Check" says about a client that did not answer.

    TOW's own client errors are plain words written for the owner. Anything else (the
    qBittorrent library, sockets) is reduced to a class: its raw text names hosts, ports and
    socket errors, which turned the button into a probe of which ports are open where.
    """
    from tow.clients.managed import ClientError

    if isinstance(exc, ClientError):
        # Every client says "does not answer" and "login refused" in the same words as
        # qBittorrent's, not with the network library's class name ("(ConnectError)").
        kind = exc.code.rpartition(".")[2]
        if kind in {"no_connection", "no_web"}:
            return t("web.settings.ping_unreachable")
        if kind == "bad_login":
            return t("web.settings.ping_login")
        return str(exc)
    name = type(exc).__name__.casefold()
    if any(word in name for word in ("login", "unauthorized", "forbidden")):
        return t("web.settings.ping_login")
    return t("web.settings.ping_unreachable")
