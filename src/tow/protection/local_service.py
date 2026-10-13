"""Opt-in runtime manifests: no installer, autostart or arbitrary executable settings."""

from __future__ import annotations

import socket
import sys
import time
from importlib.resources import files
from typing import Any
from urllib.parse import urlsplit

from tow.paths import runtime_dir
from tow.platform import current, plain_kind
from tow.platform._local_service_worker import read_manifest, safe_environment
from tow.protection.spec import service_url
from tow.store import atomic_write_bytes


def managed(provider: dict[str, Any]) -> bool:
    if provider.get("kind") != "flaresolverr" or current().name != "windows":
        return False
    folder = runtime_dir() / "services" / "flaresolverr"
    if plain_kind(folder, missing_ok=True) is None:
        return False
    if plain_kind(folder.parent, missing_ok=True) not in {None, "dir"} or plain_kind(folder.parent.parent) != "dir":
        raise ValueError("redirected local service installation")
    if plain_kind(folder / "manifest.json", missing_ok=True) is None:
        return False
    return bool(read_manifest(folder)["url"] == service_url(provider["url"]))


def listening(port: int, timeout: float) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=timeout):
            return True
    except OSError:
        return False


def ensure(provider: dict[str, Any], deadline: float) -> bool:
    """Only called by an applying provider request, never a settings view or diagnostics."""
    if provider.get("enabled") is False:
        return False
    try:
        installed = managed(provider)
    except OSError, ValueError:
        endpoint = urlsplit(service_url(provider["url"]))
        left = deadline - time.monotonic()
        if (
            endpoint.hostname == "127.0.0.1"
            and endpoint.port
            and left > 0
            and listening(endpoint.port, min(0.25, left))
        ):
            return False  # a damaged local installation cannot block an independently operated API
        raise
    if not installed:
        return False
    port = urlsplit(service_url(provider["url"])).port
    assert port is not None
    left = deadline - time.monotonic()
    if left <= 0:
        raise TimeoutError("local module launch budget exhausted")
    if listening(port, min(0.25, left)):
        return True  # a manually operated listener is reused, never adopted or stopped
    folder = runtime_dir() / "services" / "flaresolverr"
    worker = folder / "worker.py"
    source = files("tow.platform").joinpath("_local_service_worker.py").read_bytes()
    atomic_write_bytes(worker, source)
    env = safe_environment(folder)
    if time.monotonic() >= deadline:
        raise TimeoutError("local module preparation exceeded launch budget")
    current().spawn_detached(
        [str(getattr(sys, "_base_executable", sys.executable)), "-I", str(worker)], cwd=folder, env=env, hidden=True
    )
    while time.monotonic() < deadline:
        if listening(port, min(0.25, deadline - time.monotonic())):
            return True
        time.sleep(min(0.05, max(0, deadline - time.monotonic())))
    raise TimeoutError("local module did not become ready")
