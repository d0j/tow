"""Only a live, current, bounded continuation may defer a site's owner attention."""

from __future__ import annotations

from copy import deepcopy
from datetime import UTC, datetime

import pytest
from test_site_retries import T0, config, failed

from tow import site_retries
from tow.supervisor import layout


def iso(value):
    return datetime.fromtimestamp(value, UTC).isoformat()


def world():
    state = {"topics": [failed()], "mirrors": {}}
    cfg = config()
    item = site_retries.policies(state, cfg, now=T0)["a"]
    report = {
        "at": iso(T0 - 86400),  # status is change-driven, deliberately not a heartbeat
        "site_retries": {"a": {"at": iso(T0 + 1800), "revision": item["revision"], "observed_at": T0}},
    }
    return state, cfg, item, report, {}


def project(state, cfg, item, report, schedule, *, now=T0):
    return site_retries.recovery_status(state, cfg, report=report, schedule=schedule, now=now)


def test_actual_future_plan_is_pending_without_recomputing_or_writing_any_evidence():
    args = world()
    before = deepcopy(args)
    [entry] = project(*args).values()
    assert entry == {
        "phase": "scheduled",
        "revision": args[2]["revision"],
        "observed_at": T0,
        "next_at": T0 + 1800,
        "deadline": T0 + 1860,
        "attempt": 1,
        "reason": "site_retry",
    }
    assert args == before


@pytest.mark.parametrize("change", ["missing", "stopping", "observation", "revision", "timestamp", "too_far"])
def test_missing_stale_or_invalid_report_never_hides_attention(change):
    state, cfg, item, report, schedule = world()
    if change == "missing":
        report = {}
    elif change == "stopping":
        report["stopping"] = True
    else:
        field, value = {
            "observation": ("observed_at", T0 - 1),
            "revision": ("revision", "old"),
            "timestamp": ("at", "not a time"),
            "too_far": ("at", iso(T0 + 86400 * 365)),
        }[change]
        report["site_retries"]["a"][field] = value
    assert project(state, cfg, item, report, schedule) == {}


@pytest.mark.parametrize("count", [3, 0, 4, True, "1"])
def test_exhausted_or_damaged_current_budget_has_no_pending_future_plan(count):
    state, cfg, item, report, schedule = world()
    schedule["site_retry_attempts"] = {"a": {"revision": item["revision"], "count": count, "started_at": T0}}
    assert project(state, cfg, item, report, schedule) == {}


def test_the_last_dispatched_attempt_remains_pending_only_while_its_exact_job_runs():
    state, cfg, item, report, schedule = world()
    report["site_retries"] = {}
    report["job"] = {"name": "site_retry", "pid": 42, "since": iso(T0), "deadline": iso(T0 + 3600)}
    batch = {"revision": item["revision"], "observed_at": T0, "count": 3, "started_at": T0}
    schedule.update(site_retry_batch={"a": batch}, site_retry_attempts={"a": deepcopy(batch)})
    entry = project(state, cfg, item, report, schedule, now=T0 + 100)["a"]
    assert entry["phase"] == "running"
    assert entry["attempt"] == 3
    assert entry["deadline"] == T0 + 3600
    assert project(state, cfg, item, report, schedule, now=T0 + 3600) == {}
    report["job"] = None
    assert project(state, cfg, item, report, schedule) == {}


@pytest.mark.parametrize("change", ["observation", "revision", "count", "start", "job_timeout", "job_pid"])
def test_an_unbound_or_malformed_running_job_is_not_continuation_authority(change):
    state, cfg, item, report, schedule = world()
    report["site_retries"] = {}
    report["job"] = {"name": "site_retry", "pid": 42, "since": iso(T0), "deadline": iso(T0 + 3600)}
    batch = {"revision": item["revision"], "observed_at": T0, "count": 3, "started_at": T0}
    schedule.update(site_retry_batch={"a": batch}, site_retry_attempts={"a": deepcopy(batch)})
    if change == "job_timeout":
        report["job"]["deadline"] = iso(T0 + 86400)
    elif change == "job_pid":
        report["job"]["pid"] = True
    else:
        key, value = {
            "observation": ("observed_at", T0 - 1),
            "revision": ("revision", "old"),
            "count": ("count", True),
            "start": ("started_at", T0 - 5),
        }[change]
        batch[key] = value
    assert project(state, cfg, item, report, schedule) == {}


def test_overdue_plan_has_only_a_short_queue_grace_or_a_proven_bounded_blocking_job():
    state, cfg, item, report, schedule = world()
    at = T0 + 1800
    assert project(state, cfg, item, report, schedule, now=at + 59)["a"]["phase"] == "queued"
    assert project(state, cfg, item, report, schedule, now=at + 60) == {}
    report["job"] = {"name": "backup", "pid": 42, "since": iso(at - 100), "deadline": iso(at + 1700)}
    assert project(state, cfg, item, report, schedule, now=at + 60)["a"]["deadline"] == at + 1760
    assert project(state, cfg, item, report, schedule, now=at + 1700) == {}


def test_a_new_server_pause_is_a_floor_even_for_a_still_current_plan():
    state, cfg, item, report, schedule = world()
    state["mirrors"] = {"demo": {"rate_cool": {"https://demo.example": T0 + 86400}}}
    assert project(state, cfg, item, report, schedule) == {}
    report["site_retries"]["a"]["at"] = iso(T0 + 86400)
    assert project(state, cfg, item, report, schedule)["a"]["next_at"] == T0 + 86400


@pytest.mark.parametrize("field", ["paused", "once_done"])
def test_owner_pause_or_once_completion_cancels_the_pending_projection(field):
    state, cfg, item, report, schedule = world()
    state["topics"][0][field] = True
    assert project(state, cfg, item, report, schedule) == {}


def test_layout_passes_actual_saved_facts_and_preserves_the_existing_ui_return_type(monkeypatch):
    state, cfg, _item, report, schedule = world()
    monkeypatch.setattr(layout, "status", lambda: report)
    monkeypatch.setattr(layout, "read_json", lambda path: schedule)
    monkeypatch.setattr(site_retries.time, "time", lambda: T0)
    assert layout.site_recovery_status(state, cfg)["a"]["phase"] == "scheduled"
    assert layout.site_retry_status(state, cfg) == {"a": iso(T0 + 1800)}
    monkeypatch.setattr(layout, "status", dict)
    assert layout.site_recovery_status(state, cfg) == {}
