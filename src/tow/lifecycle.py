"""The TOW service as Settings sees it: its status, autostart and "Restart TOW".

TOW runs as one process, ``tow run`` (``tow.supervisor``); autostart is ``tow.autostart``. A
restart asked from Settings goes to the supervisor as a control request and is followed on
``data/service-restart.json`` (queued → stopping → starting → ready / failed).
"""

from __future__ import annotations

import json
import os
import time
import uuid
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from tow.config import load_config, port_of
from tow.paths import data_dir
from tow.store import atomic_write_text

_MARKER_NAME = "service-restart.json"


def service_restart_path() -> Path:
    return data_dir() / _MARKER_NAME


def _now() -> str:
    return datetime.now(UTC).isoformat()


def _write_marker(payload: dict[str, Any]) -> None:
    atomic_write_text(service_restart_path(), json.dumps(payload, ensure_ascii=False, indent=2) + "\n")


def update_restart_marker(operation_id: str, fields: dict[str, Any]) -> bool:
    """The supervisor reports a restart asked from Settings (stopping, starting, ready, failed).

    Only the operation the marker names is updated: a late report never overwrites a newer one.
    """
    current = _read_marker()
    if not current or current.get("operation_id") != operation_id:
        return False
    _write_marker({**current, **fields, "updated_at": _now()})
    return True


def _read_marker() -> dict[str, Any] | None:
    path = service_restart_path()
    if not path.is_file():
        return None
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except OSError, UnicodeError, json.JSONDecodeError:
        return {"status": "unknown", "error": "restart marker unreadable"}
    return value if isinstance(value, dict) else {"status": "unknown", "error": "restart marker invalid"}


# The settings page would ask the OS (schtasks, systemctl, launchctl) on every render; the
# registration only changes through set_autostart (which drops this cache) or by hand.
_AUTOSTART_CACHE_SECONDS = 60.0
_autostart_cache: tuple[float, dict[str, Any]] | None = None


def _autostart_status() -> dict[str, Any]:
    """The autostart of this OS (task "TOW", tow.service, io.tow) - read back, cached a minute."""
    global _autostart_cache
    now = time.monotonic()
    if _autostart_cache is not None and now - _autostart_cache[0] < _AUTOSTART_CACHE_SECONDS:
        return _autostart_cache[1]
    from tow.autostart import backend

    try:
        chosen = backend()
        status = {**chosen.status(), "supports_without_login": chosen.supports_without_login}
    except Exception as exc:  # noqa: BLE001 - a broken read-back is off, never a broken Settings page
        status = {"on": False, "error": str(exc)[:200], "supports_without_login": False}
    _autostart_cache = (now, status)
    return status


def _forget_cached() -> None:
    global _autostart_cache
    _autostart_cache = None


def service_status() -> dict[str, Any]:
    from tow.supervisor import layout

    autostart = _autostart_status()
    return {
        "pid": os.getpid(),
        "restart": _read_marker(),
        "autostart": bool(autostart.get("on")),
        "without_login": bool(autostart.get("without_login")),
        "supports_without_login": bool(autostart.get("supports_without_login")),
        "autostart_detail": autostart,
        "supervisor": layout.status() or None,
    }


def set_autostart(enabled: bool, *, without_login: bool = False) -> dict[str, Any]:
    """Turn autostart on or off (read back before it counts)."""
    _forget_cached()
    try:
        from tow.autostart import backend

        chosen = backend()
        return chosen.enable(without_login=without_login) if enabled else chosen.disable()
    finally:
        _forget_cached()  # the read-back right after must see the change


def request_restart(*, reason: str = "settings") -> dict[str, Any]:
    """Ask the running ``tow run`` to restart its web server; the marker follows the operation."""
    reason = reason if reason in {"settings", "restore", "manual"} else "manual"
    from tow.supervisor import layout

    if layout.running() is None:
        # A web server started by hand (``tow serve``) has nobody to start it again.
        return {"ok": False, "error": "TOW does not run as `tow run`: restart it by hand"}
    operation_id = f"restart-{uuid.uuid4().hex[:12]}"
    port = port_of(load_config())
    marker = {
        "operation_id": operation_id,
        "status": "queued",
        "pid": os.getpid(),
        "port": port,
        "via": "supervisor",
        "created_at": _now(),
        "reason": reason,
    }
    _write_marker(marker)
    layout.request("restart", by=reason, operation_id=operation_id)
    return {"ok": True, "operation_id": operation_id}


__all__ = [
    "request_restart",
    "service_restart_path",
    "service_status",
    "set_autostart",
    "update_restart_marker",
]
