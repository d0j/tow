"""Watchdog: is TOW up, are the scheduled checks running, are the night copies good? (C1)

``tow run`` (tow.supervisor) makes this pass in-process every 10 minutes; ``tow watchdog`` runs
one by hand (a diagnostic). It only says what it sees - the supervisor itself restarts the web
server and runs the checks:

- TOW not answering -> why: closed, crashed (the last error it wrote), or hung (port held, no
  answer);
- scheduled checks late or failing every run -> since when, and the last check error;
- a long silence -> on the next pass says what happened: switched off / restarted (by whom,
  e.g. Windows Update), crashed, asleep, logged off - from facts the OS keeps (tow.pulse);
- the night copies, the heartbeat ping and the messages that could not be delivered yet.

Messages go to the owner's messengers only when something changes - never on every run.
"""

from __future__ import annotations

import time
from collections.abc import Callable
from dataclasses import dataclass
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from tow import i18n
from tow.config import interval_sec_of, load_config, port_of
from tow.diagnostic_json import check_epochs, check_types, encode_object, epoch, read_object
from tow.i18n import t
from tow.log import log_event
from tow.paths import data_dir
from tow.pulse import Machine, Probes, clock, duration, last_crash_line, outage_cause
from tow.store import atomic_write_text

STATE_NAME = "watchdog.json"
# Checks are late when the last scheduled one is older than two intervals plus this slack.
STALE_SLACK_SEC = 10 * 60
# The watchdog runs every 10 minutes; a longer silence means TOW (or the PC) was not running.
OUTAGE_GAP_SEC = 40 * 60
DEPLOY_GRACE_SEC = 30 * 60
# A busy TOW may answer late: a held port with no answer is re-checked once after this.
HUNG_CONFIRM_SEC = 15.0
# Night copies run daily; a day and a half without a good one is worth a message.
BACKUP_STALE_SEC = 36 * 3600
# Scheduled checks that start but fail this many times in a row are reported like late ones.
CHECK_FAILURES_ALERT = 3


def _state_path() -> Path:
    return data_dir() / STATE_NAME


def _load_state() -> dict[str, Any]:
    try:
        value = read_object(_state_path())
        check_epochs(value, ("at", "checks_watch_since", "backup_watch_since", "down_since", "boot_ts", "asleep_sec"))
        check_types(
            value,
            dict.fromkeys(
                (
                    "service",
                    "checks",
                    "backup",
                    "backup_cleanup",
                    "restore_point_cleanup",
                    "restore_point_cleanup_monitoring",
                    "monitoring",
                ),
                bool,
            ),
        )
        check_types(value, {"last_problem": dict, "restore_point_cleanup_location": str})
        problem = value.get("last_problem")
        if isinstance(problem, dict):
            check_epochs(problem, ("at",))
            check_types(problem, {"text": str})
    except FileNotFoundError:
        return {}
    except OSError, UnicodeError, ValueError, TypeError, RecursionError:
        return {"read_error": True}
    return value


def _save_state(value: dict[str, Any]) -> None:
    atomic_write_text(_state_path(), encode_object(value))


def healthy(port: int, *, timeout: float = 3.0) -> bool:
    import httpx

    try:
        # trust_env=False: a system proxy (HTTP_PROXY without 127.0.0.1 in NO_PROXY) must never
        # stand between the watchdog and this machine's TOW - it made a healthy TOW look hung.
        response = httpx.get(f"http://127.0.0.1:{port}/healthz", timeout=timeout, trust_env=False)
        value = response.json() if response.status_code == 200 else None
        return isinstance(value, dict) and value.get("ok") is True
    except httpx.HTTPError, ValueError, RecursionError:
        return False


# An update stops the service on purpose and says so at the install root: update-state.json
# (scripts/update.py) or deploy-state.json (deploy.ps1 before 1.18).
UPDATE_MARKERS = ("update-state.json", "deploy-state.json")


def _update_in_progress(marker: Path) -> bool:
    try:
        state = read_object(marker, bom=True)
        started = datetime.fromisoformat(str(state.get("started_at")))
        age = (datetime.now(UTC) - started.astimezone(UTC)).total_seconds()
    except OSError, UnicodeError, ValueError, TypeError, AttributeError, RecursionError, OverflowError:
        return False
    return state.get("status") == "in_progress" and 0 <= age < DEPLOY_GRACE_SEC


def _deploy_running() -> bool:
    """An update (or an older deploy) is replacing TOW right now, for at most 30 minutes."""
    from tow.paths import root

    install = root()  # where update.py writes them, also when TOW_ROOT names the install
    return any(_update_in_progress(install / name) for name in UPDATE_MARKERS)


def _last_scheduled_check(state: dict[str, Any]) -> int:
    """When the last scheduled check really ran. A blocked attempt moves ``auto_at_ts`` (the
    header countdown) but not ``auto_ok_at_ts``: a check failing every run is not "on time".
    Never ``at_ts``: manual checks and progress passes move it, scheduled checks or not."""
    health = state.get("health")
    if not isinstance(health, dict):
        return 0
    value = health.get("auto_ok_at_ts") if "auto_ok_at_ts" in health else health.get("auto_at_ts")
    return int(epoch(value) or 0)


def _scheduled_failures(state: dict[str, Any]) -> int:
    health = state.get("health")
    try:
        return int(health.get("check_failures") or 0) if isinstance(health, dict) else 0
    except TypeError, ValueError, OverflowError:
        return 0


def _wake_ts(p: _Pass, machine: Machine | None) -> float | None:
    """The first pass after a long silence (PC asleep or off): when the machine came back.

    The scheduled check runs a few minutes after a wake; without this the pass right after
    every sleep called the checks late and sent the heartbeat to /fail.
    """
    last_pass = p.previous.get("at")
    if not isinstance(last_pass, (int, float)) or p.now() - last_pass <= OUTAGE_GAP_SEC:
        return None
    if machine is not None and machine.boot_ts > last_pass:
        return max(machine.boot_ts, machine.logon_ts or 0.0)
    asleep = p.previous.get("asleep_sec")
    if machine is not None and isinstance(asleep, (int, float)) and machine.asleep_sec - asleep >= 10 * 60:
        return min(p.now(), float(last_pass) + machine.asleep_sec - asleep)
    return None  # no sign of a sleep or a restart: the silence itself is the problem


def ping_heartbeat(url: str, *, ok: bool) -> bool:
    """External pulse: GET the URL (healthchecks.io style), or URL/fail on a problem.

    The service on the other side alerts the owner when the pings STOP - the case
    this machine cannot report itself (switched off, hung, no network).
    """
    import httpx

    from tow.http import client as http_client

    target = url.rstrip("/") + ("" if ok else "/fail")
    try:
        with http_client(follow_redirects=False, public_only=True) as client, client.stream("GET", target) as response:
            return response.status_code < 400
    except httpx.HTTPError:
        return False


def _default_probes() -> Probes:
    """``tow watchdog`` by hand: reads only (no process, no port, no event log), and the last
    error the web server wrote."""
    probes = Probes.inert()
    probes.crash_line = lambda since: last_crash_line(data_dir() / "logs", since)
    return probes


@dataclass
class _Pass:
    """What one watchdog pass learned; turned into alerts and the saved state."""

    now: Callable[[], float]
    previous: dict[str, Any]
    report: dict[str, Any]
    lang: str = i18n.DEFAULT  # messages go out in the owner's language
    cause: str = ""
    crash: str = ""
    hung: bool = False
    checks_why: str = ""
    backup_why: str = ""


def _service(
    p: _Pass,
    port: int,
    probes: Probes,
    since: float,
    *,
    is_healthy: Callable[[int], bool],
    deploy_running: Callable[[], bool],
    sleep: Callable[[float], None],
) -> None:
    """Is TOW answering; if not, why (the supervisor restarts it, this pass only says so)."""
    report = p.report
    if report["service_ok"]:
        return
    if deploy_running():
        report["skipped"] = "deploy in progress"
        return
    if probes.port_listening(port):
        sleep(HUNG_CONFIRM_SEC)  # it may only be busy
        report["service_ok"] = is_healthy(port)
        p.hung = not report["service_ok"] and probes.port_listening(port)
        if report["service_ok"]:
            return
    p.crash = "" if p.hung else probes.crash_line(since)
    if p.hung:
        p.cause = t("watchdog.cause.hung", p.lang, port=port)
    elif p.crash:
        p.cause = t("watchdog.cause.crashed", p.lang, error=p.crash)
    else:
        p.cause = t("watchdog.cause.not_running", p.lang)
    report["cause"] = p.cause


def _checks(p: _Pass, interval: int, state: dict[str, Any], *, wake_ts: float | None = None) -> None:
    """Are scheduled checks running; if not, since when and why."""
    report = p.report
    health = state.get("health")
    health = health if isinstance(health, dict) else {}
    last_check = _last_scheduled_check(state)
    report["last_check_ts"] = last_check
    # No scheduled check yet: lateness counts from the first pass that saw none.
    watched = p.previous.get("checks_watch_since")
    watch_since = float(watched) if isinstance(watched, (int, float)) and not last_check else p.now()
    if not last_check:
        report["checks_watch_since"] = int(watch_since)
    # Right after a wake the lateness counts from the wake, for this one pass (M7).
    since = max(last_check or watch_since, wake_ts or 0)
    if wake_ts is not None:
        report["wake_ts"] = int(wake_ts)
    late = not since or p.now() - since > 2 * interval + STALE_SLACK_SEC
    failures = _scheduled_failures(state)
    if failures >= CHECK_FAILURES_ALERT:
        report["checks_failing"] = failures  # they start, but every one of them fails (M6)
        report["checks_error"] = str(health.get("check_error") or "")
    report["checks_late"] = late
    report["checks_ok"] = not late and not report.get("checks_failing")
    if not late or not report["service_ok"]:
        return
    error = str(health.get("check_error") or "").strip()
    if error:
        p.checks_why = t("pulse.checks.last_error", p.lang, error=error[:160])
        report["checks_cause"] = p.checks_why


def _backups(p: _Pass, cfg: dict[str, Any]) -> None:
    """Did the last night copy fail, or is the newest good one too old?"""
    from tow.restore_points import cleanup_status
    from tow.snapshots import list_snapshots, status, status_failed

    st = status()
    last_ok = st.get("last_ok_at")
    if not isinstance(last_ok, (int, float)):
        newest = list_snapshots(limit=1)
        last_ok = newest[0]["created_ts"] if newest else None
    watched = p.previous.get("backup_watch_since")
    since = float(watched) if isinstance(watched, (int, float)) else p.now()
    p.report["backup_watch_since"] = int(since)
    failed = status_failed(st)
    stale = p.now() - float(last_ok if last_ok else since) > BACKUP_STALE_SEC
    p.report["backup_ok"] = not failed and not stale
    p.report["backup_cleanup_pending"] = st.get("last_cleanup_pending") is True
    p.report["backup_cleanup_read_error"] = st.get("read_error") is True
    cleanup = cleanup_status(cfg=cfg)
    location = cleanup["location"] or p.previous.get("restore_point_cleanup_location") or ""
    same_folder = p.previous.get("restore_point_cleanup_location") in (None, location)
    previously_known = same_folder and type(p.previous.get("restore_point_cleanup")) is bool
    p.report["restore_point_cleanup_location"] = location
    p.report["restore_point_cleanup_pending"] = cleanup["pending"]
    p.report["restore_point_cleanup_read_error"] = cleanup["read_error"] or (
        cleanup["pending"] is None and previously_known
    )
    if failed:
        reason = st.get("last_error") or t("watchdog.backup.unknown_reason", p.lang)
        p.backup_why = (
            t("watchdog.backup.status_unreadable", p.lang)
            if st.get("read_error")
            else t("watchdog.backup.failed", p.lang, error=reason)
        )
    elif stale:
        p.backup_why = (
            t("watchdog.backup.stale", p.lang, at=clock(last_ok)) if last_ok else t("watchdog.backup.never", p.lang)
        )


def _outage(p: _Pass, probes: Probes, machine: Machine | None) -> None:
    """Nothing on this machine can speak while it is off or asleep; say it on the way back."""
    last_pass = p.previous.get("at")
    if not isinstance(last_pass, (int, float)) or p.now() - last_pass <= OUTAGE_GAP_SEC:
        return
    asleep = p.previous.get("asleep_sec")
    why = outage_cause(
        since_ts=float(last_pass),
        now_ts=p.now(),
        now_machine=machine,
        asleep_before=float(asleep) if isinstance(asleep, (int, float)) else None,
        events=probes.shutdown_events,
        lang=p.lang,
    )
    p.report["outage"] = {"from": int(last_pass), "to": int(p.now()), "cause": why}
    p.report["alerts"].append(
        t(
            "watchdog.alert.outage",
            p.lang,
            start=clock(last_pass),
            end=clock(p.now()),
            duration=duration(p.now() - last_pass, p.lang),
            cause=why,
        )
    )


def _change_alerts(p: _Pass, current: dict[str, bool]) -> None:
    report, previous = p.report, p.previous
    for key, ok in current.items():
        before = previous.get(key)
        if key.startswith("restore_point_cleanup") and previous.get("restore_point_cleanup_location") not in (
            None,
            report["restore_point_cleanup_location"],
        ):
            before = None  # a new folder cannot resolve a warning about the old one
        if (before if type(before) is bool else True) == ok:
            continue
        if key == "monitoring":
            report["alerts"].append(
                t("watchdog.alert.monitoring_unreadable" if not ok else "watchdog.alert.monitoring_ok", p.lang)
            )
        elif key == "service" and not ok:
            report["alerts"].append(t("watchdog.alert.down", p.lang, cause=p.cause))
        elif key == "backup":
            report["alerts"].append(
                t("watchdog.alert.backup_failing", p.lang, problem=p.backup_why)
                if not ok
                else t("watchdog.alert.backup_ok", p.lang)
            )
        elif key == "backup_cleanup":
            report["alerts"].append(
                t("watchdog.alert.backup_cleanup_pending", p.lang)
                if not ok
                else t("watchdog.alert.backup_cleanup_ok", p.lang)
            )
        elif key == "restore_point_cleanup":
            report["alerts"].append(
                t("watchdog.alert.point_cleanup_pending", p.lang)
                if not ok
                else t("watchdog.alert.point_cleanup_ok", p.lang)
            )
        elif key == "restore_point_cleanup_monitoring":
            report["alerts"].append(
                t(
                    "watchdog.alert.point_cleanup_unreadable" if not ok else "watchdog.alert.point_cleanup_readable",
                    p.lang,
                )
            )
        elif key == "service":
            down_since = previous.get("down_since")
            took = (
                t("watchdog.alert.was_down_for", p.lang, duration=duration(p.now() - down_since, p.lang))
                if isinstance(down_since, (int, float))
                else ""
            )
            report["alerts"].append(t("watchdog.alert.up_again", p.lang) + took)
        elif not ok and report.get("checks_failing") and not report.get("checks_late"):
            report["alerts"].append(
                t(
                    "watchdog.alert.checks_failing",
                    p.lang,
                    count=report["checks_failing"],
                    error=report.get("checks_error") or t("watchdog.backup.unknown_reason", p.lang),
                )
            )
        elif not ok:
            last_check = report.get("last_check_ts")
            last = clock(last_check) if last_check else "—"
            why = f" — {p.checks_why}" if p.checks_why else ""
            report["alerts"].append(t("watchdog.alert.checks_late", p.lang, since=last) + why)
        else:
            report["alerts"].append(t("watchdog.alert.checks_ok", p.lang))


def _remember(p: _Pass, current: dict[str, bool], machine: Machine | None) -> None:
    previous, report = p.previous, p.report
    saved: dict[str, Any] = {**current, "at": int(p.now()), "backup_watch_since": report.get("backup_watch_since")}
    saved["restore_point_cleanup_location"] = report["restore_point_cleanup_location"]
    if report.get("checks_watch_since"):
        saved["checks_watch_since"] = report["checks_watch_since"]
    if machine is not None:
        saved["asleep_sec"] = round(machine.asleep_sec, 1)
        saved["boot_ts"] = int(machine.boot_ts)
    if not current["service"]:
        saved["down_since"] = int(p.now()) if previous.get("service", True) else previous.get("down_since")
    problem = {"at": int(p.now()), "text": "\n".join(report["alerts"])} if report["alerts"] else None
    if problem or previous.get("last_problem"):
        saved["last_problem"] = problem or previous.get("last_problem")
    _save_state(saved)


def run_watchdog(
    *,
    is_healthy: Callable[[int], bool] = healthy,
    deploy_running: Callable[[], bool] = _deploy_running,
    send: Callable[[str], bool] | None = None,
    now: Callable[[], float] = time.time,
    sleep: Callable[[float], None] = time.sleep,
    pulse: Callable[[str, bool], bool] | None = None,
    flush: Callable[[], None] | None = None,
    probes: Probes | None = None,
    wake_ts: float | None = None,
) -> dict[str, Any]:
    """One watchdog pass; returns what it saw and why.

    ``tow run`` calls it every 10 minutes with its own probes and ``wake_ts``, the last time it
    saw the machine wake up.
    """
    from tow.store import load_state

    cfg = load_config()
    port = port_of(cfg)
    interval = interval_sec_of(cfg)
    probes = probes or _default_probes()
    previous = _load_state()
    last_pass = previous.get("at")
    since = float(last_pass) if isinstance(last_pass, (int, float)) else now() - 600
    report: dict[str, Any] = {
        "port": port,
        "service_ok": is_healthy(port),
        "monitoring_ok": not previous.get("read_error"),
        "alerts": [],
    }
    p = _Pass(now=now, previous=previous, report=report, lang=i18n.message_language(cfg))

    _service(
        p,
        port,
        probes,
        since,
        is_healthy=is_healthy,
        deploy_running=deploy_running,
        sleep=sleep,
    )
    machine = probes.machine()
    wakes = [ts for ts in (_wake_ts(p, machine), wake_ts) if ts is not None]
    _checks(p, interval, load_state(), wake_ts=max(wakes) if wakes else None)
    _backups(p, cfg)
    current = {
        "monitoring": report["monitoring_ok"],
        "service": report["service_ok"],
        "checks": report["checks_ok"],
        "backup": report["backup_ok"],
        "restore_point_cleanup_monitoring": not report["restore_point_cleanup_read_error"],
    }
    # An unreadable observation keeps the last confirmed result, not a fabricated recovery.
    if not report["backup_cleanup_read_error"]:
        current["backup_cleanup"] = not report["backup_cleanup_pending"]
    elif type(previous.get("backup_cleanup")) is bool:
        current["backup_cleanup"] = previous["backup_cleanup"]
    pending = report["restore_point_cleanup_pending"]
    if type(pending) is bool:
        current["restore_point_cleanup"] = not pending
    elif (
        previous.get("restore_point_cleanup_location") in (None, report["restore_point_cleanup_location"])
        and type(previous.get("restore_point_cleanup")) is bool
    ):
        current["restore_point_cleanup"] = previous["restore_point_cleanup"]
    if report.get("skipped"):
        current["service"] = previous.get("service", True)  # a deploy is not an outage
    _outage(p, probes, machine)
    _change_alerts(p, current)
    _remember(p, current, machine)

    heartbeat = str(cfg.get("heartbeat_url") or "").strip()
    if heartbeat:
        healthy_now = bool(report["service_ok"] and report["checks_ok"])
        report["heartbeat"] = (pulse or (lambda u, ok: ping_heartbeat(u, ok=ok)))(heartbeat, healthy_now)
    (flush or _flush_queued_messages)()
    if report["alerts"]:
        delivered = (send or send_to_messengers)("\n".join(report["alerts"]))
        report["delivered"] = delivered
        log_event("watchdog_alert", alerts=report["alerts"], delivered=delivered, how="auto")
    return report


def last_problem() -> dict[str, Any] | None:
    """The last thing the watchdog reported (for the settings page)."""
    state = _load_state()
    if state.get("read_error"):
        return {"at": None, "text": t("watchdog.alert.monitoring_unreadable")}
    value = state.get("last_problem")
    return value if isinstance(value, dict) and value.get("text") else None


def _flush_queued_messages() -> None:
    """Messages not delivered yet, retried on every pass: those a check staged but could not hand
    over (TOW stopped in between) and those a messenger could not take."""
    from tow.delivery import dispatch
    from tow.notifiers import flush_outbox
    from tow.store import SecretStoreError, load_secrets, load_state

    state = load_state()
    if not state.get("notify_outbox") and not state.get("notify_pending"):
        return
    try:
        secrets = load_secrets()
    except SecretStoreError:
        return
    if state.get("notify_pending"):
        dispatch(lambda *, text, operation_id, topic: True)  # into the queue; flushed just below
    flush_outbox(secrets)


def send_to_messengers(text: str) -> bool:
    from tow.notify import send
    from tow.store import SecretStoreError, load_secrets

    try:
        return send(load_secrets(), text)
    except SecretStoreError:
        return False
