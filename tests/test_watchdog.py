from __future__ import annotations

import json
from datetime import UTC, datetime, timedelta

import pytest

from tow.check import run as check_run
from tow.store import save_state
from tow.watchdog import run_watchdog


class Clock:
    def __init__(self):
        self.t = datetime(2026, 10, 1, 12, 0, tzinfo=UTC).timestamp()

    def now(self):
        return self.t

    def sleep(self, seconds):
        self.t += seconds


def _run(clock, *, up, deploy=False, probes=None):
    sent: list[str] = []
    report = run_watchdog(
        is_healthy=lambda _port: up,
        deploy_running=lambda: deploy,
        send=lambda text: sent.append(text) or True,
        now=clock.now,
        sleep=clock.sleep,
        probes=probes,
    )
    return report, sent


@pytest.fixture
def clock():
    clock = Clock()
    save_state({"topics": [], "health": {"auto_at_ts": int(clock.t) - 600}})
    return clock


def test_quiet_when_all_is_well(clock):
    report, sent = _run(clock, up=True)

    assert (report["service_ok"], report["checks_ok"]) == (True, True)
    assert sent == []


def test_a_dead_service_is_told_once_and_never_started_here(clock):
    # 1.21: the watchdog only says what it sees; `tow run` restarts the web server itself.
    report, sent = _run(clock, up=False)

    assert report["service_ok"] is False
    assert "started" not in report
    assert sent == ["TOW недоступен: процесс не запущен (закрыт или аварийно завершился)"]


def test_quiet_hours_hold_the_watchdogs_alert_and_queued_retries(clock, monkeypatch):
    from tow import delivery
    from tow.store import load_state

    quiet = {"on": True}
    monkeypatch.setattr(delivery, "quiet_now", lambda cfg, now=None: quiet["on"])
    flushed: list[object] = []
    monkeypatch.setattr("tow.notifiers.flush_outbox", lambda secrets: flushed.append(secrets))
    state = load_state()
    state["notify_pending"] = [{"id": "a", "text": "waiting retry", "operation_id": "bot"}]
    save_state(state)

    report, sent = _run(clock, up=False)
    assert sent == []
    assert report["held"] is True
    assert load_state()["notify_queue"] == ["TOW недоступен: процесс не запущен (закрыт или аварийно завершился)"]
    assert flushed == []  # nothing goes out during the quiet hours, retries neither

    quiet["on"] = False
    _run(clock, up=False)
    state = load_state()
    assert "notify_queue" not in state  # released after the quiet hours: handed to the messengers
    assert "notify_pending" not in state
    assert len(flushed) == 1


def test_outage_is_reported_once_and_its_end_too(clock):
    _, first = _run(clock, up=False)
    _, second = _run(clock, up=False)
    _, third = _run(clock, up=True)

    assert first == ["TOW недоступен: процесс не запущен (закрыт или аварийно завершился)"]
    assert second == []
    assert third == ["TOW снова работает (не работал 1 мин)"]


def test_a_deploy_is_not_an_outage(clock):
    report, sent = _run(clock, up=False, deploy=True)
    assert sent == []
    assert report["skipped"] == "deploy in progress"


def test_stale_scheduled_checks_alert_on_change_only(clock):
    from tow.config import load_config

    interval = int(load_config()["interval_sec"])
    save_state({"topics": [], "health": {"auto_at_ts": int(clock.t) - 3 * interval}})
    report, sent = _run(clock, up=True)
    assert report["checks_ok"] is False
    assert len(sent) == 1
    assert sent[0].startswith("TOW: плановые проверки не выполняются с ")

    _, again = _run(clock, up=True)
    assert again == []

    save_state({"topics": [], "health": {"auto_at_ts": int(clock.t)}})
    _, back = _run(clock, up=True)
    assert back == ["TOW: плановые проверки снова идут"]


def test_a_data_folder_deleted_while_tow_runs_is_reported(clock):
    # QA: deleting data\ while TOW ran went unnoticed - TOW silently went on empty, and the
    # watchdog's own state went with the folder, so it saw nothing to compare.
    import shutil

    from tow.paths import data_dir
    from tow.watchdog import data_marker

    seen = data_marker(create=True)
    assert seen is not None
    assert data_marker(create=True) == seen  # made once
    sent: list[str] = []
    quiet = run_watchdog(
        is_healthy=lambda _port: True, send=sent.append, now=clock.now, sleep=clock.sleep, data_id=seen
    )
    assert "data_lost" not in quiet
    assert sent == []

    from tow.paths import config_path

    for item in data_dir().iterdir():  # the whole folder (config.yaml of the tests lives in it)
        if item != config_path():
            shutil.rmtree(item) if item.is_dir() else item.unlink()
    report = run_watchdog(
        is_healthy=lambda _port: True, send=sent.append, now=clock.now, sleep=clock.sleep, data_id=seen
    )
    assert report["data_lost"] is True
    (alert,) = report["alerts"]
    assert str(data_dir()) in alert
    assert "ночную копию" in alert  # and how to get the data back
    assert sent == [alert]


def test_an_unreadable_data_marker_is_not_a_lost_folder(clock, monkeypatch):
    from tow import watchdog

    def unreadable(**_kwargs):
        raise PermissionError(13, "denied")

    monkeypatch.setattr(watchdog, "data_marker", unreadable)
    report = run_watchdog(is_healthy=lambda _port: True, send=lambda _t: True, now=clock.now, data_id="abc")
    assert "data_lost" not in report


def test_the_supervisor_says_a_lost_data_folder_once(monkeypatch, caplog):
    from tow import supervisor, watchdog

    reports = iter([{"data_lost": True}, {}])
    monkeypatch.setattr(watchdog, "run_watchdog", lambda **kwargs: next(reports))
    monkeypatch.setattr(supervisor, "_expire_undo", lambda: None)
    monkeypatch.setattr(supervisor, "_data_seen", {"id": "old"})
    supervisor._watchdog_pass(None)
    assert supervisor._data_seen["id"] not in (None, "old")  # the folder it goes on with
    assert "data folder was deleted or replaced" in caplog.text
    supervisor._watchdog_pass(None)
    assert caplog.text.count("data folder was deleted or replaced") == 1


def test_a_lost_data_folder_is_said_on_home_in_history_and_by_tow_watchdog(monkeypatch, capsys):
    """Round-3 audit: the alert went to the messengers only - History showed an empty
    "watchdog alert" line and Home the page of a new install. Home says it (with the way back)
    until topics are watched again, History shows the alert, `tow watchdog` reports it."""
    import json

    from fastapi.testclient import TestClient

    from tow import cli, supervisor, watchdog
    from tow.i18n import t
    from tow.log import format_event
    from tow.paths import data_dir
    from tow.store import save_state
    from tow.web import app

    reports = iter([{"data_lost": True}, {}, {}])
    monkeypatch.setattr(watchdog, "run_watchdog", lambda **kwargs: next(reports))
    monkeypatch.setattr(supervisor, "_expire_undo", lambda: None)
    monkeypatch.setattr(supervisor, "_data_seen", {"id": "old"})
    supervisor._watchdog_pass(None)
    supervisor._watchdog_pass(None)  # the next pass: no topics yet, the note stays
    said = t("watchdog.alert.data_lost", folder=str(data_dir()))
    assert supervisor.data_lost() == {"at": supervisor.data_lost()["at"], "folder": str(data_dir())}

    assert said in TestClient(app).get("/", headers={"Accept": "text/html"}).text

    monkeypatch.setattr(
        watchdog, "run_watchdog", lambda **kwargs: {"service_ok": True, "checks_ok": True, "alerts": []}
    )
    assert cli.main(["watchdog", "--json"]) == 2
    report = json.loads(capsys.readouterr().out)
    assert (report["data_lost"], report["alerts"]) == (True, [said])

    detail = format_event({"kind": "watchdog_alert", "alerts": [said], "delivered": False, "how": "auto"})["detail"]
    assert said in detail

    save_state({"topics": [{"id": "t", "title": "Show", "url": "http://rutor.info/torrent/1", "save_path": "M:\\s"}]})
    supervisor._watchdog_pass(None)
    assert supervisor.data_lost() is None
    assert said not in TestClient(app).get("/", headers={"Accept": "text/html"}).text


def test_deploy_marker_counts_only_while_fresh_and_in_progress(tmp_path, monkeypatch):
    from tow import watchdog

    monkeypatch.setattr("tow.paths.repo_root", lambda: tmp_path / "app")
    marker = tmp_path / "deploy-state.json"
    fresh = datetime.now(UTC).isoformat()
    old = (datetime.now(UTC) - timedelta(hours=2)).isoformat()

    marker.write_text(json.dumps({"status": "in_progress", "started_at": fresh}), encoding="utf-8")
    assert watchdog._deploy_running() is True
    marker.write_text(json.dumps({"status": "in_progress", "started_at": old}), encoding="utf-8")
    assert watchdog._deploy_running() is False
    marker.write_text(json.dumps({"status": "ok", "started_at": fresh}), encoding="utf-8")
    assert watchdog._deploy_running() is False


@pytest.mark.parametrize(("up", "expected_ok"), [(True, True), (False, False)])
def test_external_pulse_is_sent_every_pass_with_the_health(clock, up, expected_ok):
    # The watchdog pings heartbeat_url (or URL/fail) so an outside service can alert when
    # the pings stop - the PC switched off or hung, which TOW cannot report itself.
    from tow.config import load_config, save_config

    cfg = load_config()
    cfg["heartbeat_url"] = "https://hc-ping.com/abc"
    save_config(cfg)
    pings = []

    report = run_watchdog(
        is_healthy=lambda _p: up,
        deploy_running=lambda: False,
        send=lambda _t: True,
        now=clock.now,
        sleep=clock.sleep,
        pulse=lambda url, ok: pings.append((url, ok)) or True,
    )

    assert pings == [("https://hc-ping.com/abc", expected_ok)]
    assert report["heartbeat"] is True


def test_ping_goes_to_fail_endpoint_on_a_problem(monkeypatch):
    from tow.watchdog import ping_heartbeat

    calls = []

    class Response:
        status_code = 200

        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

    class Client:
        def __enter__(self):
            return self

        def __exit__(self, *_args):
            return None

        def stream(self, _method, url):
            calls.append(url)
            return Response()

    options = []

    def client(**kwargs):
        options.append(kwargs)
        return Client()

    monkeypatch.setattr("tow.http.client", client)

    assert ping_heartbeat("https://hc-ping.com/abc/", ok=True) is True
    assert ping_heartbeat("https://hc-ping.com/abc", ok=False) is True
    assert calls == ["https://hc-ping.com/abc", "https://hc-ping.com/abc/fail"]
    assert options == [{"follow_redirects": False, "public_only": True}] * 2


def test_heartbeat_url_must_be_https():
    from tow.config import ConfigError, load_config
    from tow.paths import config_path

    config_path().write_text("heartbeat_url: http://example.com/x\n", encoding="utf-8")

    with pytest.raises(ConfigError, match="heartbeat_url must be an https"):
        load_config()


def test_a_long_silence_is_reported_when_the_machine_is_back(clock):
    # No outside service: the next watchdog pass after the PC was off/asleep says so.
    from tow.config import set_interval_sec

    set_interval_sec(12 * 3600)  # checks twice a day: three hours off leaves none overdue
    _run(clock, up=True)  # a normal pass records its time
    clock.t += 3 * 3600  # PC switched off for three hours

    report, sent = _run(clock, up=True)

    assert report["outage"]["to"] - report["outage"]["from"] == 3 * 3600
    assert len(sent) == 1
    assert sent[0] == (
        f"TOW не работал с {fmt(clock.t - 3 * 3600)} до {fmt(clock.t)} (3 ч): "
        "компьютер был выключен или спал, либо TOW не был запущен"
    )

    clock.t += 600  # the next regular pass: quiet again
    _, again = _run(clock, up=True)
    assert again == []


# --- why, and what the watchdog does about it (tow.pulse) -------------------------------------

from tow.pulse import Machine, Probes, describe_shutdown, outage_cause, parse_events  # noqa: E402
from tow.pulse import clock as fmt  # noqa: E402

HOUR = 3600


def _probes(**overrides):
    probes = Probes.inert()
    for name, value in overrides.items():
        setattr(probes, name, value)
    return probes


def test_a_hung_tow_is_named_and_never_stopped_here(clock):
    probes = _probes(port_listening=lambda _p: True)

    report, sent = _run(clock, up=False, probes=probes)

    assert report["cause"] == "процесс завис: порт 8787 занят, но не отвечает"
    assert sent == ["TOW недоступен: процесс завис: порт 8787 занят, но не отвечает"]


def test_a_busy_tow_that_answers_on_the_second_try_is_left_alone(clock):
    answers = iter([False, True])
    report = run_watchdog(
        is_healthy=lambda _p: next(answers),
        deploy_running=lambda: False,
        send=lambda _t: True,
        now=clock.now,
        sleep=clock.sleep,
        probes=_probes(port_listening=lambda _p: True),
    )
    assert report["service_ok"] is True
    assert report["alerts"] == []


def test_a_crash_is_reported_with_its_last_error(clock):
    probes = _probes(crash_line=lambda _since: "OSError: [Errno 28] No space left on device")

    _, sent = _run(clock, up=False, probes=probes)

    assert sent == [
        "TOW недоступен: процесс аварийно завершился, последняя ошибка: OSError: [Errno 28] No space left on device"
    ]


def test_late_checks_say_since_when_and_the_last_error(clock):
    from tow.config import load_config

    interval = int(load_config()["interval_sec"])
    save_state({"topics": [], "health": {"auto_at_ts": int(clock.t) - 3 * interval, "check_error": "qbit: нет связи"}})

    clock_t = clock.t
    _, sent = _run(clock, up=True)
    assert sent == [
        f"TOW: плановые проверки не выполняются с {fmt(clock_t - 3 * interval)} — последняя проверка: qbit: нет связи"
    ]


def test_a_manual_check_or_a_progress_pass_does_not_count_as_a_scheduled_check(clock):
    # 1.21: at_ts moves with every check; only the scheduled ones (auto_at_ts) say the schedule runs.
    from tow.config import load_config

    interval = int(load_config()["interval_sec"])
    save_state({"topics": [], "health": {"at_ts": int(clock.t)}})
    report, sent = _run(clock, up=True)
    assert report["last_check_ts"] == 0
    assert report["checks_ok"] is True  # no scheduled check yet: watched from now on
    clock.t += 2 * interval + 11 * 60
    save_state({"topics": [], "health": {"at_ts": int(clock.t)}})
    report, sent = _run(clock, up=True)
    assert report["checks_ok"] is False
    assert sent[0].splitlines()[-1] == "TOW: плановые проверки не выполняются с —"


def test_the_last_problem_is_kept_for_the_settings_page(clock):
    from tow.watchdog import last_problem

    assert last_problem() is None
    _run(clock, up=False)
    problem = last_problem()
    assert problem is not None
    assert problem["text"].startswith("TOW недоступен")
    clock.t += 600
    _run(clock, up=False)  # a pass without news keeps it
    assert last_problem() == problem


# outage causes -------------------------------------------------------------------------------

T0 = 1_790_000_000.0


def _cause(machine, *, asleep_before=0.0, events=()):
    return outage_cause(
        since_ts=T0,
        now_ts=T0 + 5 * HOUR,
        now_machine=machine,
        asleep_before=asleep_before,
        events=lambda _a, _b: list(events),
    )


def test_outage_after_windows_update_restart():
    events = [
        {
            "id": 1074,
            "ts": T0 + HOUR,
            "process": r"C:\WINDOWS\servicing\TrustedInstaller.exe (PC)",
            "reason": "Operating System: Upgrade (Planned)",
            "action": "restart",
        }
    ]
    text = _cause(Machine(boot_ts=T0 + HOUR + 120, asleep_sec=0, logon_ts=T0 + HOUR + 150), events=events)
    assert text.startswith("Центр обновления Windows перезагрузил компьютер в ")
    assert "; снова включён в " in text
    assert "вход в Windows" not in text  # logged on right away


def test_outage_after_a_restart_from_the_start_menu_and_a_late_logon():
    events = [
        {
            "id": 1074,
            "ts": T0 + HOUR,
            "process": r"C:\WINDOWS\SystemApps\X\StartMenuExperienceHost.exe (PC)",
            "reason": "Other (Unplanned)",
            "action": "power off",
        }
    ]
    text = _cause(Machine(boot_ts=T0 + 2 * HOUR, asleep_sec=0, logon_ts=T0 + 3 * HOUR), events=events)
    assert text.startswith("компьютер выключили через меню «Пуск» в ")
    assert "вход в Windows — " in text
    assert "(TOW начинает работу после входа)" in text


def test_outage_after_a_power_loss():
    events = [{"id": 41, "ts": T0 + 2 * HOUR, "process": "", "reason": "", "action": ""}]
    text = _cause(Machine(boot_ts=T0 + 2 * HOUR, asleep_sec=0), events=events)
    assert text.startswith("компьютер аварийно выключился около ")
    assert "(пропало питание, компьютер завис или произошёл сбой Windows)" in text


def test_outage_while_asleep_logged_off_or_unexplained():
    asleep = _cause(Machine(boot_ts=T0 - 9 * HOUR, asleep_sec=4.5 * HOUR), asleep_before=0.5 * HOUR)
    assert asleep == "компьютер спал примерно 4 ч"
    logged_off = _cause(Machine(boot_ts=T0 - 9 * HOUR, asleep_sec=0, logon_ts=T0 + 4 * HOUR))
    assert logged_off.startswith("вы выходили из Windows; вход — ")
    awake = _cause(Machine(boot_ts=T0 - 9 * HOUR, asleep_sec=0, logon_ts=T0 - 9 * HOUR))
    assert awake == "компьютер работал, но TOW не был запущен"
    assert _cause(None).startswith("компьютер был выключен или спал")


def test_outage_message_uses_the_machine_facts(clock):
    _run(clock, up=True)
    clock.t += 3 * HOUR
    probes = _probes(machine=lambda: Machine(boot_ts=clock.t - 600, asleep_sec=0, logon_ts=clock.t - 590))

    _, sent = _run(clock, up=True, probes=probes)

    assert sent == [
        (
            f"TOW не работал с {fmt(clock.t - 3 * HOUR)} до {fmt(clock.t)} (3 ч):"
            f" компьютер был выключен или перезагружен; снова включён в {fmt(clock.t - 600)}"
        )
    ]


def test_shutdown_events_are_parsed_from_the_event_log_xml():
    ns = "xmlns='http://schemas.microsoft.com/win/2004/08/events/event'"
    raw = (
        f"<Event {ns}><System><EventID Qualifiers='32768'>1074</EventID>"
        "<TimeCreated SystemTime='2026-09-12T11:02:11.2667912Z'/></System><EventData>"
        "<Data Name='param1'>C:\\WINDOWS\\servicing\\TrustedInstaller.exe (PC)</Data>"
        "<Data Name='param3'>Operating System: Upgrade (Planned)</Data><Data Name='param5'>restart</Data>"
        f"</EventData></Event><Event {ns}><System><EventID>41</EventID>"
        "<TimeCreated SystemTime='2026-09-10T08:00:00.0000000Z'/></System></Event>"
    )
    events = parse_events(raw)
    assert [e["id"] for e in events] == [41, 1074]  # oldest first
    assert events[1]["action"] == "restart"
    assert describe_shutdown(events).startswith("Центр обновления Windows перезагрузил компьютер в ")
    assert parse_events("not xml <") == []
    assert describe_shutdown([]) == ""


def test_crash_line_reads_the_last_error_after_the_last_pass(tmp_path):
    import os

    from tow.pulse import last_crash_line

    (tmp_path / "serve-stderr.log").write_text(
        "starting\nTraceback (most recent call last):\n  File x\nRuntimeError: boom at https://user:pw@host/x?k=1\n",
        encoding="utf-8",
    )
    os.utime(tmp_path / "serve-stderr.log", (T0 + 10, T0 + 10))
    assert last_crash_line(tmp_path, T0) == "RuntimeError: boom at https://host"  # no login, path or query
    assert last_crash_line(tmp_path, T0 + 60) == ""  # older than the last pass: not this outage


def test_crash_line_ignores_normal_records(tmp_path):
    from tow.pulse import last_crash_line

    (tmp_path / "serve.log").write_text(
        "2026-10-01 15:28:15,163 INFO uvicorn.error: Uvicorn running on http://0.0.0.0:8787\n"
        '2026-10-01 15:29:00,000 INFO uvicorn.access: 127.0.0.1 - "GET /healthz HTTP/1.1" 500\n',
        encoding="utf-8",
    )
    assert last_crash_line(tmp_path, 0) == ""
    with (tmp_path / "serve.log").open("a", encoding="utf-8") as handle:
        handle.write("2026-10-01 15:30:00,000 ERROR tow.web: store unavailable\n")
    assert last_crash_line(tmp_path, 0) == "2026-10-01 15:30:00,000 ERROR tow.web: store unavailable"


def test_update_restarts_are_mentioned_before_the_last_manual_one():
    update = {
        "id": 1074,
        "ts": T0,
        "process": r"C:\WINDOWS\servicing\TrustedInstaller.exe (PC)",
        "reason": "Operating System: Upgrade (Planned)",
        "action": "restart",
    }
    manual = {
        "id": 1074,
        "ts": T0 + 600,
        "process": r"C:\X\StartMenuExperienceHost.exe (PC)",
        "reason": "Other (Unplanned)",
        "action": "restart",
    }
    text = describe_shutdown([update, manual])
    assert text.startswith(
        "Центр обновления Windows установил обновления и перезагрузил компьютер; затем компьютер перезагрузили"
    )


# --- no proxy for the local health check ------------------------------------------------------


def test_the_local_health_check_ignores_system_proxies(monkeypatch):
    import httpx

    from tow.watchdog import healthy

    seen = {}

    def get(url, **kwargs):
        seen.update(kwargs, url=url)
        return httpx.Response(200, json={"ok": True})

    monkeypatch.setenv("HTTP_PROXY", "http://proxy.invalid:3128")
    monkeypatch.setattr(httpx, "get", get)
    assert healthy(8787) is True
    assert seen["url"] == "http://127.0.0.1:8787/healthz"
    assert seen["trust_env"] is False  # HTTP_PROXY made a healthy TOW look hung


def test_the_health_check_can_ask_for_one_install(monkeypatch):
    import httpx

    from tow.watchdog import healthy

    answer = {"ok": True, "install": "aaaa"}
    monkeypatch.setattr(httpx, "get", lambda url, **kwargs: httpx.Response(200, json=answer))
    assert healthy(8787) is True  # any TOW
    assert healthy(8787, install="aaaa") is True
    assert healthy(8787, install="bbbb") is False  # a copy of the folder on the same port
    answer.pop("install")
    assert healthy(8787, install="aaaa") is False


# --- failing scheduled checks are not "on time"; no false "late" after a wake (M6, M7) --------


def test_a_scheduled_check_failing_every_run_is_reported(clock, monkeypatch):
    from tow import check

    monkeypatch.setattr(check_run, "machine_now", lambda: datetime.fromtimestamp(clock.t, UTC))
    good = int(clock.t) - 600
    save_state({"topics": [], "health": {"auto_at_ts": good, "at_ts": good}})
    for _ in range(3):
        health = check.record_check_failure(RuntimeError("legacy plaintext secrets"), how="auto")
    assert health["auto_at_ts"] == int(clock.t)  # the header countdown runs from the attempt
    assert health["auto_ok_at_ts"] == good  # the watchdog from the last check that ran
    assert health["check_failures"] == 3
    check.record_check_failure(RuntimeError("x"), how="manual")  # a manual one does not count
    report, sent = _run(clock, up=True)
    assert report["checks_ok"] is False
    assert sent == [
        "TOW: плановые проверки запускаются, но не проходят (неудачных запусков подряд: 3); последняя ошибка: error"
    ]


def test_failed_attempts_do_not_keep_the_schedule_looking_fresh(clock, monkeypatch):
    from tow import check
    from tow.config import load_config

    interval = int(load_config()["interval_sec"])
    monkeypatch.setattr(check_run, "machine_now", lambda: datetime.fromtimestamp(clock.t, UTC))
    old = int(clock.t) - 3 * interval
    save_state({"topics": [], "health": {"auto_at_ts": old, "at_ts": old}})
    check.record_check_failure(RuntimeError("x"), how="auto")  # one fresh but failed attempt
    report, _ = _run(clock, up=True)
    assert report["last_check_ts"] == old
    assert report["checks_late"] is True


def test_a_completed_scheduled_check_resets_the_failure_count(monkeypatch):
    from test_check_contract import FakeClient, _wire_fake_check

    from tow import check
    from tow.store import load_state

    save_state({"topics": [], "health": {"auto_ok_at_ts": 5, "check_failures": 4, "check_ok": False}})
    _wire_fake_check(monkeypatch, FakeClient())
    check.run_check(apply=True, notify=False, how="manual")
    health = load_state()["health"]
    assert health["auto_ok_at_ts"] == 5  # a manual check does not stand for the schedule
    assert "check_failures" not in health
    check.run_check(apply=True, notify=False, how="auto")
    health = load_state()["health"]
    assert health["auto_ok_at_ts"] == health["at_ts"] == health["auto_at_ts"]


def _awake_machine(clock, asleep):
    return lambda: Machine(boot_ts=clock.t - 30 * HOUR, asleep_sec=asleep["sec"])


def test_the_pass_after_a_sleep_measures_lateness_from_the_wake(clock, monkeypatch):
    from tow.config import load_config, save_config

    cfg = load_config()
    cfg["heartbeat_url"] = "https://hc-ping.com/abc"
    save_config(cfg)
    interval = int(load_config()["interval_sec"])
    asleep = {"sec": 100.0}
    probes = _probes(machine=_awake_machine(clock, asleep))
    pings = []

    def run():
        return run_watchdog(
            is_healthy=lambda _p: True,
            deploy_running=lambda: False,
            send=lambda _t: True,
            now=clock.now,
            sleep=clock.sleep,
            pulse=lambda url, ok: pings.append(ok) or True,
            flush=lambda: None,
            probes=probes,
        )

    run()  # a normal pass before the PC goes to sleep
    save_state({"topics": [], "health": {"auto_ok_at_ts": int(clock.t) - interval}})
    clock.t += 2 * interval  # asleep for a whole day: the scheduled check could not run
    asleep["sec"] += 2 * interval - 300  # awake again for five minutes
    report = run()
    assert report["wake_ts"] == int(clock.t) - 300
    assert report["checks_ok"] is True  # the check runs a few minutes after the wake
    assert pings == [True, True]  # and the heartbeat does not go to /fail

    clock.t += 600  # the next pass: the check still has not run - now it is late
    report = run()
    assert report["checks_ok"] is False
    assert pings[-1] is False


def test_a_long_gap_without_a_sleep_is_still_late(clock):
    from tow.config import load_config

    interval = int(load_config()["interval_sec"])
    asleep = {"sec": 100.0}
    probes = _probes(machine=_awake_machine(clock, asleep))
    _run(clock, up=True, probes=probes)
    save_state({"topics": [], "health": {"auto_ok_at_ts": int(clock.t) - interval}})
    clock.t += 2 * interval  # awake all along: the scheduler itself stood still
    report, _ = _run(clock, up=True, probes=probes)
    assert "wake_ts" not in report
    assert report["checks_ok"] is False


# --- 1.18: the update marker and the pass inside `tow run` ------------------------------------


@pytest.mark.parametrize("name", ["update-state.json", "deploy-state.json"])
def test_an_update_marker_holds_the_watchdog_back(tmp_path, monkeypatch, name):
    from tow import watchdog

    monkeypatch.setattr("tow.paths.repo_root", lambda: tmp_path / "app")
    fresh = datetime.now(UTC).isoformat()
    assert watchdog._deploy_running() is False
    (tmp_path / name).write_text(json.dumps({"status": "in_progress", "started_at": fresh}), encoding="utf-8")
    assert watchdog._deploy_running() is True
    (tmp_path / name).write_text("[not a mapping]", encoding="utf-8")
    assert watchdog._deploy_running() is False


def test_the_supervisors_wake_time_counts_for_lateness(clock):
    from tow.config import load_config

    interval = int(load_config()["interval_sec"])
    save_state({"topics": [], "health": {"auto_ok_at_ts": int(clock.t) - 3 * interval}})
    report = run_watchdog(
        is_healthy=lambda _p: True,
        deploy_running=lambda: False,
        send=lambda _t: True,
        now=clock.now,
        sleep=clock.sleep,
        flush=lambda: None,
        probes=_probes(),
        wake_ts=clock.t - 300,
    )
    assert report["wake_ts"] == int(clock.t) - 300
    assert report["checks_ok"] is True


def test_tow_watchdog_by_hand_is_a_real_pass_and_says_so(clock, monkeypatch):
    # Its help and the guides called `tow watchdog` "a diagnostic; it changes nothing", yet by
    # hand it makes the scheduled pass: its alert goes to the messengers and is remembered.
    from tow import cli, watchdog
    from tow.i18n import t

    sent: list[str] = []
    real_pass = watchdog.run_watchdog
    monkeypatch.setattr(
        watchdog,
        "run_watchdog",
        lambda: real_pass(
            is_healthy=lambda _port: False,
            deploy_running=lambda: False,
            send=lambda text: sent.append(text) or True,
            now=clock.now,
            sleep=clock.sleep,
        ),
    )

    assert cli.main(["watchdog"]) == 2
    assert sent  # told
    assert watchdog._load_state().get("service") is False  # and remembered
    for language, nothing in (("en", "changes nothing"), ("ru", "ничего не меняет")):
        said = t("cli.help.watchdog", language)
        assert nothing not in said
        assert ("messengers" if language == "en" else "мессенджеры") in said
