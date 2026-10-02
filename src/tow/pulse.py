"""Why TOW was silent: facts the watchdog tells the owner.

Everything here reads facts the operating system already keeps (through ``tow.platform``) -
nothing extra runs in the background:

- boot time, the time spent asleep since boot, and when the owner signed in;
- why the PC went down (on Windows: the System event log - 1074 who restarted or powered off
  and why, 41 / 6008 it went down without a clean shutdown; on Linux: when the boot ended);
- the last error the web server wrote before it died.

Every probe fails soft (None / "" / []): a missing fact makes the message less specific,
never wrong. On a development checkout the watchdog uses ``Probes.inert()``.
"""

from __future__ import annotations

import re
import socket
from collections.abc import Callable
from dataclasses import dataclass, field
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from tow import i18n, platform
from tow.i18n import t
from tow.platform.windows import FILETIME_UNIX_EPOCH, parse_events

__all__ = ["FILETIME_UNIX_EPOCH", "parse_events"]

_UPDATE_MARKERS = ("trustedinstaller", "mousocoreworker", "usoclient", "wuauclt", "update", "upgrade")


def clock(ts: float, lang: str | None = None) -> str:
    """A short local time the way the language writes it (``_meta.datetime_short``)."""
    return i18n.format_datetime(datetime.fromtimestamp(ts, UTC).astimezone(), lang, short=True)


def duration(seconds: float, lang: str | None = None) -> str:
    lang = lang or i18n.message_language()
    minutes = max(1, round(seconds / 60))
    hours, minutes = divmod(minutes, 60)
    days, hours = divmod(hours, 24)
    parts = [t("pulse.duration.days", lang, count=days)] if days else []
    if hours:
        parts.append(t("pulse.duration.hours", lang, count=hours))
    if minutes and not days:
        parts.append(t("pulse.duration.minutes", lang, count=minutes))
    return " ".join(parts) or t("pulse.duration.minutes", lang, count=1)


@dataclass(frozen=True)
class Machine:
    boot_ts: float
    asleep_sec: float
    logon_ts: float | None = None


def machine(now: float | None = None) -> Machine | None:
    """Boot time, seconds asleep since boot and the sign-in time of this session."""
    backend = platform.current()
    boot = backend.boot_time(now)
    asleep = backend.asleep_seconds()
    if boot is None or asleep is None:
        return None
    return Machine(boot_ts=boot, asleep_sec=asleep, logon_ts=backend.logon_time())


def shutdown_events(since_ts: float, until_ts: float) -> list[dict[str, Any]]:
    """Shutdown records between two moments, oldest first."""
    return platform.current().shutdown_reasons(since_ts, until_ts)


def _by_update(event: dict[str, Any]) -> bool:
    text = f"{event.get('process')} {event.get('reason')}".lower()
    return event.get("id") == 1074 and any(marker in text for marker in _UPDATE_MARKERS)


_VIA = {
    "startmenuexperiencehost.exe": "pulse.via.start_menu",
    "explorer.exe": "pulse.via.start_menu",
    "shutdown.exe": "pulse.via.shutdown_command",
    "winlogon.exe": "pulse.via.logon_screen",
}


def describe_shutdown(events: list[dict[str, Any]], lang: str | None = None) -> str:
    """The last shutdown in plain words, or "" when Windows recorded none."""
    if not events:
        return ""
    lang = lang or i18n.message_language()
    last = events[-1]
    if last["id"] == "stopped":  # Linux: only when the boot ended is known, not how
        return t("pulse.shutdown.stopped", lang, at=clock(last["ts"]))
    if last["id"] in (41, 6008):
        return t("pulse.shutdown.crash", lang, at=clock(last["ts"]))
    if _by_update(last):
        return t("pulse.shutdown.update", lang, at=clock(last["ts"]))
    action = "restarted" if "restart" in str(last["action"]).lower() else "powered_off"
    process = str(last["process"]).split(" (")[0]
    name = Path(process.replace("\\", "/")).name if process else ""
    via_key = _VIA.get(name.lower())
    via = t(via_key, lang) if via_key else f"({name})" if name else ""
    text = t(f"pulse.shutdown.{action}", lang, via=via, at=clock(last["ts"])).replace("  ", " ")
    if any(_by_update(event) for event in events[:-1]):
        text = t("pulse.shutdown.after_updates", lang, text=text)
    return text


def outage_cause(
    *,
    since_ts: float,
    now_ts: float,
    now_machine: Machine | None,
    asleep_before: float | None,
    events: Callable[[float, float], list[dict[str, Any]]],
    lang: str | None = None,
) -> str:
    """Why nothing ran between ``since_ts`` (the last watchdog pass) and now."""
    lang = lang or i18n.message_language()
    gap = now_ts - since_ts
    if now_machine is None:
        return t("pulse.outage.unknown", lang)
    if now_machine.boot_ts > since_ts:
        what = describe_shutdown(events(since_ts, now_machine.boot_ts), lang) or t(
            "pulse.outage.off_or_restarted", lang
        )
        text = t("pulse.outage.back_on", lang, what=what, at=clock(now_machine.boot_ts))
        logon = now_machine.logon_ts
        if logon and logon - now_machine.boot_ts > 5 * 60:
            text += t("pulse.outage.logon_later", lang, at=clock(logon))
        return text
    if asleep_before is not None:
        slept = now_machine.asleep_sec - asleep_before
        if slept >= max(0.5 * gap, 10 * 60):
            return t("pulse.outage.asleep", lang, duration=duration(slept, lang))
    if now_machine.logon_ts and now_machine.logon_ts > since_ts:
        return t("pulse.outage.logged_off", lang, at=clock(now_machine.logon_ts))
    return t("pulse.outage.scheduler_idle", lang)


# --- the service ----------------------------------------------------------------------------


def port_listening(port: int) -> bool:
    try:
        with socket.create_connection(("127.0.0.1", port), timeout=2):
            return True
    except OSError:
        return False


# A log record at ERROR/CRITICAL level, or the last line of a Python traceback.
_ERROR_LINE = re.compile(r"\s(ERROR|CRITICAL)\s|^[\w.]*(Error|Exception|Exit|Interrupt)\b(:|$)")


def last_crash_line(log_dir: Path, since_ts: float) -> str:
    """The last error the web server wrote after ``since_ts`` (scrubbed, short), or ""."""
    from tow.log import scrub_text

    for name in ("serve-stderr.log", "serve.log"):
        path = log_dir / name
        try:
            if path.stat().st_mtime < since_ts:
                continue
            with path.open("rb") as handle:
                handle.seek(max(0, path.stat().st_size - 20000))
                lines = handle.read().decode("utf-8", "replace").splitlines()
        except OSError:
            continue
        for line in reversed(lines):
            text = line.strip()
            if text and _ERROR_LINE.search(text):
                return scrub_text(text)[-200:]
    return ""


@dataclass
class Probes:
    """What the watchdog asks the system; tests and dev checkouts pass their own."""

    machine: Callable[[], Machine | None] = machine
    shutdown_events: Callable[[float, float], list[dict[str, Any]]] = shutdown_events
    port_listening: Callable[[int], bool] = port_listening
    crash_line: Callable[[float], str] = field(default=lambda _since: "")

    @classmethod
    def inert(cls) -> Probes:
        return cls(
            machine=lambda: None,
            shutdown_events=lambda _a, _b: [],
            port_listening=lambda _p: False,
        )
