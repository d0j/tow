"""A broken handoff receipt journal is visible without inventing a failed native add."""

from __future__ import annotations

import json

import pytest
from test_check_pipeline import OLD, Client, Tracker, _topic, _wire

from tow import check, client_only_monitor, log, messages_feed
from tow.check.run import record_check_failure
from tow.errors import TowError
from tow.issues.kinds.client_only_monitor import collect
from tow.issues.model import Snapshot
from tow.store import load_state, save_state, state_path


def marker():
    return load_state()["health"]["client_only_monitor"]


def events():
    return [row for row in log.read_events() if row["kind"].startswith("client_only_monitor_")]


def issues():
    return collect(Snapshot(load_state(), {}, 100))


def test_healthy_monitor_without_a_failure_does_not_touch_state_or_log(monkeypatch):
    save_state({"topics": [], "health": {"qbit_ok": False, "check_ok": False}})
    before = state_path().read_bytes()
    monkeypatch.setattr(client_only_monitor, "save_state", lambda _state: pytest.fail("healthy state write"))
    monkeypatch.setattr(log, "append_event", lambda *_args, **_kwargs: pytest.fail("healthy event"))
    assert client_only_monitor.observe(None) is False
    assert state_path().read_bytes() == before
    assert issues() == []


def test_error_and_recovery_are_durable_shared_messages_and_current_attention_clears():
    save_state({"topics": [], "health": {"qbit_ok": False}, "other": "kept"})
    assert client_only_monitor.observe(TowError("client_only.store_invalid")) is True
    assert marker()["ok"] is False
    assert marker()["pending"] == []
    found = issues()
    assert len(found) == 1
    assert found[0].requires_attention
    assert found[0].severity == "bad"
    assert found[0].target.filter == (("group", "errors"),)
    assert client_only_monitor.observe(TowError("client_only.store_invalid")) is False
    rendered = messages_feed.render(messages_feed.feed().refresh().projection, lang="en")["items"]
    assert rendered[0]["kind"] == "err"
    assert rendered[0]["href"] == "/history?group=errors"
    assert "monitoring unavailable" in rendered[0]["text"]
    assert client_only_monitor.observe(None) is True
    assert marker()["ok"] is True
    assert issues() == []
    assert load_state()["health"]["qbit_ok"] is False
    assert load_state()["other"] == "kept"
    assert client_only_monitor.observe(None) is False
    assert [row["kind"] for row in events()] == ["client_only_monitor_recovered", "client_only_monitor_failed"]
    assert not any(row["kind"] in {"client_add_failed", "client_only_result"} for row in log.read_events())


@pytest.mark.parametrize("lost_ack", [False, True])
def test_failed_log_append_retries_the_same_operation_without_a_duplicate(monkeypatch, lost_ack):
    save_state({"topics": []})
    append = log.append_event
    fail = {"yes": True}

    def flaky(kind, **fields):
        if fail["yes"]:
            if lost_ack:
                append(kind, **fields)
            return None
        return append(kind, **fields)

    monkeypatch.setattr(log, "append_event", flaky)
    client_only_monitor.observe(OSError("token=secret path=/private/content"))
    first_id = marker()["operation_id"]
    assert marker()["pending"]  # the next observation must reconcile the lost acknowledgement
    assert "secret" not in json.dumps(load_state())
    fail["yes"] = False
    client_only_monitor.observe(OSError("another raw error"))
    assert marker()["operation_id"] == first_id
    assert marker()["pending"] == []
    assert len(events()) == 1
    assert events()[0]["operation_id"] == first_id


def test_log_unavailable_across_error_recovery_and_another_error_keeps_a_bounded_pending_epoch(monkeypatch):
    save_state({"topics": []})
    append = log.append_event
    monkeypatch.setattr(log, "append_event", lambda *_args, **_kwargs: None)
    client_only_monitor.observe(OSError())
    first_id = marker()["operation_id"]
    for _ in range(40):
        client_only_monitor.observe(None)
        assert marker()["ok"] is True
        assert issues() == []
        assert len(marker()["pending"]) <= 2
        client_only_monitor.observe(OSError())
        assert marker()["ok"] is False
        assert issues()
        assert marker()["operation_id"] == first_id
    monkeypatch.setattr(log, "append_event", append)
    client_only_monitor.observe(None)
    assert marker()["pending"] == []
    assert issues() == []
    assert len(events()) == 2


def test_a_fresh_failure_after_published_recovery_has_its_own_event_generation():
    save_state({"topics": []})
    client_only_monitor.observe(OSError())
    first_id = marker()["operation_id"]
    client_only_monitor.observe(None)
    client_only_monitor.observe(OSError())
    assert marker()["operation_id"] != first_id
    assert len(events()) == 3
    assert len(issues()) == 1


@pytest.mark.parametrize("recover", [False, True])
def test_check_commit_preserves_monitor_changes_made_after_the_check_started(monkeypatch, recover):
    save_state({"topics": [_topic(hash=OLD)], "health": {"qbit_ok": True}})
    if recover:
        client_only_monitor.observe(OSError())

    def during_fetch():
        client_only_monitor.observe(None if recover else OSError())

    client = Client()
    _wire(monkeypatch, {"main": client}, Tracker(during_fetch=during_fetch))
    check.run_check(apply=True, notify=False, how="test")
    assert marker()["ok"] is recover
    assert bool(issues()) is not recover


def test_record_check_failure_preserves_the_current_monitor_marker():
    save_state({"topics": [], "health": {"qbit_ok": True}})
    client_only_monitor.observe(OSError())
    before = marker()
    record_check_failure(TowError("check.no_tracker"))
    assert marker() == before
    assert load_state()["health"]["qbit_ok"] is True


def test_state_write_failure_propagates_without_a_native_result_or_log(monkeypatch):
    save_state({"topics": []})
    before = state_path().read_bytes()
    monkeypatch.setattr(client_only_monitor, "save_state", lambda _state: (_ for _ in ()).throw(OSError("unwritable")))
    with pytest.raises(OSError, match="unwritable"):
        client_only_monitor.observe(OSError())
    assert state_path().read_bytes() == before
    assert events() == []


def test_crash_after_log_append_before_marker_ack_is_deduplicated_on_restart(monkeypatch):
    save_state({"topics": []})
    save = client_only_monitor.save_state
    calls = []

    def fail_ack(state):
        calls.append(state)
        if len(calls) == 2:
            raise OSError("ack write failed")
        save(state)

    monkeypatch.setattr(client_only_monitor, "save_state", fail_ack)
    with pytest.raises(OSError, match="ack write failed"):
        client_only_monitor.observe(OSError())
    assert len(events()) == 1
    assert marker()["pending"]
    monkeypatch.setattr(client_only_monitor, "save_state", save)
    client_only_monitor.observe(OSError())
    assert len(events()) == 1
    assert marker()["pending"] == []


@pytest.mark.parametrize("malformed", [{"pending": [None]}, {"pending": None}, {"error": []}, {"pending": ["x"]}])
@pytest.mark.parametrize("recovered", [False, True])
def test_invalid_optional_diagnostic_shape_cannot_poison_error_or_recovery(malformed, recovered):
    save_state({"topics": [], "health": {"client_only_monitor": {"ok": False, **malformed}}})
    client_only_monitor.observe(None if recovered else OSError())
    assert marker()["ok"] is recovered
    assert bool(issues()) is not recovered
    assert isinstance(marker()["pending"], list)
