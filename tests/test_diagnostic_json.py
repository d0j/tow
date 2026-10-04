"""Small service records must not break observation or turn unknown into success."""

from __future__ import annotations

import io
import json
import math
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient

from tow import diagnostic_json, lifecycle, snapshots, watchdog
from tow.pulse import Probes
from tow.store import save_state
from tow.supervisor import layout

READERS = ("backup", "watchdog", "restart", "supervisor")
BAD_TIMES = (True, float("nan"), float("inf"), -float("inf"), "NaN", "Infinity", 10**400, -1, 1e20, [], {})
TIME_IDS = (
    "bool",
    "nan",
    "inf",
    "negative-inf",
    "nan-text",
    "inf-text",
    "huge",
    "negative",
    "calendar",
    "list",
    "object",
)
BAD_JSON = (
    b"[" * 20000 + b"0" + b"]" * 20000,
    b'{"x":NaN}',
    b'{"x":Infinity}',
    b'{"x":1e9999}',
    b'{"x":-Infinity}',
    b'{"x":',
    b"[]",
    b"null",
    b"\xff",
    b'{"x":' + b"[" * 129 + b"0" + b"]" * 129 + b"}",
)
JSON_IDS = ("deep", "nan", "inf", "overflow", "negative-inf", "truncated", "array", "null", "encoding", "depth-limit")


def _path(name):
    return {
        "backup": snapshots.status_path(),
        "watchdog": watchdog._state_path(),
        "restart": lifecycle.service_restart_path(),
        "supervisor": layout.status_path(),
    }[name]


def _read(name):
    return {
        "backup": snapshots.status,
        "watchdog": watchdog._load_state,
        "restart": lifecycle._read_marker,
        "supervisor": lambda: layout.read_json(_path(name)),
    }[name]()


def _write(name, raw):
    path = _path(name)
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(raw)
    return path


def _watchdog():
    return watchdog.run_watchdog(
        is_healthy=lambda port: True,
        deploy_running=lambda: False,
        now=lambda: 1800000000,
        sleep=lambda seconds: None,
        flush=lambda: None,
        send=lambda text: True,
        probes=Probes.inert(),
    )


@pytest.mark.parametrize("name", READERS)
@pytest.mark.parametrize("raw", BAD_JSON, ids=JSON_IDS)
def test_bad_json_is_refused_read_only(name, raw):
    path = _write(name, raw)
    before = set(path.parent.iterdir())
    value = _read(name)
    assert not value or "x" not in value
    assert path.read_bytes() == raw
    assert set(path.parent.iterdir()) == before
    if name in ("backup", "watchdog"):
        assert value["read_error"] is True
    if name == "restart":
        assert value["status"] == "unknown"


@pytest.mark.parametrize("name", READERS)
def test_missing_records_are_normal_first_start(name):
    assert not _read(name)


@pytest.mark.parametrize("name", READERS)
def test_oversize_record_is_refused_without_rewriting(name, monkeypatch):
    monkeypatch.setattr(diagnostic_json, "MAX_BYTES", 128)
    raw = json.dumps({"x": "a" * 128}).encode()
    path = _write(name, raw)
    assert "x" not in _read(name)
    assert path.read_bytes() == raw


def test_size_is_bounded_during_read_not_only_by_stat(tmp_path, monkeypatch):
    requested = []

    class Handle(io.BytesIO):
        def read(self, size=-1):
            requested.append(size)
            assert size == diagnostic_json.MAX_BYTES + 1
            return super().read(size)

    monkeypatch.setattr(Path, "open", lambda self, mode: Handle(b" " * (diagnostic_json.MAX_BYTES + 2)))
    with pytest.raises(ValueError, match="size limit"):
        diagnostic_json.read_object(tmp_path / "record.json")
    assert requested == [diagnostic_json.MAX_BYTES + 1]


@pytest.mark.parametrize("value", BAD_TIMES, ids=TIME_IDS)
def test_epoch_refuses_bad_scalars(value):
    result = layout._epoch(value)
    assert math.isfinite(result)
    assert not isinstance(result, bool)
    assert result == 0


@pytest.mark.parametrize("value", [0, 1, 1800000000.125, "1800000000.125"])
def test_valid_epoch_and_legacy_numeric_text_remain_supported(value):
    assert diagnostic_json.epoch(value) == float(value)


def test_epoch_must_be_displayable_on_the_current_platform():
    value = diagnostic_json.MAX_EPOCH
    try:
        datetime.fromtimestamp(value, UTC).astimezone()
    except OSError, OverflowError, ValueError:
        assert diagnostic_json.epoch(value) is None
    else:
        assert diagnostic_json.epoch(value) == value


@pytest.mark.parametrize("error", [OSError, ValueError, OverflowError])
def test_platform_calendar_failures_are_refused(monkeypatch, error):
    class Calendar:
        @staticmethod
        def fromtimestamp(*args):
            raise error("synthetic calendar limit")

    monkeypatch.setattr(diagnostic_json, "datetime", Calendar)
    assert diagnostic_json.epoch(1800000000) is None
    _write("backup", b'{"last_ok_at":1800000000}')
    assert snapshots.status()["read_error"] is True


@pytest.mark.parametrize(
    ("name", "field"),
    [
        ("backup", "last_ok_at"),
        ("backup", "last_error_at"),
        ("watchdog", "at"),
        ("watchdog", "down_since"),
        ("watchdog", "asleep_sec"),
        ("watchdog", "backup_watch_since"),
    ],
)
@pytest.mark.parametrize("value", BAD_TIMES, ids=TIME_IDS)
def test_known_timestamps_cannot_poison_records(name, field, value):
    _write(name, json.dumps({field: value}).encode())
    assert _read(name)["read_error"] is True


@pytest.mark.parametrize(
    ("name", "payload"),
    [
        ("backup", {"last_error": []}),
        ("backup", {"last_cleanup_pending": "true"}),
        ("backup", {"last_missing": [1]}),
        ("backup", {"last_bytes": True}),
        ("backup", {"last_bytes": -1}),
        ("watchdog", {"service": 1}),
        ("watchdog", {"last_problem": []}),
        ("watchdog", {"last_problem": {"at": 1e20, "text": "broken"}}),
        ("watchdog", {"last_problem": {"text": []}}),
        ("restart", {"operation_id": []}),
    ],
)
def test_known_container_and_scalar_shapes_are_validated(name, payload):
    _write(name, json.dumps(payload).encode())
    value = _read(name)
    assert value.get("read_error") or value.get("status") == "unknown"


@pytest.mark.parametrize("name", READERS)
def test_io_failure_is_not_first_start(name, monkeypatch):
    def denied(self, *args, **kwargs):
        raise PermissionError("synthetic denial")

    monkeypatch.setattr(Path, "open", denied)
    value = _read(name)
    assert value == {} if name == "supervisor" else value.get("read_error") or value.get("status") == "unknown"


@pytest.mark.parametrize("value", BAD_TIMES, ids=TIME_IDS)
def test_settings_and_watchdog_do_not_crash_or_claim_backup_success(value):
    from tow.web import app

    _write("backup", json.dumps({"last_ok_at": value}).encode())
    save_state({"topics": [], "mirrors": {}})
    response = TestClient(app, raise_server_exceptions=False).get("/settings")
    assert response.status_code == 200
    assert "Результат неизвестен" in response.text
    assert "Последняя копия не удалась" not in response.text
    assert _watchdog()["backup_ok"] is False


def test_unreadable_backup_notifies_once_and_recovers_after_real_record():
    raw = b'{"last_ok_at":NaN}'
    path = _write("backup", raw)
    first, second = _watchdog(), _watchdog()
    assert first["backup_ok"] is False
    assert len(first["alerts"]) == 1
    assert "последний результат неизвестен" in first["alerts"][0]
    assert second["alerts"] == []
    assert path.read_bytes() == raw
    snapshots._record(last_ok_at=1800000000, last_error="")
    assert "read_error" not in snapshots.status()
    assert _watchdog()["alerts"] == ["TOW: ночные копии снова делаются"]
    assert _watchdog()["alerts"] == []


def test_unreadable_watchdog_reports_monitoring_not_fake_outage_then_recovers():
    _write("watchdog", b'{"at":Infinity}')
    first = _watchdog()
    assert first["monitoring_ok"] is False
    assert first["service_ok"] is True
    assert "outage" not in first
    assert first["alerts"] == ["TOW: предыдущая запись сторожа не читается; непрерывность наблюдения неизвестна"]
    assert _watchdog()["alerts"] == ["TOW: записи сторожа снова читаются"]
    assert _watchdog()["alerts"] == []


def test_settings_displays_unreadable_watchdog_without_fabricated_date():
    from tow.web import app

    _write("watchdog", b'{"last_problem":{"at":1e999}}')
    response = TestClient(app, raise_server_exceptions=False).get("/settings")
    assert response.status_code == 200
    assert "непрерывность наблюдения неизвестна" in response.text
    assert "1970" not in response.text


@pytest.mark.parametrize("failed_at", [1800000000, 1799999000])
def test_latest_error_wins_with_equal_or_backward_clock(failed_at):
    _write(
        "backup",
        json.dumps({"last_ok_at": 1800000000, "last_error_at": failed_at, "last_error": "synthetic failure"}).encode(),
    )
    assert snapshots.status_failed(snapshots.status()) is True
    assert _watchdog()["backup_ok"] is False


def test_success_clears_previous_error_even_when_old_error_date_is_in_future():
    _write("backup", json.dumps({"last_ok_at": 1800000000, "last_error_at": 1800001000, "last_error": ""}).encode())
    assert snapshots.status_failed(snapshots.status()) is False
    assert _watchdog()["backup_ok"] is True


@pytest.mark.parametrize(
    ("payload", "failed"),
    [
        ({}, False),
        ({"last_error_at": 2}, True),
        ({"last_error_at": 2, "last_ok_at": 1}, True),
        ({"last_error_at": 2, "last_ok_at": 2}, True),
        ({"last_error_at": 2, "last_ok_at": 3}, False),
    ],
)
def test_legacy_backup_without_error_text_uses_dates(payload, failed):
    assert snapshots.status_failed(payload) is failed


@pytest.mark.parametrize("raw", BAD_JSON, ids=JSON_IDS)
def test_invalid_update_marker_never_hides_outage(tmp_path, raw):
    marker = tmp_path / "update-state.json"
    marker.write_bytes(raw)
    assert watchdog._update_in_progress(marker) is False
    assert marker.read_bytes() == raw


@pytest.mark.parametrize(("offset", "expected"), [(-60, True), (-3600, False), (3600, False)])
def test_update_grace_is_bounded_and_supports_legacy_bom(tmp_path, offset, expected):
    marker = tmp_path / "deploy-state.json"
    raw = (
        b"\xef\xbb\xbf"
        + json.dumps(
            {"status": "in_progress", "started_at": (datetime.now(UTC) + timedelta(seconds=offset)).isoformat()}
        ).encode()
    )
    marker.write_bytes(raw)
    assert watchdog._update_in_progress(marker) is expected
    assert marker.read_bytes() == raw


@pytest.mark.parametrize("durable", [True, False])
@pytest.mark.parametrize("value", [{"x": float("nan")}, {"x": float("inf")}, {"x": "a" * 256}])
def test_bad_diagnostic_write_preserves_old_file(tmp_path, monkeypatch, durable, value):
    path = tmp_path / "record.json"
    before = b'{"ok":true}'
    path.write_bytes(before)
    entries = set(tmp_path.iterdir())
    monkeypatch.setattr(diagnostic_json, "MAX_BYTES", 128)
    with pytest.raises(ValueError, match=r"JSON compliant|size limit"):
        layout.write_json(path, value, durable=durable)
    assert path.read_bytes() == before
    assert set(tmp_path.iterdir()) == entries


@pytest.mark.parametrize(
    "next_jobs", [["bad"], "bad", True, {"check": "9999-12-31T23:59:59-12:00"}, {"check": "invalid"}]
)
def test_bad_plan_does_not_break_countdown(monkeypatch, next_jobs):
    monkeypatch.setattr(layout, "status", lambda: {"next": next_jobs})
    assert layout.next_check_at(3600, {}) is None


@pytest.mark.parametrize("value", BAD_TIMES, ids=TIME_IDS)
def test_bad_persisted_cadence_does_not_escape_to_schedule(value):
    from tow.supervisor.core import Supervisor

    supervisor = object.__new__(Supervisor)
    supervisor._persisted = {"check_started_at": value, "backup_attempt_at": value}
    supervisor._facts = {"last_scheduled_check": value, "last_backup_ok": value}
    assert supervisor._facts_now() == {"last_scheduled_check": 0, "last_backup_ok": 0, "last_backup_attempt": 0}


@pytest.mark.parametrize("value", BAD_TIMES, ids=TIME_IDS)
def test_bad_main_health_timestamp_does_not_crash_watchdog(value):
    assert watchdog._last_scheduled_check({"health": {"auto_at_ts": value}}) == 0
