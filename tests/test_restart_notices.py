"""A normal machine restart is history, while failed recovery remains actionable."""

from __future__ import annotations

import os
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from test_check_pipeline import OLD, Client, Tracker, _topic, _wire

from tow import check, clock, log, messages_feed, platform, watchdog
from tow.check import client_ops
from tow.config import load_config, save_config
from tow.errors import TowError
from tow.pulse import Machine, Probes
from tow.store import load_state, save_state
from tow.supervisor import layout

NOW = datetime(2026, 11, 4, 10, tzinfo=UTC).timestamp()
CFG = {"clients": {"main": {"kind": "qbittorrent", "default": True}}}
SECRETS = {"clients": {"main": {"host": "127.0.0.1"}}}


def feed():
    snapshot = messages_feed.feed().refresh()
    return messages_feed.render(snapshot.projection, lang="en")


@pytest.fixture
def supervisor(monkeypatch):
    facts = {"now": NOW, "boot": NOW - 120}
    job = {
        "name": "check",
        "pid": os.getpid(),
        "since": datetime.fromtimestamp(NOW - 10, UTC).isoformat(),
        "deadline": datetime.fromtimestamp(NOW + 3590, UTC).isoformat(),
    }
    report = {"job": job, "stopping": False}
    monkeypatch.setattr(clock, "machine_now", lambda: datetime.fromtimestamp(facts["now"], UTC))
    monkeypatch.setattr(platform.current(), "boot_time", lambda _now=None: facts["boot"])
    monkeypatch.setattr(layout, "status", lambda: report)
    return facts, report


def pool(monkeypatch, *, previous=None, how="auto", error=None):
    records, messages = [], []

    def ping():
        if error is not None:
            raise error
        return "ok"

    monkeypatch.setattr(client_ops, "_open_client", lambda *_args, **_kwargs: SimpleNamespace(ping=ping))
    previous = previous or {}
    item = client_ops.ClientPool(
        cfg=CFG,
        apply=True,
        notify=True,
        how=how,
        record=lambda kind, **fields: records.append({"kind": kind, **fields}),
        batch=SimpleNamespace(queue=lambda topic, **fields: messages.append(fields["kind"])),
        default_id="main",
        previous_qbit_ok=previous.get("ok", True),
        previous_clients_ok={},
        previous_starting=frozenset(previous.get("starting") or []),
    )
    return item, records, messages


def test_local_refused_socket_during_confirmed_boot_waits_without_false_health_or_down_up_spam(monkeypatch, supervisor):
    item, records, messages = pool(monkeypatch, error=ConnectionRefusedError())
    item.open_default(SECRETS)
    assert records[0]["kind"] == "client_starting"
    assert item.default_ok is False
    assert item.health() == {"main": False}
    assert item.starting_clients() == ["main"]
    assert messages == []
    next_item, _records, recovered = pool(monkeypatch, previous={"ok": False, "starting": item.starting_clients()})
    next_item.open_default(SECRETS)
    assert next_item.default_ok is True
    assert next_item.starting_clients() == []
    assert recovered == []  # never announced down: no misleading recovery notification


def test_a_refusal_after_the_original_boot_grace_is_announced_once(monkeypatch, supervisor):
    facts, _report = supervisor
    facts["now"] += 300
    previous = {"ok": False, "starting": ["main"]}
    item, records, messages = pool(monkeypatch, previous=previous, error=ConnectionRefusedError())
    item.open_default(SECRETS)
    assert records[0]["kind"] == "client_unreachable"
    assert item.default_ok is False
    assert item.starting_clients() == []
    assert messages == ["qbit_down"]
    again, _records, repeated = pool(monkeypatch, previous={"ok": False}, error=ConnectionRefusedError())
    again.open_default(SECRETS)
    assert repeated == []


@pytest.mark.parametrize(("age", "starting"), [(299.999, True), (300, False), (301, False)])
def test_five_minute_startup_boundary_is_measured_from_boot_not_repeated_failures(
    monkeypatch, supervisor, age, starting
):
    facts, _report = supervisor
    facts["boot"] = facts["now"] - age
    item, records, messages = pool(monkeypatch, error=ConnectionRefusedError())
    item.open_default(SECRETS)
    assert records[0]["kind"] == ("client_starting" if starting else "client_unreachable")
    assert item.default_ok is False
    assert messages == ([] if starting else ["qbit_down"])


def test_check_commit_keeps_startup_mark_across_an_unrelated_check_and_clears_it_after_actual_contact(
    monkeypatch, supervisor
):
    client = Client()
    client.put(OLD, "/media/tv", tags=["tow"])
    _wire(monkeypatch, {"main": client}, Tracker())
    save_state({"topics": [_topic(hash=OLD, last_ok=True)], "health": {"qbit_ok": True}})
    monkeypatch.setattr(client, "ping", lambda: (_ for _ in ()).throw(ConnectionRefusedError()))
    check.run_check(apply=True, notify=False, how="auto")
    assert load_state()["health"]["qbit_ok"] is False
    assert load_state()["health"]["clients_starting"] == ["main"]
    assert client.adds == []
    check.run_check(apply=True, notify=False, how="manual", ids=[])
    assert load_state()["health"]["clients_starting"] == ["main"]
    monkeypatch.setattr(client, "ping", lambda: "ok")
    check.run_check(apply=True, notify=False, how="auto")
    assert load_state()["health"]["qbit_ok"] is True
    assert "clients_starting" not in load_state()["health"]


@pytest.mark.parametrize(
    "invalid", ["manual", "remote", "no_supervisor", "foreign_job", "expired", "naive", "future_boot", "no_boot"]
)
def test_startup_does_not_quiet_a_manual_remote_unknown_or_unsupervised_failure(monkeypatch, supervisor, invalid):
    facts, report = supervisor
    secrets = SECRETS
    if invalid == "remote":
        secrets = {"clients": {"main": {"host": "remote.example"}}}
    elif invalid == "no_supervisor":
        report.clear()
    elif invalid == "foreign_job":
        report["job"]["pid"] += 1
    elif invalid == "expired":
        facts["now"] += 4000
    elif invalid == "naive":
        report["job"]["deadline"] = "2026-11-04T11:00:00"
    elif invalid == "future_boot":
        facts["boot"] = NOW + 20
    elif invalid == "no_boot":
        facts["boot"] = None
    item, records, messages = pool(
        monkeypatch, how="manual" if invalid == "manual" else "auto", error=ConnectionRefusedError()
    )
    item.open_default(secrets)
    assert records[0]["kind"] == "client_unreachable"
    assert messages == ["qbit_down"]
    assert not item.default_ok


@pytest.mark.parametrize("error", [TimeoutError(), PermissionError(), TowError("client.transmission.bad_login")])
def test_recent_boot_never_quiets_other_errors(monkeypatch, supervisor, error):
    item, records, messages = pool(monkeypatch, error=error)
    item.open_default(SECRETS)
    assert records[0]["kind"] == "client_unreachable"
    assert messages == ["qbit_down"]


def monitor_start():
    cfg = load_config()
    cfg.update(interval_sec=43200, backup_enabled=False, quiet_hours=None)
    save_config(cfg)
    save_state({"topics": [], "health": {"auto_at_ts": int(NOW)}})
    probes = Probes.inert()
    probes.machine = lambda: Machine(NOW - 600, 0, NOW - 590)
    watchdog.run_watchdog(is_healthy=lambda _port: True, now=lambda: NOW, probes=probes, send=lambda _text: True)
    return probes


@pytest.mark.parametrize("normal", ["shutdown", "sleep", "recovered"])
def test_confirmed_clean_return_or_recovery_is_shared_as_information(normal):
    probes = monitor_start()
    if normal == "sleep":
        probes.machine = lambda: Machine(NOW - 600, 4100, NOW - 590)
    elif normal == "shutdown":
        probes.machine = lambda: Machine(NOW + 4000, 0, NOW + 4030)
        probes.shutdown_events = lambda _a, _b: [
            {
                "id": 1074,
                "ts": NOW + 30,
                "process": "StartMenuExperienceHost.exe",
                "action": "shutdown",
                "power_action": "power_off",
            }
        ]
    else:
        watchdog.run_watchdog(
            is_healthy=lambda _port: False, now=lambda: NOW + 600, probes=probes, send=lambda _text: True
        )
    at = NOW + (1200 if normal == "recovered" else 4200)
    delivered = []
    report = watchdog.run_watchdog(
        is_healthy=lambda _port: True, now=lambda: at, probes=probes, send=lambda text: delivered.append(text) or True
    )
    assert report["alerts"]
    assert log.read_events(limit=1)[0]["kind"] == "watchdog_notice", {
        key: value for key, value in watchdog._load_state().items() if isinstance(value, bool)
    }
    assert feed()["items"][0]["kind"] == "info"
    assert feed()["items"][0]["text"].startswith("TOW service status")
    assert delivered
    if normal != "recovered":
        assert delivered[0].startswith("TOW снова работает; пауза")
        assert "Тревога" not in delivered[0]


@pytest.mark.parametrize(
    "problem", ["unknown", "crash", "crash_recorded_after_boot", "delayed", "service_down", "checks_fail"]
)
def test_unknown_crash_delayed_return_or_another_current_failure_stays_a_warning(problem):
    probes = monitor_start()
    probes.machine = lambda: Machine(NOW + 4000, 0, NOW + 4030)
    event = {
        "id": 1074,
        "ts": NOW + 30,
        "process": "StartMenuExperienceHost.exe",
        "action": "shutdown",
        "power_action": "power_off",
    }
    if problem == "crash":
        event = {"id": 41, "ts": NOW + 30}
    observations = [] if problem == "unknown" else [event]
    if problem == "crash_recorded_after_boot":
        observations.append({"id": 41, "ts": NOW + 4010})
    probes.shutdown_events = lambda start, end: [item for item in observations if start <= item["ts"] <= end]
    if problem == "checks_fail":
        save_state({"topics": [], "health": {"auto_at_ts": NOW, "check_failures": 3, "check_error": "synthetic"}})
    at = NOW + (6000 if problem == "delayed" else 4200)
    report = watchdog.run_watchdog(
        is_healthy=lambda _port: problem != "service_down", now=lambda: at, probes=probes, send=lambda _text: True
    )
    assert report["alerts"]
    assert log.read_events(limit=1)[0]["kind"] == "watchdog_alert"
    assert feed()["items"][0]["kind"] == "warn"


def test_a_pending_clean_restart_notice_keeps_its_first_payload_and_severity_after_diagnostic_crash(monkeypatch):
    probes = monitor_start()
    probes.machine = lambda: Machine(NOW + 4000, 0, NOW + 4030)
    probes.shutdown_events = lambda _a, _b: [
        {"id": 1074, "ts": NOW + 30, "process": "explorer.exe", "action": "shutdown", "power_action": "power_off"}
    ]
    original = watchdog._save_state
    monkeypatch.setattr(watchdog, "_save_state", lambda _value: (_ for _ in ()).throw(OSError("synthetic crash")))
    with pytest.raises(OSError, match="synthetic crash"):
        watchdog.run_watchdog(
            is_healthy=lambda _port: True, now=lambda: NOW + 4200, probes=probes, send=lambda _text: True
        )
    staged = load_state()["watchdog_notice_observation"]
    token = load_state()["watchdog_notice_revision"]
    assert staged["notice"] is True
    monkeypatch.setattr(watchdog, "_save_state", original)
    delivered = []
    report = watchdog.run_watchdog(
        is_healthy=lambda _port: True,
        now=lambda: NOW + 4800,  # now outside grace: retrying a diagnostic is not another outage
        probes=probes,
        send=lambda text: delivered.append(text) or True,
    )
    assert report["notice"] is True
    assert report["alerts"] == staged["alerts"]
    assert delivered == ["\n".join(staged["alerts"])]
    assert load_state()["watchdog_notice_revision"] == token
    assert log.read_events(limit=1)[0]["kind"] == "watchdog_notice"
    assert watchdog.last_problem()["at"] == NOW + 4200
