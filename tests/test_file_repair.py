"""Durable one-shot recovery never turns observations or lost replies into repeated starts."""

from __future__ import annotations

from copy import deepcopy
from types import SimpleNamespace
from typing import ClassVar

import pytest

from tow import check, file_presence, file_repair, log, platform
from tow.check import repair
from tow.check import run as check_run
from tow.clients.files import recovery_fingerprint
from tow.errors import TowError
from tow.paths import download_history_path, state_path
from tow.progress import reconcile_topic
from tow.store import load_download_history, load_state, save_download_history, save_state

HASH = "a" * 40
CONFIG = {"clients": {"main": {"kind": "qbittorrent", "default": True}}}


class Client:
    client_id = "main"
    client_kind = "fake"
    capabilities: ClassVar = {"inspect": True, "recheck_files": True}

    def __init__(self, path):
        self.info = {
            "hash": HASH,
            "save_path": str(path),
            "tags": ["tow"],
            "state": "stoppedUP",
            "files": [
                {"index": n, "name": f"S01E{n + 1:02d}.mkv", "size": 10, "priority": 1, "progress": 1.0}
                for n in range(2)
            ],
        }
        self.calls = []
        self.fail_check = self.fail_resume = self.lose_resume = False

    def inspect_torrent(self, infohash):
        return deepcopy(self.info)

    def ping(self):
        return "fake 1.0"

    def stop_owned_torrent(self, infohash):
        self.calls.append("stop")
        self.info["state"] = "stoppedUP"
        return deepcopy(self.info)

    def recheck_owned_torrent(self, infohash, *, expected_save_path, expected_files_fingerprint):
        saved = load_state()["topics"][0]["repair_pending"]
        assert saved["phase"] == "checking"
        assert saved["recheck_dispatched"] is True
        assert expected_save_path == self.info["save_path"]
        assert expected_files_fingerprint == recovery_fingerprint(self.info["files"])
        self.calls.append("check")
        self.info["state"] = "checkingUP"
        if self.fail_check:
            raise OSError("lost file-check reply")

    def finish_check(self):
        self.info["state"] = "stoppedDL"
        self.info["files"][0]["progress"] = 0.0

    def resume_repaired_torrent(self, infohash, *, expected_save_path, expected_files_fingerprint):
        saved = load_state()["topics"][0]["repair_pending"]
        assert saved["phase"] == "resuming"
        assert saved["resume_dispatched"] is True
        assert expected_save_path == self.info["save_path"]
        assert expected_files_fingerprint == recovery_fingerprint(self.info["files"])
        self.calls.append("resume")
        if self.fail_resume:
            raise OSError("start not accepted")
        self.info["state"] = "queuedDL"  # obey the client's existing download queue
        if self.lose_resume:
            raise OSError("lost start reply")
        return deepcopy(self.info)

    def has_active_file_overlap(self, infohash, *, expected_save_path, expected_files_fingerprint):
        return False


def world(tmp_path, monkeypatch):
    clock = {"now": 1_000.0}
    monkeypatch.setattr(file_repair.time, "time", lambda: clock["now"])
    monkeypatch.setattr(file_repair, "load_config", lambda: deepcopy(CONFIG))
    monkeypatch.setattr(platform, "volume_of", lambda path: platform.Volume("uuid:repair-disk", "local"))
    monkeypatch.setattr(repair.shutil, "disk_usage", lambda path: SimpleNamespace(free=10**12))
    client = Client(tmp_path)
    monkeypatch.setattr(file_repair.factory, "from_secrets", lambda *args, **kwargs: client)
    monkeypatch.setattr(file_repair.factory, "on_this_computer", lambda *args, **kwargs: True)
    topic = {
        "id": "synthetic",
        "title": "Synthetic Show [2 of 24]",
        "url": "https://tracker.example/topic/1",
        "hash": HASH,
        "save_path": str(tmp_path),
        "client_id": "main",
        "selection": {"mode": "all"},
        "selection_hash": HASH,
        "selection_verified": True,
    }
    (tmp_path / "S01E02.mkv").write_bytes(b"0123456789")
    file_presence.observe(
        topic, {"missing_files": 1, "unverified_files": 0}, client_id="main", at="2026-09-12T20:00:00+00:00"
    )
    save_state({"topics": [topic], "mirrors": {}})
    save_download_history(
        {"topics": {"synthetic": {"items": {}, "volume": {"path": str(tmp_path), "id": "uuid:repair-disk"}}}}
    )
    return client, clock


def setup_run():
    state = load_state()
    topic = state["topics"][0]
    records = []
    return topic, state, records


def advance(client):
    topic, state, records = setup_run()
    run = SimpleNamespace(
        apply=True,
        how="progress",
        state=state,
        remote_clients=frozenset(),
        get_client_for=lambda topic: ("main", client),
        record=lambda kind, **fields: records.append((kind, fields)),
    )
    row = {}
    handled = repair.recheck(topic, run, row)
    return handled, row, records


def test_one_request_checks_then_restores_only_through_the_selected_client(tmp_path, monkeypatch):
    client, _clock = world(tmp_path, monkeypatch)
    requested = file_repair.request("synthetic")
    assert requested["phase"] == "requested"
    assert client.calls == []
    assert file_repair.request("synthetic")["id"] == requested["id"]  # double click cannot reset its budget
    assert advance(client)[1]["status"] == "repair_pending"
    assert client.calls == ["check"]
    client.finish_check()
    assert advance(client)[1]["repair_phase"] == "downloading"
    assert client.calls == ["check", "resume"]
    assert advance(client)[1]["repair_pending"] is True
    (tmp_path / "S01E01.mkv").write_bytes(b"0123456789")
    client.info["files"][0]["progress"] = 1.0
    client.info["state"] = "stoppedUP"
    assert advance(client)[1]["repair_finished"] is True
    topic = load_state()["topics"][0]
    assert "repair_pending" not in topic
    assert "files_missing" in topic  # ordinary reconcile commits history and publishes the one recovery message
    assert client.calls == ["check", "resume"]


def test_lost_check_reply_is_observed_after_restart_without_dispatching_the_check_twice(tmp_path, monkeypatch):
    client, clock = world(tmp_path, monkeypatch)
    file_repair.request("synthetic")
    client.fail_check = True
    advance(client)
    assert client.calls == ["check"]
    clock["now"] += 61
    advance(client)
    client.finish_check()
    advance(client)
    assert client.calls == ["check", "resume"]


@pytest.mark.parametrize("accepted", [False, True])
def test_lost_start_reply_never_causes_a_second_start(tmp_path, monkeypatch, accepted):
    client, clock = world(tmp_path, monkeypatch)
    file_repair.request("synthetic")
    advance(client)
    client.finish_check()
    client.lose_resume, client.fail_resume = accepted, not accepted
    advance(client)
    clock["now"] += 61
    advance(client)
    clock["now"] += 61
    advance(client)
    assert client.calls.count("resume") == 1
    receipt = load_state()["topics"][0]["repair_pending"]
    assert receipt["phase"] == ("downloading" if accepted else "blocked")


def test_an_owner_stop_after_the_authorized_start_is_never_overridden(tmp_path, monkeypatch):
    client, _clock = world(tmp_path, monkeypatch)
    file_repair.request("synthetic")
    advance(client)
    client.finish_check()
    advance(client)
    client.info["state"] = "stoppedDL"
    assert advance(client)[1]["status"] == "failed"
    assert load_state()["topics"][0]["repair_pending"]["blocked"] == "repair.client_stopped"
    advance(client)
    assert client.calls == ["check", "resume"]


@pytest.mark.parametrize(
    "change", [{"paused": True}, {"file_observation_revision": "new-endpoint"}, {"hash": "b" * 40}]
)
def test_changed_owner_intent_cancels_without_any_client_command(tmp_path, monkeypatch, change):
    client, _clock = world(tmp_path, monkeypatch)
    file_repair.request("synthetic")
    state = load_state()
    state["topics"][0].update(change)
    save_state(state)
    assert advance(client)[0] is False
    assert client.calls == []
    assert "repair_pending" not in load_state()["topics"][0]


def test_cancelling_an_inflight_request_fences_a_stale_workers_resume(tmp_path, monkeypatch):
    client, _clock = world(tmp_path, monkeypatch)
    file_repair.request("synthetic")
    advance(client)
    stale, state, _records = setup_run()
    assert file_repair.cancel("synthetic")
    client.finish_check()
    run = SimpleNamespace(
        apply=True,
        how="progress",
        state=state,
        remote_clients=frozenset(),
        get_client_for=lambda topic: ("main", client),
        record=lambda *args, **kwargs: None,
    )
    repair.recheck(stale, run, {})
    assert client.calls == ["check"]
    assert "repair_pending" not in load_state()["topics"][0]


@pytest.mark.parametrize("failure", ["space", "volume", "selection", "owner"])
def test_fresh_guards_refuse_mutation_when_the_environment_changed(tmp_path, monkeypatch, failure):
    client, _clock = world(tmp_path, monkeypatch)
    file_repair.request("synthetic")
    if failure == "space":
        monkeypatch.setattr(repair.shutil, "disk_usage", lambda path: SimpleNamespace(free=0))
    elif failure == "volume":
        monkeypatch.setattr(platform, "volume_of", lambda path: platform.Volume("uuid:other-disk", "local"))
    elif failure == "selection":
        client.info["files"][0]["priority"] = 0
    else:
        client.info["tags"] = []
    row = advance(client)[1]
    assert row["status"] == ("repair_pending" if failure in {"space", "volume"} else "failed")
    if failure in {"space", "volume"}:
        saved = load_state()["topics"][0]["repair_pending"]
        assert saved["retry_error"]["code"] == f"repair.waiting_{failure}"
    assert client.calls == []


def test_a_deadline_is_persisted_and_never_rearmed_by_a_restart(tmp_path, monkeypatch):
    client, clock = world(tmp_path, monkeypatch)
    requested = file_repair.request("synthetic")
    clock["now"] = requested["deadline"] + 1
    assert advance(client)[1]["status"] == "failed"
    assert load_state()["topics"][0]["repair_pending"]["blocked"] == "repair.deadline"
    assert client.calls == []


def test_failed_durable_dispatch_write_never_sends_a_client_command(tmp_path, monkeypatch):
    client, _clock = world(tmp_path, monkeypatch)
    file_repair.request("synthetic")

    def failed(_state):
        raise OSError("synthetic failed receipt write")

    monkeypatch.setattr(file_repair, "save_state", failed)
    with pytest.raises(OSError, match="synthetic failed receipt write"):
        advance(client)
    assert client.calls == []


@pytest.mark.parametrize(
    "change",
    [
        {"version": True},
        {"attempts": True},
        {"deadline": float("nan")},
        {"resume_authorized": 1},
        {"phase": "resuming"},
        {"resume_dispatched": "yes"},
    ],
)
def test_malformed_receipts_are_never_permission_to_mutate(tmp_path, monkeypatch, change):
    client, _clock = world(tmp_path, monkeypatch)
    receipt = file_repair.request("synthetic")
    topic = load_state()["topics"][0]
    topic["repair_pending"] = {**receipt, **change}
    assert file_repair.receipt_of(topic) == {}
    assert client.calls == []


def test_request_rejects_a_paused_topic_without_creating_authority(tmp_path, monkeypatch):
    client, _clock = world(tmp_path, monkeypatch)
    state = load_state()
    state["topics"][0]["paused"] = True
    save_state(state)
    with pytest.raises(TowError, match=r"paused|пауз"):
        file_repair.request("synthetic")
    assert "repair_pending" not in load_state()["topics"][0]
    assert client.calls == []


@pytest.mark.parametrize("scope", [{"progress_only": True}, {"space_only": True}, {}])
def test_real_check_pipeline_preserves_durable_phases_and_publishes_only_committed_recovery(
    tmp_path, monkeypatch, scope
):
    client, _clock = world(tmp_path, monkeypatch)
    monkeypatch.setattr(check_run, "load_config", lambda: deepcopy(CONFIG))
    monkeypatch.setattr(check_run, "load_trackers", lambda config: {})

    def no_tracker(*args, **kwargs):
        raise AssertionError("active file recovery must not ask a tracker")

    monkeypatch.setattr(check_run, "check_topic", no_tracker)
    topic = load_state()["topics"][0]
    history = load_download_history()
    target = tmp_path / "S01E01.mkv"
    target.write_bytes(b"0123456789")
    reconcile_topic(topic, client, history, "2026-09-12T20:00:00+00:00")
    target.unlink()
    reconcile_topic(topic, client, history, "2026-09-12T20:01:00+00:00")
    save_download_history(history)
    file_repair.request("synthetic")

    first = check.run_check(apply=True, notify=False, how="manual", ids=["synthetic"], **scope)

    assert first["results"][0]["status"] == "repair_pending"
    assert load_state()["topics"][0]["repair_pending"]["phase"] == "checking"
    assert client.calls == ["check"]
    client.finish_check()
    check.run_check(apply=True, notify=False, how="progress", progress_only=True)
    assert load_state()["topics"][0]["repair_pending"]["phase"] == "downloading"
    assert load_state()["topics"][0]["files_missing"]["count"] == 1
    assert client.calls == ["check", "resume"]
    check.run_check(apply=True, notify=False, how="progress", progress_only=True)
    assert client.calls == ["check", "resume"]
    target.write_bytes(b"0123456789")
    client.info["files"][0]["progress"] = 1.0
    original_log = check_run.log_event
    committed = []

    def after_commit(kind, **fields):
        if kind == "files_restored":
            saved = load_state()["topics"][0]
            assert "repair_pending" not in saved
            assert "files_missing" not in saved
            assert load_download_history()["topics"]["synthetic"]["summary"]["completed"] == 2
            committed.append(kind)
        return original_log(kind, **fields)

    monkeypatch.setattr(check_run, "log_event", after_commit)
    check.run_check(apply=True, notify=False, how="progress", progress_only=True)
    check.run_check(apply=True, notify=False, how="progress", progress_only=True)

    assert committed == ["files_restored"]
    assert [event["kind"] for event in log.read_events()].count("files_restored") == 1
    assert client.calls == ["check", "resume"]


def test_a_real_dry_run_never_dispatches_or_changes_recovery_history_or_logs(tmp_path, monkeypatch):
    client, _clock = world(tmp_path, monkeypatch)
    monkeypatch.setattr(check_run, "load_config", lambda: deepcopy(CONFIG))
    monkeypatch.setattr(check_run, "load_trackers", lambda config: {})
    file_repair.request("synthetic")
    state_before = state_path().read_bytes()
    history_before = download_history_path().read_bytes()
    log_before = log.log_path().read_bytes()

    result = check.run_check(apply=False, notify=True, how="manual", progress_only=True)

    assert result["preview"] is True
    assert client.calls == []
    assert state_path().read_bytes() == state_before
    assert download_history_path().read_bytes() == history_before
    assert log.log_path().read_bytes() == log_before


def test_monitoring_a_queued_recovery_does_not_reapply_command_only_space_or_overlap_guards(tmp_path, monkeypatch):
    client, _clock = world(tmp_path, monkeypatch)
    file_repair.request("synthetic")
    advance(client)
    client.finish_check()
    advance(client)
    monkeypatch.setattr(repair.shutil, "disk_usage", lambda path: SimpleNamespace(free=0))
    client.has_active_file_overlap = lambda *args, **kwargs: True

    assert advance(client)[1]["repair_phase"] == "downloading"

    assert client.calls == ["check", "resume"]
    assert file_repair.active(load_state()["topics"][0])


@pytest.mark.parametrize("running", [False, True])
def test_explicit_retry_of_already_known_zero_progress_does_not_recheck_or_repeat_resume(
    tmp_path, monkeypatch, running
):
    client, _clock = world(tmp_path, monkeypatch)
    client.info["files"][0]["progress"] = 0.0
    client.info["state"] = "queuedDL" if running else "stoppedDL"
    receipt = file_repair.request("synthetic")
    assert receipt["check_required"] is False

    advance(client)
    advance(client)

    assert client.calls == ([] if running else ["resume"])
    assert load_state()["topics"][0]["repair_pending"]["phase"] == "downloading"


def test_partial_cached_claims_still_require_rechecking_instead_of_direct_resume(tmp_path, monkeypatch):
    client, _clock = world(tmp_path, monkeypatch)
    client.info["files"][0]["progress"] = 0.5
    assert file_repair.request("synthetic")["check_required"] is True
    advance(client)
    assert client.calls == ["check"]


@pytest.mark.parametrize(
    "change", [{"phase": "blocked"}, {"phase": "blocked", "blocked": True}, {"blocked_params": "bad"}]
)
def test_malformed_terminal_diagnostics_are_not_executable_receipts(tmp_path, monkeypatch, change):
    client, _clock = world(tmp_path, monkeypatch)
    receipt = file_repair.request("synthetic")
    topic = load_state()["topics"][0]
    topic["repair_pending"] = {**receipt, **change}
    assert file_repair.receipt_of(topic) == {}
    assert client.calls == []


def test_a_real_pipeline_fails_closed_on_malformed_authority_instead_of_falling_through_to_tracker_apply(
    tmp_path, monkeypatch
):
    client, _clock = world(tmp_path, monkeypatch)
    monkeypatch.setattr(check_run, "load_config", lambda: deepcopy(CONFIG))
    monkeypatch.setattr(check_run, "load_trackers", lambda config: {})
    state = load_state()
    state["topics"][0]["repair_pending"] = {"phase": "checking", "resume_authorized": True}
    save_state(state)

    def forbidden(*args, **kwargs):
        raise AssertionError("malformed repair authority must never dispatch an ordinary tracker apply")

    monkeypatch.setattr(check_run, "check_topic", forbidden)

    result = check.run_check(apply=True, notify=False, how="manual")

    assert result["results"][0]["status"] == "failed"
    assert client.calls == []


def test_cancellation_before_dispatch_reservation_prevents_the_native_command(tmp_path, monkeypatch):
    client, _clock = world(tmp_path, monkeypatch)
    file_repair.request("synthetic")
    original = file_repair.persist

    def cancel_before(topic, receipt, *, expected_id):
        if receipt and receipt["phase"] == "checking":
            file_repair.cancel("synthetic")
        return original(topic, receipt, expected_id=expected_id)

    monkeypatch.setattr(file_repair, "persist", cancel_before)
    advance(client)

    assert client.calls == []
    assert "repair_pending" not in load_state()["topics"][0]


def test_cancellation_after_dispatch_reservation_allows_only_that_reserved_command(tmp_path, monkeypatch):
    client, _clock = world(tmp_path, monkeypatch)
    file_repair.request("synthetic")
    original = file_repair.persist
    cancelled = []

    def cancel_after(topic, receipt, *, expected_id):
        saved = original(topic, receipt, expected_id=expected_id)
        if saved and receipt and receipt["phase"] == "checking" and not cancelled:
            cancelled.append(file_repair.cancel("synthetic"))
        return saved

    def reserved_check(*args, **kwargs):
        client.calls.append("check")
        client.info["state"] = "checkingUP"

    monkeypatch.setattr(file_repair, "persist", cancel_after)
    monkeypatch.setattr(client, "recheck_owned_torrent", reserved_check)
    advance(client)
    client.finish_check()
    advance(client)

    assert cancelled == [True]
    assert client.calls == ["check"]
    assert "repair_pending" not in load_state()["topics"][0]


def test_request_cas_refuses_an_endpoint_change_during_client_inspection(tmp_path, monkeypatch):
    client, _clock = world(tmp_path, monkeypatch)
    original = client.inspect_torrent

    def changed(infohash):
        state = load_state()
        state["topics"][0]["file_observation_revision"] = "changed-during-inspection"
        save_state(state)
        return original(infohash)

    monkeypatch.setattr(client, "inspect_torrent", changed)

    with pytest.raises(TowError) as caught:
        file_repair.request("synthetic")

    assert caught.value.code == "repair.changed"
    assert client.calls == []
    assert "repair_pending" not in load_state()["topics"][0]


def test_progress_json_number_spelling_cannot_fake_a_completed_hash_check():
    assert file_repair.progress_signature({"files": [{"index": 0, "progress": 0}]}) == file_repair.progress_signature(
        {"files": [{"index": 0, "progress": -0.0}]}
    )
    assert file_repair.progress_signature({"files": [{"index": 0, "progress": 1}]}) == file_repair.progress_signature(
        {"files": [{"index": 0, "progress": 1.0}]}
    )


@pytest.mark.parametrize("phase", [[], {}, None, True, 0, "invalid-phase"])
def test_corrupt_unhashable_or_unknown_phase_is_handled_without_any_apply_fallback(tmp_path, monkeypatch, phase):
    client, _clock = world(tmp_path, monkeypatch)
    file_repair.request("synthetic")
    state = load_state()
    state["topics"][0]["repair_pending"]["phase"] = phase
    assert file_repair.receipt_of(state["topics"][0]) == {}
    save_state(state)
    monkeypatch.setattr(check_run, "load_config", lambda: deepcopy(CONFIG))
    monkeypatch.setattr(check_run, "load_trackers", lambda config: {})

    def forbidden(*args, **kwargs):
        raise AssertionError("corrupt receipt must not fall through to an applying tracker check")

    monkeypatch.setattr(check_run, "check_topic", forbidden)
    result = check.run_check(apply=True, notify=False, how="manual")

    assert result["results"][0]["status"] == "failed"
    assert client.calls == []


@pytest.mark.parametrize("state", ["uploading", "downloading", "queuedDL"])
def test_active_stale_torrent_is_stopped_before_the_actual_adapters_overlap_preflight(tmp_path, monkeypatch, state):
    client, _clock = world(tmp_path, monkeypatch)
    client.info["state"] = state
    file_repair.request("synthetic")
    overlap_calls = []

    def stopped_preflight(*args, **kwargs):
        assert client.info["state"].startswith("stopped"), "ManagedClient requires a stopped repair target"
        overlap_calls.append(client.info["state"])
        return False

    monkeypatch.setattr(client, "has_active_file_overlap", stopped_preflight)

    assert advance(client)[1]["repair_phase"] == "checking"

    assert client.calls == ["stop", "check"]
    assert overlap_calls == ["stoppedUP"]
    client.finish_check()
    assert advance(client)[1]["repair_phase"] == "downloading"
    assert client.calls == ["stop", "check", "resume"]
    assert len(overlap_calls) == 2


@pytest.mark.parametrize("state", ["error", "unknown", "allocating", "moving", "metaDL"])
def test_an_ambiguous_active_state_is_not_new_authority_to_stop_or_recheck(tmp_path, monkeypatch, state):
    client, _clock = world(tmp_path, monkeypatch)
    file_repair.request("synthetic")
    client.info["state"] = state

    assert advance(client)[1]["status"] == "failed"

    assert client.calls == []


def test_active_recovery_with_the_real_qbittorrent_adapter_stops_before_native_preflight(tmp_path, monkeypatch):
    from test_client_file_recovery import RecoveryQbit, writes
    from test_clients_managed import TORRENT, H, K, Torrent, _qbittorrent

    world(tmp_path, monkeypatch)
    server = RecoveryQbit()
    client = _qbittorrent(server, monkeypatch)
    client.client_id = "main"
    torrent = Torrent(TORRENT, str(tmp_path), ["tow"], paused=False)
    torrent.progress = 1.0
    server.torrents[K] = torrent
    native_files = server.torrents_files

    def completed_files(*args, **kwargs):
        rows = native_files(*args, **kwargs)
        for row in rows:
            row["progress"] = 1.0
        return rows

    monkeypatch.setattr(server, "torrents_files", completed_files)
    monkeypatch.setattr(file_repair.factory, "from_secrets", lambda *args, **kwargs: client)
    info = client.inspect_torrent(H)
    for row in info["files"][1:]:
        path = tmp_path / row["name"]
        assert row["size"] <= 100_000  # the fixture is synthetic, never a media payload
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"x" * row["size"])
    topic = load_state()["topics"][0]
    topic.update(hash=H, selection_hash=H)
    file_presence.observe(
        topic, {"missing_files": 1, "unverified_files": 0}, client_id="main", at="2026-09-12T20:00:00+00:00"
    )
    save_state({"topics": [topic], "mirrors": {}})
    file_repair.request("synthetic")
    server.calls.clear()

    handled, row, _records = advance(client)

    assert handled is True
    assert row["repair_phase"] == "checking"
    assert writes(server) == ["torrents_stop", "torrents_recheck"]


def test_fresh_completion_settles_a_blocked_receipt_without_a_spurious_cancel_message(tmp_path, monkeypatch):
    client, _clock = world(tmp_path, monkeypatch)
    file_repair.request("synthetic")
    advance(client)
    client.finish_check()
    advance(client)
    client.info["state"] = "stoppedDL"
    advance(client)
    assert load_state()["topics"][0]["repair_pending"]["phase"] == "blocked"
    (tmp_path / "S01E01.mkv").write_bytes(b"0123456789")
    client.info["files"][0]["progress"] = 1.0
    client.info["state"] = "stoppedUP"
    monkeypatch.setattr(check_run, "load_config", lambda: deepcopy(CONFIG))
    monkeypatch.setattr(check_run, "load_trackers", lambda config: {})

    check.run_check(apply=True, notify=False, how="progress", progress_only=True)
    check.run_check(apply=True, notify=False, how="progress", progress_only=True)

    current = load_state()["topics"][0]
    assert "repair_pending" not in current
    assert "files_missing" not in current
    assert load_download_history()["topics"]["synthetic"]["summary"]["completed"] == 2
    kinds = [event["kind"] for event in log.read_events()]
    assert kinds.count("files_restored") == 1
    assert "files_repair_cancelled" not in kinds
    assert client.calls == ["check", "resume"]
