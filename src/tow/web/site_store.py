"""Writes of the web pages that span stores: one store transaction (``tow.store_transaction``) for
config, state, secrets and the change's undo, and a site's login in the secrets."""

from __future__ import annotations

import copy
import logging
from collections.abc import Callable
from typing import Any

from fastapi.responses import RedirectResponse

from tow import store_transaction, undo
from tow.log import error_class
from tow.store_transaction import StoreTransaction
from tow.web import _context, services
from tow.web.views import flash_redirect

_LOG = logging.getLogger("tow.web")


def secret_undo_cleanup_pending() -> bool:
    """Cheap unlocked pre-check so ordinary requests never wait for the persistence lock."""
    try:
        state = _context.state(quarantine=False)  # the request's snapshot
    except Exception as exc:  # noqa: BLE001 - a request is never failed by this pre-check; the next one retries
        _LOG.warning("pending undo cleanup not checked: %s", type(exc).__name__)
        return False
    return undo.cleanup_needed(state)


def commit_site_stores(
    new_config: dict[str, Any],
    new_state: dict[str, Any],
    new_secrets: dict[str, Any],
    *,
    undo_fields: dict[str, Any] | None = None,
    secret_undo_snapshot: dict[str, Any] | None = None,
) -> None:
    """Config, state and secrets as one transaction, with the change's undo (its secret snapshot
    written inside the same transaction).

    Raises ``store_transaction.TransactionError`` with every store as it was before.
    """
    with store_transaction.transaction() as txn:
        if undo_fields is not None:
            undo.stamp(new_state, "site", txn=txn, snapshot=secret_undo_snapshot, **undo_fields)
        txn.save_config(new_config)
        txn.save_secrets(new_secrets)
        txn.save_state(new_state)


def save_together(target: str, write: Callable[[store_transaction.StoreTransaction], None]) -> RedirectResponse | None:
    """Run ``write`` as one store transaction (a settings save: config, state, secrets and the
    undo's snapshot together); None when it committed, else the refusal to show at ``target``."""
    try:
        with store_transaction.transaction() as txn:
            write(txn)
    except store_transaction.RollbackError as exc:
        services.log_event("settings_rollback_fail", error=error_class(exc), how="manual")
        return flash_redirect(target, "web.common.rollback_incomplete", "err")
    except store_transaction.TransactionError as exc:
        services.log_event(
            "settings_save_fail", status="restored", error=error_class(exc.__cause__ or exc), how="manual"
        )
        return flash_redirect(target, "web.settings.not_saved", "err")
    return None


def settings_undo(
    txn: StoreTransaction,
    state: dict[str, Any],
    secrets_before: dict[str, Any],
    old_sec: int,
    secret_scope: list[str] | None = None,
    old_ttl_sec: int | None = None,
) -> None:
    """The settings change's undo, its secrets snapshot written inside ``txn``."""
    fields: dict[str, Any] = {"interval_sec": old_sec, "secret_scope": secret_scope}
    if old_ttl_sec is not None:
        fields["flash_ttl_sec"] = old_ttl_sec
    undo.stamp(state, "settings", txn=txn, snapshot=secrets_before, **fields)


def save_secrets_with_undo(
    target: str, secrets_before: dict[str, Any], secrets: dict[str, Any], old_sec: int, secret_scope: list[str]
) -> RedirectResponse | None:
    """New secrets and the undo that puts the ``secret_scope`` part back, as one transaction."""
    state = services.load_state()

    def write(txn: StoreTransaction) -> None:
        settings_undo(txn, state, secrets_before, old_sec, secret_scope)
        txn.save_secrets(secrets)
        txn.save_state(state)

    return save_together(target, write)


def with_login(secrets: dict[str, Any], name: str, username: str, password: str) -> dict[str, Any]:
    """``secrets`` with the site's login as typed (an empty field keeps what is saved)."""
    updated = copy.deepcopy(secrets)
    entry = updated.setdefault("trackers", {}).setdefault(name, {})
    if username.strip():
        entry["username"] = username.strip()
    if password.strip():
        entry["password"] = password.strip()
    return updated


def save_tracker_login(
    name: str,
    username: str,
    password: str,
    *,
    base: dict[str, Any] | None = None,
) -> None:
    if not (username.strip() or password.strip()):
        return
    services.save_secrets(
        with_login(base if isinstance(base, dict) else services.load_secrets(), name, username, password)
    )


def restore_unverified_password(
    name: str, previous: dict[str, Any] | None, submitted_user: str, submitted_password: str
) -> bool:
    with services.persistence_lock():
        secrets = services.load_secrets()
        trackers = secrets.get("trackers")
        if not isinstance(trackers, dict):
            return False
        current = trackers.get(name)
        if not isinstance(current, dict):
            return False
        if current.get("username") != submitted_user or current.get("password") != submitted_password:
            return False
        for key in ("username", "password"):
            if isinstance(previous, dict) and key in previous:
                current[key] = previous[key]
            else:
                current.pop(key, None)
        if not current:
            trackers.pop(name, None)
        services.save_secrets(secrets)
        return True
