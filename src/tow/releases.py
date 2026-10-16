"""Read-only stable-release discovery. No credentials, downloads or automatic installation.

The process cache is shared by all browsers. Its lock coalesces simultaneous checks;
failures back off too, without turning an unknown version into 'up to date'. Every check
publishes its answer as one immutable record: ``cached()`` hands out the last one without the
lock (a page's poll never waits for GitHub), and ``ReleaseProducer`` - started by the web
server - keeps asking when a check is due, whether or not a page asks.
"""

from __future__ import annotations

import json
import logging
import re
import threading
import time
from collections.abc import Callable, Mapping
from types import MappingProxyType
from typing import Any

import httpx

from tow import __version__
from tow.net_guard import PublicOnlyTransport

LATEST_URL = "https://api.github.com/repos/d0j/tow/releases/latest"
RELEASES_URL = "https://github.com/d0j/tow/releases"
CHECK_INTERVAL = 12 * 60 * 60
FAILURE_INTERVAL = 60 * 60
MANUAL_INTERVAL = 60
MAX_BYTES = 256 * 1024
_VERSION = re.compile(r"v?(0|[1-9][0-9]{0,8})\.(0|[1-9][0-9]{0,8})\.(0|[1-9][0-9]{0,8})")


def _version(value: object) -> tuple[int, ...] | None:
    if not isinstance(value, str) or not _VERSION.fullmatch(value):
        return None
    return tuple(int(part) for part in value.removeprefix("v").split("."))


def version_parts(value: object) -> tuple[int, ...] | None:
    return _version(value)


def _fetch_release(url: str, *, expected: str | None = None) -> str:
    deadline = time.monotonic() + 8
    with (
        httpx.Client(
            transport=PublicOnlyTransport(),
            trust_env=False,
            follow_redirects=False,
            timeout=4,
            headers={"Accept": "application/vnd.github+json", "User-Agent": "TOW-release-check"},
        ) as client,
        client.stream("GET", url) as response,
    ):
        response.raise_for_status()
        body = bytearray()
        for chunk in response.iter_bytes():
            if time.monotonic() > deadline or len(body) + len(chunk) > MAX_BYTES:
                raise ValueError("release response limit")
            body.extend(chunk)
    release = json.loads(body)
    if (
        not isinstance(release, dict)
        or release.get("draft") is not False
        or release.get("prerelease") is not False
        or not isinstance(release.get("tag_name"), str)
        or _version(release["tag_name"]) is None
    ):
        raise ValueError("not a stable release")
    version = str(release["tag_name"]).removeprefix("v")
    if expected is not None and version != expected:
        raise ValueError("unexpected release")
    return version


def _fetch_latest() -> str:
    return _fetch_release(LATEST_URL)


def published_version(version: str) -> str:
    if _version(version) is None:
        raise ValueError("invalid version")
    version = version.removeprefix("v")
    return _fetch_release(f"https://api.github.com/repos/d0j/tow/releases/tags/v{version}", expected=version)


class ReleaseChecker:
    def __init__(self) -> None:
        self._lock = threading.Lock()
        self._attempt: float | None = None
        self._next = 0.0
        self._latest = ""
        self._checked_at: int | None = None
        self._ok = False
        # The last answer, published whole: replaced (never changed) after each check, so a
        # reader without the lock sees one answer or the next, never half of one.
        self._published: Mapping[str, Any] = MappingProxyType(self._answer(enabled=True))

    def _answer(self, *, enabled: bool) -> dict[str, Any]:
        current, latest_version = _version(__version__), _version(self._latest)
        available = current is not None and latest_version is not None and latest_version > current
        return {
            "current": __version__,
            "latest": self._latest,
            "available": available,
            "comparable": current is not None,
            "checks_enabled": enabled,
            "ok": self._ok,
            "checked_at": self._checked_at,
            "url": f"{RELEASES_URL}/tag/v{self._latest}" if self._latest else RELEASES_URL,
        }

    def cached(self, *, enabled: bool = True) -> Mapping[str, Any]:
        """The last published answer, read-only, without waiting for a check in progress and
        without asking GitHub. ``enabled``: the owner's setting now (said as is)."""
        published = self._published
        if published["checks_enabled"] == enabled:
            return published
        return MappingProxyType({**published, "checks_enabled": enabled})

    def check(self, *, force: bool = False, enabled: bool = True) -> dict[str, Any]:
        with self._lock:
            now = time.monotonic()
            due = self._attempt is None or now >= self._next
            if force and self._attempt is not None and now - self._attempt >= MANUAL_INTERVAL:
                due = True
            if due and (enabled or force):
                self._attempt = now
                try:
                    latest = _fetch_latest()
                    if _version(latest) is None:
                        raise ValueError("invalid version")
                except httpx.HTTPError, OSError, ValueError, RecursionError:
                    self._ok = False
                    self._next = time.monotonic() + FAILURE_INTERVAL
                else:
                    self._latest = latest
                    self._checked_at = int(time.time())
                    self._ok = True
                    self._next = time.monotonic() + CHECK_INTERVAL
            answer = self._answer(enabled=enabled)
            self._published = MappingProxyType(dict(answer))
            return answer


checker = ReleaseChecker()


def _checks_enabled() -> bool:
    from tow.config import as_bool, load_config

    return as_bool(load_config().get("check_updates", True))


def release_status(*, force: bool = False) -> dict[str, Any]:
    return checker.check(force=force, enabled=_checks_enabled())


def cached(*, enabled: bool = True) -> Mapping[str, Any]:
    """``checker.cached``: the last published answer, never a network request or a wait."""
    return checker.cached(enabled=enabled)


# How often the producer asks whether a check is due (the check itself keeps its own pace:
# CHECK_INTERVAL after an answer, FAILURE_INTERVAL after a failure), and its first wait.
PRODUCER_INTERVAL = 10 * 60
PRODUCER_FIRST_DELAY = 60


class ReleaseProducer:
    """Keeps release discovery alive in the web server: every ``interval`` seconds it runs
    ``check`` (``release_status``: a GitHub request only when one is due), in its own thread, so
    pages learn of a release from ``cached()`` without asking GitHub themselves."""

    def __init__(
        self,
        check: Callable[[], object] | None = None,
        *,
        interval: float = PRODUCER_INTERVAL,
        first_delay: float = PRODUCER_FIRST_DELAY,
    ) -> None:
        self._check = check or release_status
        self._interval = interval
        self._first_delay = first_delay
        self._stop = threading.Event()
        self._thread: threading.Thread | None = None

    def start(self) -> None:
        if self._thread is not None and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="tow-release-check", daemon=True)
        self._thread.start()

    def stop(self, timeout: float = 5.0) -> None:
        self._stop.set()
        thread, self._thread = self._thread, None
        if thread is not None and thread is not threading.current_thread():
            thread.join(timeout)

    def _run(self) -> None:
        delay = self._first_delay
        while not self._stop.wait(delay):
            delay = self._interval
            try:
                self._check()
            except Exception as exc:  # noqa: BLE001 - discovery goes on at the next turn; the reason is logged
                logging.getLogger("tow.releases").warning("release check failed: %s", type(exc).__name__)
