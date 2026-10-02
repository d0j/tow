"""tow.pulse probes with the platform faked (tow.platform is injected; its backends have their own tests).

test_watchdog.py covers the outage wording; these tests cover the probes themselves and the
remaining message branches. Nothing here runs a process or opens a socket.
"""

from __future__ import annotations

from typing import Any

import pytest

from tow import platform, pulse
from tow.i18n import t

T0 = 1_790_000_000.0
HOUR = 3600


# --- durations and clock ----------------------------------------------------------------------


@pytest.mark.parametrize(
    ("seconds", "parts"),
    [
        (0, [("pulse.duration.minutes", 1)]),
        (45 * 60, [("pulse.duration.minutes", 45)]),
        (3 * HOUR + 5 * 60, [("pulse.duration.hours", 3), ("pulse.duration.minutes", 5)]),
        (2 * 24 * HOUR, [("pulse.duration.days", 2)]),
        (2 * 24 * HOUR + 3 * HOUR + 59, [("pulse.duration.days", 2), ("pulse.duration.hours", 3)]),
    ],
)
def test_duration_in_words(seconds, parts):
    expected = " ".join(t(key, "ru", count=count) for key, count in parts)
    assert pulse.duration(seconds, "ru") == expected
    assert pulse.duration(seconds) == expected  # the configured message language (ru in tests)


def test_clock_is_local_day_month_hours_minutes():
    # tests/conftest.py pins Asia/Jerusalem; pulse.clock uses the process's local zone,
    # so only the shape is fixed here.
    import re

    assert re.fullmatch(r"\d\d\.\d\d \d\d:\d\d", pulse.clock(T0))


# --- the machine: boot, sleep and logon times (from tow.platform) ------------------------------


class FakeBackend:
    """Only what pulse asks the platform; tests/test_platform.py covers the real backends."""

    name = "linux"

    def __init__(self, *, boot=None, asleep=None, logon=None, events=(), owner=None, stopped=True) -> None:
        self.boot, self.asleep, self.logon, self.events = boot, asleep, logon, list(events)
        self.owner, self.stopped = owner, stopped
        self.asked: list[tuple] = []

    def boot_time(self, now=None):
        self.asked.append(("boot_time", now))
        return self.boot

    def asleep_seconds(self):
        return self.asleep

    def logon_time(self):
        return self.logon

    def shutdown_reasons(self, since, until):
        self.asked.append(("shutdown_reasons", since, until))
        return self.events

    def port_owner(self, port):
        self.asked.append(("port_owner", port))
        return self.owner

    def terminate(self, pid, timeout=10.0):
        self.asked.append(("terminate", pid, timeout))
        return self.stopped


def test_machine_reads_boot_sleep_and_logon_from_the_platform():
    backend = FakeBackend(boot=T0 - 10 * HOUR, asleep=2 * HOUR, logon=T0 - 2 * HOUR)
    with platform.use(backend):
        machine = pulse.machine(now=T0)

    assert machine == pulse.Machine(boot_ts=T0 - 10 * HOUR, asleep_sec=2 * HOUR, logon_ts=T0 - 2 * HOUR)
    assert backend.asked == [("boot_time", T0)]


@pytest.mark.parametrize(("boot", "asleep"), [(None, 1.0), (T0, None)])
def test_machine_unknown_when_the_platform_does_not_say(boot, asleep):
    with platform.use(FakeBackend(boot=boot, asleep=asleep)):
        assert pulse.machine(now=T0) is None


def test_shutdown_records_come_from_the_platform():
    record = {"id": 6008, "ts": T0 + 60, "process": "", "reason": "", "action": ""}
    backend = FakeBackend(events=[record])
    with platform.use(backend):
        assert pulse.shutdown_events(T0, T0 + HOUR) == [record]
    assert backend.asked == [("shutdown_reasons", T0, T0 + HOUR)]


def test_a_linux_boot_end_is_told_without_guessing_how():
    events = [{"id": "stopped", "ts": T0, "process": "", "reason": "", "action": ""}]
    assert pulse.describe_shutdown(events, "ru") == t("pulse.shutdown.stopped", "ru", at=pulse.clock(T0))


_NS = "xmlns='http://schemas.microsoft.com/win/2004/08/events/event'"


def test_damaged_event_records_are_skipped():
    raw = (
        f"<Event {_NS}><Other/></Event>"  # no System element
        f"<Event {_NS}><System><EventID>abc</EventID>"
        "<TimeCreated SystemTime='2026-09-12T11:02:11Z'/></System></Event>"
        f"<Event {_NS}><System><EventID>41</EventID></System></Event>"  # no TimeCreated
        f"<Event {_NS}><System><EventID>41</EventID>"
        "<TimeCreated SystemTime='2026-09-12T11:02:11Z'/></System></Event>"
    )
    events = pulse.parse_events(raw)
    assert [(e["id"], e["process"], e["reason"], e["action"]) for e in events] == [(41, "", "", "")]


# --- describing the shutdown ----------------------------------------------------------------------


def _shutdown(process: str, action: str = "power off", event_id: int = 1074) -> dict[str, Any]:
    return {"id": event_id, "ts": T0, "process": process, "reason": "Other (Unplanned)", "action": action}


@pytest.mark.parametrize(
    ("process", "via_key", "via_text"),
    [
        (r"C:\WINDOWS\system32\shutdown.exe (PC)", "pulse.via.shutdown_command", None),
        (r"C:\WINDOWS\system32\winlogon.exe (PC)", "pulse.via.logon_screen", None),
        (r"C:\Tools\MyCleaner.exe (PC)", None, "(MyCleaner.exe)"),
        ("", None, ""),
    ],
)
def test_who_powered_the_computer_off(process, via_key, via_text):
    via = t(via_key, "ru") if via_key else via_text
    expected = t("pulse.shutdown.powered_off", "ru", via=via, at=pulse.clock(T0)).replace("  ", " ")
    assert pulse.describe_shutdown([_shutdown(process)], "ru") == expected


def test_a_restart_is_told_apart_from_a_power_off():
    text = pulse.describe_shutdown([_shutdown(r"C:\X\explorer.exe (PC)", action="restart")], "ru")
    via = t("pulse.via.start_menu", "ru")
    assert text == t("pulse.shutdown.restarted", "ru", via=via, at=pulse.clock(T0)).replace("  ", " ")


# --- outage cause: remaining branches ----------------------------------------------------------


def test_outage_without_previous_sleep_reading_falls_through_to_logon():
    machine = pulse.Machine(boot_ts=T0 - 9 * HOUR, asleep_sec=8 * HOUR, logon_ts=T0 + HOUR)
    text = pulse.outage_cause(
        since_ts=T0, now_ts=T0 + 5 * HOUR, now_machine=machine, asleep_before=None, events=lambda *_: [], lang="ru"
    )
    assert text == t("pulse.outage.logged_off", "ru", at=pulse.clock(T0 + HOUR))


def test_a_short_sleep_does_not_explain_a_long_outage():
    machine = pulse.Machine(boot_ts=T0 - 9 * HOUR, asleep_sec=HOUR + 5 * 60, logon_ts=None)
    text = pulse.outage_cause(
        since_ts=T0, now_ts=T0 + 5 * HOUR, now_machine=machine, asleep_before=HOUR, events=lambda *_: [], lang="ru"
    )
    assert text == t("pulse.outage.scheduler_idle", "ru")


def test_a_restart_without_event_log_records_is_still_explained():
    machine = pulse.Machine(boot_ts=T0 + HOUR, asleep_sec=0, logon_ts=None)
    text = pulse.outage_cause(
        since_ts=T0, now_ts=T0 + 5 * HOUR, now_machine=machine, asleep_before=0, events=lambda *_: [], lang="ru"
    )
    what = t("pulse.outage.off_or_restarted", "ru")
    assert text == t("pulse.outage.back_on", "ru", what=what, at=pulse.clock(T0 + HOUR))


# --- the service port and process ---------------------------------------------------------------


def test_port_listening_is_a_connect_to_loopback(monkeypatch):
    seen: list[tuple] = []

    class Connection:
        def __enter__(self):
            return self

        def __exit__(self, *_exc):
            return False

    monkeypatch.setattr(
        pulse.socket, "create_connection", lambda address, timeout: seen.append(address) or Connection()
    )
    assert pulse.port_listening(8787) is True
    assert seen == [("127.0.0.1", 8787)]

    def refused(*_a, **_k):
        raise ConnectionRefusedError

    monkeypatch.setattr(pulse.socket, "create_connection", refused)
    assert pulse.port_listening(8787) is False


def test_crash_line_skips_unreadable_logs(monkeypatch, tmp_path):
    (tmp_path / "serve-stderr.log").mkdir()  # a directory: opening it fails
    (tmp_path / "serve.log").write_text("x\nValueError: bad\n", encoding="utf-8")
    assert pulse.last_crash_line(tmp_path, 0) == "ValueError: bad"
    assert pulse.last_crash_line(tmp_path / "missing", 0) == ""


# --- the probe sets ------------------------------------------------------------------------------


def test_inert_probes_report_nothing():
    probes = pulse.Probes.inert()
    assert probes.machine() is None
    assert probes.shutdown_events(0, 1) == []
    assert probes.port_listening(8787) is False
    assert probes.crash_line(0) == ""
