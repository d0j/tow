"""An overdue clock fact is normalized once, never postponed on each tick."""

from datetime import UTC, datetime

import pytest

from tow.supervisor.schedule import Schedule

T0 = datetime(2026, 10, 4, 12, tzinfo=UTC).timestamp()


@pytest.mark.parametrize("jump", [60, 3600, 86400, 365 * 86400])
@pytest.mark.parametrize("source", ["persisted", "own", "both"])
def test_future_check_becomes_due_after_one_interval(jump, source):
    now = T0 - jump
    schedule = Schedule(started_at=now, interval_sec=3600, zone=UTC)
    if source != "persisted":
        schedule.started("check", T0)
    persisted = T0 if source != "own" else 0
    for elapsed in [0, 1, 3599, 3600, 7200]:
        at = now + elapsed
        expected = now + 3600
        assert schedule.check_due_at(at, persisted) == expected
        facts = {"last_scheduled_check": persisted, "last_backup_ok": now, "last_backup_attempt": now}
        assert schedule.next_due(at, **facts)["check"] == expected
        assert ("check" in dict(schedule.due(at, **facts))) is (elapsed >= 3600)


@pytest.mark.parametrize(("name", "period"), [("progress", 1800), ("watchdog", 600)])
@pytest.mark.parametrize("jump", [60, 3600, 86400])
def test_future_periodic_job_is_not_starved(name, period, jump):
    now = T0 - jump
    schedule = Schedule(started_at=now, zone=UTC)
    schedule.started(name, T0)
    facts = {"last_scheduled_check": now, "last_backup_ok": now, "last_backup_attempt": now}
    for elapsed in [0, 1, period - 1, period, period + 1]:
        assert schedule.next_due(now + elapsed, **facts)[name] == now + period
        assert (name in dict(schedule.due(now + elapsed, **facts))) is (elapsed >= period)


@pytest.mark.parametrize(("name", "first"), [("check", 120), ("progress", 300), ("watchdog", 60)])
def test_clock_moved_back_before_first_job_does_not_stall_startup(name, first):
    schedule = Schedule(started_at=T0, zone=UTC)
    now = T0 - 86400
    facts = {"last_scheduled_check": 0, "last_backup_ok": now, "last_backup_attempt": now}
    for elapsed in [0, 1, first - 1, first, first + 1]:
        assert schedule.next_due(now + elapsed, **facts)[name] == now + first
        assert (name in dict(schedule.due(now + elapsed, **facts))) is (elapsed >= first)


def test_future_good_backup_does_not_skip_every_following_day():
    schedule = Schedule(started_at=T0, zone=UTC)
    future_ok = T0 + 365 * 86400
    tomorrow = schedule.next_slot(T0)
    assert schedule.backup_due_at(T0, future_ok, 0) == tomorrow
    assert schedule.backup_due_at(tomorrow - 1, future_ok, 0) == tomorrow
    assert schedule.backup_due_at(tomorrow, future_ok, 0) == tomorrow
    assert schedule.backup_due_at(tomorrow + 1, future_ok, 0) == tomorrow


@pytest.mark.parametrize("source", ["persisted", "own", "both"])
def test_future_failed_backup_is_retried_within_six_hours(source):
    schedule = Schedule(started_at=T0, zone=UTC)
    future = T0 + 365 * 86400
    if source != "persisted":
        schedule.started("backup", future)
    attempted = future if source != "own" else 0
    retry = T0 + 6 * 3600
    assert schedule.backup_due_at(T0, T0 - 86400, attempted) == retry
    assert schedule.backup_due_at(retry - 1, T0 - 86400, attempted) == retry
    assert schedule.backup_due_at(retry, T0 - 86400, attempted) == retry


def test_new_real_check_replaces_old_correction_anchor():
    schedule = Schedule(started_at=T0, zone=UTC)
    future = T0 + 86400
    assert schedule.check_due_at(T0, future) == T0 + 3600
    schedule.started("check", T0 + 3600)
    assert schedule.check_due_at(T0 + 3601, T0 + 3600) == T0 + 7200
    assert schedule._corrected == {}


def test_repeated_backward_corrections_pin_each_anchor_once():
    schedule = Schedule(started_at=T0, zone=UTC)
    future = T0 + 86400
    assert schedule.check_due_at(T0, future) == T0 + 3600
    assert schedule.check_due_at(T0 + 1000, future) == T0 + 3600
    assert schedule.check_due_at(T0 - 3600, future) == T0
    assert schedule.check_due_at(T0 - 1000, future) == T0
    assert schedule.check_due_at(T0, future) == T0


def test_elapsed_correction_does_not_jump_back_to_old_date():
    schedule = Schedule(started_at=T0, zone=UTC)
    future = T0 + 1
    assert schedule.check_due_at(T0, future) == T0 + 3600
    assert schedule.check_due_at(T0 + 3600, future) == T0 + 3600


def test_correction_storage_is_bounded_by_fact_sources():
    schedule = Schedule(started_at=T0, zone=UTC)
    for step in range(500):
        now = T0 - step
        schedule.next_due(now, last_scheduled_check=T0 + step, last_backup_ok=T0 + step, last_backup_attempt=T0 + step)
    assert len(schedule._corrected) <= 8
