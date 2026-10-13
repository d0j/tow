"""Local-only configuration of independently optional clearance services."""

from __future__ import annotations

from typing import Annotated

from fastapi import APIRouter, Form
from fastapi.responses import Response

from tow import access, undo
from tow.config import whole_number
from tow.store_transaction import StoreTransaction
from tow.web import services
from tow.web.site_store import save_together
from tow.web.views import flash_redirect

router = APIRouter()
_TARGET = "/settings?open=protection#acc-protection"


@router.post("/settings/protection/reset", dependencies=[access.require_local(_TARGET, "protection_ui.local_only")])
def reset_protection() -> Response:
    # The service takes check_run_lock BEFORE persistence_lock; no mutation decorator here.
    outcome = services.reset_protection()
    messages = {
        "reset": "protection_ui.reset_done",
        "empty": "protection_ui.reset_empty",
        "busy": "protection_ui.reset_busy",
        "refused": "protection_ui.reset_refused",
    }
    return flash_redirect(
        _TARGET, messages.get(outcome, "protection_ui.reset_refused"), "ok" if outcome in {"reset", "empty"} else "err"
    )


@router.post("/settings/protection", dependencies=[access.require_local(_TARGET, "protection_ui.local_only")])
@services.locked_state_mutation
def settings_protection(
    enabled: str = Form(""),
    timeout_sec: str = Form(""),
    min_interval_sec: str = Form(""),
    max_attempts: str = Form(""),
    provider_id: Annotated[list[str] | None, Form()] = None,
    provider_kind: Annotated[list[str] | None, Form()] = None,
    provider_url: Annotated[list[str] | None, Form()] = None,
    provider_order: Annotated[list[str] | None, Form()] = None,
    provider_enabled: Annotated[list[str] | None, Form()] = None,
    provider_remove: Annotated[list[str] | None, Form()] = None,
) -> Response:
    """Saving never contacts, installs or starts an external service."""
    provider_id, provider_kind = provider_id or [], provider_kind or []
    provider_url, provider_order = provider_url or [], provider_order or []
    provider_enabled, provider_remove = provider_enabled or [], provider_remove or []
    try:
        lengths = {len(provider_id), len(provider_kind), len(provider_url), len(provider_order)}
        if len(lengths) != 1 or len(provider_id) > 16 or enabled not in {"", "1"}:
            raise ValueError("invalid provider fields")
        checked = set(provider_enabled)
        removed = set(provider_remove)
        if not checked <= set(provider_id) or not removed <= set(provider_id):
            raise ValueError("unknown provider control")
        entries = []
        priorities = set()
        for ident, kind, url, order in zip(provider_id, provider_kind, provider_url, provider_order, strict=True):
            if ident in removed:
                continue
            address = url.strip()
            if not address and ident not in checked:
                continue  # the disabled blank connection suggestions are not configuration
            priority = whole_number(order)
            if priority is None or not 1 <= priority <= 16 or priority in priorities:
                raise ValueError("invalid provider order")
            priorities.add(priority)
            entries.append((priority, {"id": ident, "kind": kind, "url": address, "enabled": ident in checked}))
        configured = services.validate_protection_settings(
            {
                "enabled": enabled == "1",
                "timeout_sec": whole_number(timeout_sec),
                "min_interval_sec": whole_number(min_interval_sec),
                "max_attempts": whole_number(max_attempts),
                "providers": [item for _, item in sorted(entries)],
            }
        )
        if configured["enabled"] and not any(p["enabled"] for p in configured["providers"]):
            raise ValueError("enabled protection has no provider")
    except ValueError, TypeError:
        return flash_redirect(_TARGET, "protection_ui.invalid", "err")
    cfg = services.load_config()
    if configured == services.validate_protection_settings(cfg.get("protection")):
        return flash_redirect(_TARGET, "web.common.no_changes", "ok")
    cfg["protection"] = configured
    state = services.load_state()

    def write(txn: StoreTransaction) -> None:
        undo.invalidate(state, txn=txn)
        txn.save_config(cfg)
        txn.save_state(state)

    if refused := save_together(_TARGET, write):
        return refused
    return flash_redirect(_TARGET, "protection_ui.saved", "ok")
