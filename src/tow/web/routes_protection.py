"""Local-only configuration of independently optional clearance services."""

from __future__ import annotations

from typing import Annotated, Any

from fastapi import APIRouter, Form
from fastapi.responses import RedirectResponse, Response

from tow import access, undo
from tow.config import whole_number
from tow.store_transaction import StoreTransaction
from tow.web import edit_base, services, settings_merge
from tow.web.messages import flash_redirect
from tow.web.site_store import save_together

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
    base: Annotated[str, Form()] = "",
    resolutions: dict[str, str] = edit_base.RESOLUTIONS,
) -> Response:
    """Saving never contacts, installs or starts an external service. Only what this form
    changed since it was rendered is saved, on Site access as it is now (``settings_merge``): a
    limit changed in another tab survives, and a connection removed elsewhere comes back only
    when the owner chooses it in a conflict."""
    sent = {
        "enabled": enabled,
        "timeout_sec": timeout_sec,
        "min_interval_sec": min_interval_sec,
        "max_attempts": max_attempts,
        "provider_id": provider_id or [],
        "provider_kind": provider_kind or [],
        "provider_url": provider_url or [],
        "provider_order": provider_order or [],
        "provider_enabled": provider_enabled or [],
    }
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
    try:
        current = services.validate_protection_settings(cfg.get("protection"))
    except ValueError, TypeError:
        return flash_redirect(_TARGET, "protection_ui.invalid", "err")
    merged = _merged(current, configured, base, resolutions, sent)
    if isinstance(merged, Response):
        return merged
    if merged == current:
        return flash_redirect(_TARGET, "web.common.no_changes", "ok")
    cfg["protection"] = merged
    state = services.load_state()

    def write(txn: StoreTransaction) -> None:
        undo.invalidate(state, txn=txn)
        txn.save_config(cfg)
        txn.save_state(state)

    if refused := save_together(_TARGET, write):
        return refused
    return flash_redirect(_TARGET, "protection_ui.saved", "ok")


def _merged(
    current: dict[str, Any],
    configured: dict[str, Any],
    base: str,
    resolutions: dict[str, str],
    sent: dict[str, Any],
) -> dict[str, Any] | Response:
    """The form's save merged with Site access as saved now: the settings to save, or the
    refusal (reload, a conflict with the draft, or an invalid merged result). A form that only
    added connections while another save also only added some keeps both
    (``settings_merge.appended_connections``); this runs under the data lock, and the result is
    validated again below."""
    now, wanted = settings_merge.protection_now(current), settings_merge.protection_now(configured)
    shown = edit_base.read(settings_merge.PROTECTION, settings_merge.PROTECTION.kind, base, now)
    if shown is not None and "providers" not in shown.pending:
        union = settings_merge.appended_connections(
            str(shown.values.get("providers") or ""), current["providers"], configured["providers"]
        )
        if union is not None:
            # Proven: what is saved now is the form's base followed by additions, which the union
            # keeps - so the connections merge as if only this form had changed them.
            configured = {**configured, "providers": union}
            now = {**now, "providers": shown.values["providers"]}
            wanted = settings_merge.protection_now(configured)
    rows = settings_merge.protection_rows(tuple(settings_merge.PROTECTION.groups), now, wanted, current, configured)
    merge = settings_merge.merged(
        settings_merge.PROTECTION,
        _TARGET,
        base,
        now,
        wanted,
        resolutions,
        {},
        settings_merge.protection_draft(sent),
        rows=rows,
    )
    if isinstance(merge, RedirectResponse):
        return merge
    names = ("enabled", "timeout_sec", "min_interval_sec", "max_attempts")
    result = {**current, **{name: merge.values[name] for name in names}}
    result["providers"] = configured["providers"] if "providers" in merge.changed else current["providers"]
    try:
        result = services.validate_protection_settings(result)
    except ValueError, TypeError:
        return flash_redirect(_TARGET, "protection_ui.invalid", "err")
    if result["enabled"] and not any(item["enabled"] for item in result["providers"]):
        return flash_redirect(_TARGET, "protection_ui.invalid", "err")
    return result


@router.post("/settings/protection/remove", dependencies=[access.require_local(_TARGET, "protection_ui.local_only")])
@services.locked_state_mutation
def remove_protection_connection(provider_id: str = Form("")) -> Response:
    """One saved connection removed by its own button (P1): nothing else of the form - a draft in
    another card - is sent or saved. Enabled site access keeps at least one enabled connection."""
    cfg = services.load_config()
    try:
        current = services.validate_protection_settings(cfg.get("protection"))
        providers = [item for item in current["providers"] if item["id"] != provider_id]
        configured = services.validate_protection_settings({**current, "providers": providers})
    except ValueError, TypeError:
        return flash_redirect(_TARGET, "protection_ui.invalid", "err")
    if len(providers) == len(current["providers"]):
        return flash_redirect(_TARGET, "web.common.no_changes", "ok")
    if configured["enabled"] and not any(item["enabled"] for item in configured["providers"]):
        return flash_redirect(_TARGET, "protection_ui.remove_last", "err")
    cfg["protection"] = configured
    state = services.load_state()

    def write(txn: StoreTransaction) -> None:
        undo.invalidate(state, txn=txn)
        txn.save_config(cfg)
        txn.save_state(state)

    if refused := save_together(_TARGET, write):
        return refused
    return flash_redirect(_TARGET, "protection_ui.removed", "ok")
