from datetime import UTC, datetime, timedelta, timezone
from zoneinfo import ZoneInfo

import pytest

import tow.clock
from tow.clock import format_ui_timestamp, iso_from_epoch, machine_now, machine_timezone_label, parse_timestamp


def test_machine_now_is_timezone_aware_and_local():
    now = machine_now()
    assert now.tzinfo is not None
    assert now.utcoffset() is not None


def test_format_ui_timestamp_uses_machine_timezone_and_seconds():
    value = format_ui_timestamp(datetime(2026, 9, 12, 22, 58, 40, tzinfo=machine_now().tzinfo))
    assert value.startswith("12.09.2026 22:58:40 ")
    assert "UTC" in value


def test_the_suite_runs_in_israel_time_whatever_the_machine_zone():
    """conftest pins the zone; these hold on a UTC or New York machine too."""
    assert format_ui_timestamp("2026-09-13T16:05:25+00:00") == "13.09.2026 19:05:25 IDT UTC+03:00"
    assert format_ui_timestamp("2026-01-13T17:05:25+00:00") == "13.01.2026 19:05:25 IST UTC+02:00"
    assert iso_from_epoch(datetime(2026, 9, 11, 18, 30, 47, tzinfo=UTC).timestamp()) == "2026-09-11T21:30:47+03:00"
    assert parse_timestamp("2026-09-13T19:05:25").utcoffset() is not None


@pytest.mark.parametrize(
    ("zone", "stamp", "iso"),
    [
        (ZoneInfo("UTC"), "13.09.2026 16:05:25 UTC", "2026-09-11T18:30:47+00:00"),
        (ZoneInfo("America/New_York"), "13.09.2026 12:05:25 EDT UTC-04:00", "2026-09-11T14:30:47-04:00"),
        (timezone(timedelta(hours=5, minutes=30)), "13.09.2026 21:35:25 UTC+05:30", "2026-09-12T00:00:47+05:30"),
    ],
)
def test_local_zone_seam_drives_every_local_time(monkeypatch, zone, stamp, iso):
    monkeypatch.setattr(tow.clock, "local_zone", lambda: zone)

    assert format_ui_timestamp("2026-09-13T16:05:25+00:00") == stamp
    assert iso_from_epoch(datetime(2026, 9, 11, 18, 30, 47, tzinfo=UTC).timestamp()) == iso
    assert machine_now().tzinfo is zone
    assert parse_timestamp("2026-09-13T19:05:25").tzinfo is zone


def test_a_naive_time_is_local_wall_time_of_its_own_date():
    """Not today's offset: a winter time read in summer was an hour off (Israel: +02 vs +03)."""
    assert parse_timestamp("2026-01-13T19:05:25").utcoffset() == timedelta(hours=2)
    assert parse_timestamp("2026-07-13T19:05:25").utcoffset() == timedelta(hours=3)


def test_undo_reads_times_like_the_rest_of_tow():
    """_undo_is_live took a naive time as UTC, parse_timestamp as local: one rule now."""
    from tow import undo

    local_now = machine_now().replace(tzinfo=None, microsecond=0)  # naive, local wall time
    # Israel is UTC+2/+3: read as UTC, this local "now" was hours in the future or past.
    assert undo.is_live({"kind": "topic", "ts": local_now.isoformat()}) is True
    two_hours_ago = (machine_now() - timedelta(hours=2)).replace(tzinfo=None)
    assert undo.is_live({"kind": "topic", "ts": two_hours_ago.isoformat()}) is False
    assert undo.is_live({"kind": "topic", "ts": datetime.now(UTC).isoformat()}) is True


@pytest.mark.parametrize(
    ("zone", "label"),
    [
        (UTC, "UTC"),
        (timezone(timedelta(0), "UTC"), "UTC"),
        (timezone(timedelta(0), "GMT"), "UTC"),
        (timezone(timedelta(0), "Coordinated Universal Time"), "UTC"),
        (timezone(timedelta(hours=-5)), "UTC-05:00"),
        (timezone(timedelta(hours=3), "Jerusalem Daylight Time"), "UTC+03:00"),  # a long Windows name: offset only
        (timezone(timedelta(hours=2), "Israel Standard Time"), "UTC+02:00"),
        (ZoneInfo("Asia/Jerusalem"), "IDT UTC+03:00"),
        (timezone(timedelta(hours=3), "+03"), "UTC+03:00"),
        (timezone(timedelta(hours=5, minutes=45), "Nepal Standard Time"), "UTC+05:45"),
        (timezone(timedelta(hours=-4), "EDT"), "EDT UTC-04:00"),
    ],
)
def test_timezone_label_never_says_utc_twice(monkeypatch, zone, label):
    monkeypatch.setattr(tow.clock, "local_zone", lambda: zone)

    assert machine_timezone_label(datetime(2026, 9, 13, 12, 0, tzinfo=UTC)) == label
