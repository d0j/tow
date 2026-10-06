from __future__ import annotations

import asyncio
import contextlib
import json
import os
import shutil
import socket
import subprocess
import threading
import time
import urllib.error
import urllib.request
import uuid
from collections.abc import Callable
from pathlib import Path
from typing import Any, cast
from urllib.parse import urlparse

from tow import platform
from tow.i18n import t
from tow.log import log_event
from tow.mirrors import origin_key
from tow.paths import data_dir


class BrowserAuthError(RuntimeError):
    """Raised when the user-facing browser session cannot be completed."""


COMPLETED_SESSION_TTL_SEC = 60 * 60
MAX_COMPLETED_SESSIONS = 128
_TERMINAL_STATUSES = frozenset({"succeeded", "failed"})


class BrowserAuthManager:
    """Run one explicit, dedicated Chromium session for tracker auth.

    The manager never opens or inspects the user's normal browser profile and
    never automates a CAPTCHA. The only success condition is a download link
    rendered by the tracker page after the user has completed its own login.
    """

    def __init__(
        self,
        *,
        completed_ttl_sec: int = COMPLETED_SESSION_TTL_SEC,
        max_completed_sessions: int = MAX_COMPLETED_SESSIONS,
    ) -> None:
        self._lock = threading.RLock()
        self._sessions: dict[str, dict[str, Any]] = {}
        self._active_by_topic: dict[str, str] = {}
        self._completed_ttl_sec = max(0, int(completed_ttl_sec))
        self._max_completed_sessions = max(0, int(max_completed_sessions))
        self._swept = False

    def _sweep_stale_profiles_unlocked(self) -> None:
        """Delete login profiles a crashed or killed TOW left behind (they hold cookies)."""
        if self._swept:
            return
        self._swept = True
        root = data_dir() / "browser-auth"
        active = set(self._sessions)
        with contextlib.suppress(OSError):
            for leftover in root.iterdir():
                if leftover.is_dir() and leftover.name not in active:
                    _remove_profile(leftover / "profile")

    def _cleanup_unlocked(self, *, now: float | None = None) -> None:
        now = time.time() if now is None else now
        active_ids = set(self._active_by_topic.values())
        completed: list[tuple[float, str]] = []
        for operation_id, record in list(self._sessions.items()):
            if operation_id in active_ids or record.get("status") not in _TERMINAL_STATUSES:
                continue
            finished_at = float(record.get("finished_at") or record.get("started_at") or 0.0)
            if now - finished_at > self._completed_ttl_sec:
                self._sessions.pop(operation_id, None)
                continue
            completed.append((finished_at, operation_id))
        completed.sort(reverse=True)
        for _finished_at, operation_id in completed[self._max_completed_sessions :]:
            self._sessions.pop(operation_id, None)

    def start(
        self,
        *,
        topic_id: str,
        tracker_name: str,
        topic_url: str,
        start_url: str,
        on_success: Callable[[dict[str, str], str], dict[str, Any] | None],
        timeout_sec: int = 300,
    ) -> dict[str, Any]:
        with self._lock:
            self._cleanup_unlocked()
            self._sweep_stale_profiles_unlocked()
            active_id = self._active_by_topic.get(str(topic_id))
            if active_id:
                current = self._sessions.get(active_id)
                if current and current.get("status") in {"starting", "waiting"}:
                    return self._public(current)
            operation_id = f"browser-auth-{uuid.uuid4().hex[:12]}"
            profile = data_dir() / "browser-auth" / operation_id / "profile"
            record = {
                "operation_id": operation_id,
                "topic_id": str(topic_id),
                "tracker": str(tracker_name),
                "status": "starting",
                "message": t("browser_auth.starting"),
                "started_at": time.time(),
                "profile": str(profile),
            }
            self._sessions[operation_id] = record
            self._active_by_topic[str(topic_id)] = operation_id
        thread = threading.Thread(
            target=self._run,
            args=(record, topic_url, start_url, on_success, max(30, int(timeout_sec))),
            name=f"tow-browser-auth-{operation_id}",
            daemon=True,
        )
        thread.start()
        return self._public(record)

    def status(self, topic_id: str, operation_id: str | None = None) -> dict[str, Any]:
        with self._lock:
            self._cleanup_unlocked()
            wanted = operation_id or self._active_by_topic.get(str(topic_id))
            record = self._sessions.get(wanted or "")
            if not record or str(record.get("topic_id")) != str(topic_id):
                return {"status": "idle", "topic_id": str(topic_id)}
            return self._public(record)

    def _public(self, record: dict[str, Any]) -> dict[str, Any]:
        return {
            "operation_id": str(record.get("operation_id") or ""),
            "topic_id": str(record.get("topic_id") or ""),
            "tracker": str(record.get("tracker") or ""),
            "status": str(record.get("status") or "unknown"),
            "message": str(record.get("message") or ""),
        }

    def _set(self, record: dict[str, Any], *, status: str, message: str) -> None:
        with self._lock:
            record["status"] = status
            record["message"] = message
            record["finished_at"] = time.time()

    def _phase(self, record: dict[str, Any], phase: str) -> None:
        with self._lock:
            record["phase"] = phase

    def _log_outcome(self, record: dict[str, Any], *, error: BaseException | None = None) -> None:
        """Every outcome is in the event log: failures were invisible before (N5)."""
        fields = {
            "operation_id": record.get("operation_id"),
            "topic_id": record.get("topic_id"),
            "topic": record.get("topic_id"),
            "tracker": record.get("tracker"),
            "phase": record.get("phase"),
            "status": record.get("status"),
            "how": "manual",
        }
        if error is not None:
            fields["error_type"] = type(error).__name__
            fields["error"] = str(record.get("message") or "")
        with contextlib.suppress(Exception):
            log_event("browser_auth_" + ("succeeded" if record.get("status") == "succeeded" else "failed"), **fields)

    def _run(
        self,
        record: dict[str, Any],
        topic_url: str,
        start_url: str,
        on_success: Callable[[dict[str, str], str], dict[str, Any] | None],
        timeout_sec: int,
    ) -> None:
        process: subprocess.Popen[bytes] | None = None
        profile = Path(str(record["profile"]))
        failure: BaseException | None = None
        try:
            self._phase(record, "launch")
            executable = find_browser_executable()
            backend = platform.current()
            # Linux and macOS: HOME, the XDG folders, the keyring and the Keychain stay inside
            # the session folder too (removed with it); a snap browser cannot do that.
            launch = backend.browser_launch(executable, profile.parent / "home")
            if launch.get("confined"):
                raise BrowserAuthError(t("browser_auth.snap_browser"))
            port = _free_port()
            profile.mkdir(parents=True, exist_ok=False)
            if launch.get("env") is not None:
                (profile.parent / "home").mkdir(exist_ok=True)
            args = [
                executable,
                f"--remote-debugging-port={port}",
                f"--user-data-dir={profile}",
                "--no-first-run",
                "--no-default-browser-check",
                "--disable-sync",
                "--disable-extensions",
                "--new-window",
                "--start-maximized",
                *launch.get("args", []),
                "--",  # the end of the switches: the address is never read as one
                start_url,
            ]
            # Its own process group / session: stopping it reaches every child it starts.
            process = subprocess.Popen(
                args,
                cwd=str(data_dir()),
                env=launch.get("env"),
                stdin=subprocess.DEVNULL,
                stdout=subprocess.DEVNULL,
                stderr=subprocess.DEVNULL,
                **backend.popen_options(new_group=True, hidden=False),
            )
            _show_browser_window(process.pid)
            self._phase(record, "wait_login")
            self._set(record, status="waiting", message=_sign_in_hint(str(record.get("tracker") or "")))
            cookies, user_agent = asyncio.run(
                _wait_for_authenticated_topic(
                    port=port,
                    topic_url=topic_url,
                    timeout_sec=timeout_sec,
                    process=process,
                )
            )
            self._phase(record, "confirm")
            result = on_success(cookies, user_agent) or {}
            if result.get("ok", True):
                self._set(
                    record, status="succeeded", message=str(result.get("message") or t("browser_auth.ok_checked"))
                )
            else:
                self._set(
                    record,
                    status="failed",
                    message=str(result.get("message") or t("browser_auth.ok_check_failed")),
                )
        except BrowserAuthError as exc:
            failure = exc
            self._set(record, status="failed", message=str(exc))
        except Exception as exc:  # noqa: BLE001 - the login thread's boundary: the failure is recorded and logged
            failure = exc
            self._set(record, status="failed", message=t("browser_auth.technical_error"))
        finally:
            if process is not None:
                _terminate_process(process)
            _remove_profile(profile)
            self._log_outcome(record, error=failure)
            with self._lock:
                topic_id = str(record.get("topic_id") or "")
                if self._active_by_topic.get(topic_id) == record.get("operation_id"):
                    self._active_by_topic.pop(topic_id, None)
                self._cleanup_unlocked()


def _sign_in_hint(tracker_name: str) -> str:
    """What the owner is asked to do in the window: the site's own words (its preset), else
    the plain "sign in to <site>"."""
    from tow.trackers import presets

    key = presets.browser_login_hint(tracker_name)
    return t(key) if key else t("browser_auth.sign_in", site=tracker_name)


def find_browser_executable() -> str:
    configured = os.environ.get("TOW_BROWSER_EXECUTABLE", "").strip()
    if configured:
        path = Path(configured)
        if path.is_file():
            return str(path)
        raise BrowserAuthError(t("browser_auth.exe_missing"))
    found = platform.current().browser_executables()  # Edge, Chrome or Chromium of this system
    if found:
        return found[0]
    raise BrowserAuthError(t("browser_auth.no_browser"))


def _show_browser_window(process_id: int) -> None:
    """Show the new browser window in front (Windows starts it behind others; elsewhere a no-op)."""
    platform.current().bring_to_front(process_id)


def _free_port() -> int:
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as sock:
        sock.bind(("127.0.0.1", 0))
        return int(sock.getsockname()[1])


def _json_get(port: int, path: str) -> Any:
    url = f"http://127.0.0.1:{port}{path}"
    try:
        with urllib.request.urlopen(url, timeout=2) as response:
            return json.loads(response.read().decode("utf-8"))
    except (OSError, ValueError, urllib.error.URLError, urllib.error.HTTPError) as exc:
        raise BrowserAuthError(t("browser_auth.cdp_unavailable")) from exc


async def _wait_target(
    port: int,
    process: subprocess.Popen[bytes],
    timeout_sec: int,
    allowed_origins: set[str],
) -> str:
    deadline = time.monotonic() + min(30, timeout_sec)
    while time.monotonic() < deadline:
        if process.poll() is not None:
            raise BrowserAuthError(t("browser_auth.closed_before_login"))
        try:
            targets = await asyncio.to_thread(_json_get, port, "/json/list")
        except BrowserAuthError:
            await asyncio.sleep(0.25)
            continue
        if isinstance(targets, list):
            for target in targets:
                if (
                    isinstance(target, dict)
                    and target.get("type") == "page"
                    and target.get("webSocketDebuggerUrl")
                    and origin_key(str(target.get("url") or "")) in allowed_origins
                ):
                    return str(target["webSocketDebuggerUrl"])
        await asyncio.sleep(0.25)
    raise BrowserAuthError(t("browser_auth.cannot_connect"))


async def _wait_for_authenticated_topic(
    *,
    port: int,
    topic_url: str,
    timeout_sec: int,
    process: subprocess.Popen[bytes],
) -> tuple[dict[str, str], str]:
    try:
        from websockets.asyncio.client import connect
    except ImportError as exc:
        raise BrowserAuthError(t("browser_auth.needs_websockets")) from exc

    parsed_topic = urlparse(topic_url)
    expected_origin = origin_key(topic_url)
    expected_host = (parsed_topic.hostname or "").lower()
    if not expected_origin or not expected_host:
        raise BrowserAuthError(t("browser_auth.bad_url"))
    ws_url = await _wait_target(port, process, timeout_sec, {expected_origin})
    async with connect(ws_url, open_timeout=5, close_timeout=5, max_size=4 * 1024 * 1024) as socket_ws:
        request_id = 0

        async def call(method: str, params: dict[str, Any] | None = None) -> dict[str, Any]:
            nonlocal request_id
            request_id += 1
            ident = request_id
            await socket_ws.send(json.dumps({"id": ident, "method": method, "params": params or {}}))
            while True:
                raw = await asyncio.wait_for(socket_ws.recv(), timeout=10)
                message = json.loads(raw)
                if message.get("id") != ident:
                    continue
                if "error" in message:
                    raise BrowserAuthError(t("browser_auth.cdp_refused"))
                return message.get("result") or {}

        await call("Network.enable")
        await call("Page.bringToFront")
        version = await call("Browser.getVersion")
        user_agent = str(version.get("userAgent") or "")
        deadline = time.monotonic() + timeout_sec
        transient_errors = 0
        while time.monotonic() < deadline:
            if process.poll() is not None:
                raise BrowserAuthError(t("browser_auth.closed_before_confirm"))
            try:
                result = await _evaluate_page(call)
            except (BrowserAuthError, TimeoutError, ValueError) as exc:
                # The login page navigates while the owner signs in: "context destroyed" and
                # slow replies are normal then. Only a long run of them ends the session (N6).
                transient_errors += 1
                if transient_errors >= MAX_TRANSIENT_CDP_ERRORS:
                    raise BrowserAuthError(t("browser_auth.not_responding")) from exc
                await asyncio.sleep(1)
                continue
            transient_errors = 0
            value = (result.get("result") or {}).get("value") or {}
            href = str(value.get("href") or "")
            host = str(value.get("host") or "").lower()
            if host == expected_host and expected_origin == origin_key(href) and bool(value.get("hasDownload")):
                cookie_result = await call("Network.getAllCookies")
                cookies: dict[str, str] = {}
                for item in cookie_result.get("cookies") or []:
                    if not isinstance(item, dict):
                        continue
                    domain = str(item.get("domain") or "").lstrip(".").lower()
                    name = str(item.get("name") or "")
                    value = str(item.get("value") or "")
                    if not name or not value:
                        continue
                    if domain == expected_host or expected_host.endswith("." + domain):
                        cookies[name] = value
                if cookies:
                    return cookies, user_agent
            await asyncio.sleep(2)
    raise BrowserAuthError(t("browser_auth.not_confirmed"))


MAX_TRANSIENT_CDP_ERRORS = 15
_PAGE_PROBE = """
    (() => ({
      href: location.href,
      host: location.hostname,
      ready: document.readyState,
      hasDownload: Boolean(document.querySelector('a[href*="download.php"]'))
    }))()
"""


async def _evaluate_page(call: Callable[..., Any]) -> dict[str, Any]:
    return cast(dict[str, Any], await call("Runtime.evaluate", {"expression": _PAGE_PROBE, "returnByValue": True}))


def _terminate_process(process: subprocess.Popen[bytes]) -> None:
    """Stop the browser and all its child processes (N7: Edge's children kept the
    profile - with the tracker cookies - locked, so it stayed on disk). The whole tree on
    Windows, the process group on Linux and macOS; then the process itself if it survived."""
    platform.current().terminate(process.pid, timeout=5)
    if process.poll() is not None:
        return
    try:
        process.terminate()
        process.wait(timeout=5)
    except OSError, subprocess.TimeoutExpired:
        with contextlib.suppress(OSError):
            process.kill()


def _remove_profile(profile: Path, *, attempts: int = 10) -> bool:
    """Delete the session folder; files a dying browser still holds get a few retries."""
    root = profile.parent
    for _ in range(attempts):
        shutil.rmtree(root, ignore_errors=True)
        if not root.exists():
            return True
        time.sleep(0.5)
    return False


browser_auth = BrowserAuthManager()

__all__ = ["BrowserAuthError", "BrowserAuthManager", "browser_auth", "find_browser_executable"]
