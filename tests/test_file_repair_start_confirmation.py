"""An asynchronous native start is observed once without resending its command."""

from __future__ import annotations

from copy import deepcopy
from types import SimpleNamespace

import pytest
from test_file_repair import CONFIG, advance, world

from tow import check, file_presence, file_repair, log, platform
from tow.check import repair
from tow.check import run as check_run
from tow.errors import TowError
from tow.store import load_download_history, load_state, save_state
from tow.supervisor import _facts


def delayed_start(tmp_path, monkeypatch, *, legacy=False):
    client, clock = world(tmp_path, monkeypatch)
    client.info["files"][0]["progress"] = 0.0
    original = client.resume_repaired_torrent

    def start_once(*args, **kwargs):
        original(*args, **kwargs)
        client.info["state"] = "stoppedDL"  # native command accepted, status snapshot not advanced yet
        raise TowError("client.managed.repair_start_unconfirmed")

    monkeypatch.setattr(client, "resume_repaired_torrent", start_once)
    requested = file_repair.request("synthetic")
    row = advance(client)[1]
    assert row["status"] == "repair_pending"
    assert load_state()["topics"][0]["repair_pending"]["phase"] == "resuming"
    if legacy:
        state = load_state()
        receipt = state["topics"][0]["repair_pending"]
        receipt.update(phase="blocked", blocked="client.managed.repair_start_unconfirmed", blocked_params={})
        save_state(state)
    return client, clock, requested


@pytest.mark.parametrize("state", ["downloading", "queuedDL", "stalledDL"])
def test_late_positive_readback_confirms_exactly_one_start(tmp_path, monkeypatch, state):
    client, clock, requested = delayed_start(tmp_path, monkeypatch)
    clock["now"] += 30
    assert advance(client)[1]["repair_phase"] == "resuming"
    client.info["state"] = state
    client.info["files"][0]["progress"] = 0.4
    records = advance(client)[2]
    current = load_state()["topics"][0]["repair_pending"]
    assert current["phase"] == "downloading"
    assert current["id"] == requested["id"]
    assert [kind for kind, _fields in records] == ["files_repair_resumed"]
    assert advance(client)[2] == []
    assert client.calls == ["resume"]
    client.info["state"] = "stoppedDL"
    assert advance(client)[1]["status"] == "failed"
    assert load_state()["topics"][0]["repair_pending"]["blocked"] == "repair.client_stopped"
    assert client.calls == ["resume"]


@pytest.mark.parametrize("state", ["stoppedUP", "stoppedDL", "checkingDL", "checkingResumeData", "allocating"])
def test_start_confirmation_deadline_is_not_extended_by_stale_or_transitional_state(tmp_path, monkeypatch, state):
    client, clock, _requested = delayed_start(tmp_path, monkeypatch)
    client.info["state"] = state
    for row in client.info["files"]:
        row["progress"] = 1.0  # stopped + stale 100% cannot stand in for physical missing files
    clock["now"] += repair.CONFIRM_SEC - 1
    assert advance(client)[1]["repair_phase"] == "resuming"
    clock["now"] += 1
    assert advance(client)[1]["status"] == "failed"
    assert load_state()["topics"][0]["repair_pending"]["blocked"] == "repair.resume_unconfirmed"
    assert client.calls == ["resume"]


@pytest.mark.parametrize("state", ["error", "missingFiles", "unknown", "moving"])
def test_real_error_after_dispatch_is_not_a_pending_or_successful_start(tmp_path, monkeypatch, state):
    client, _clock, _requested = delayed_start(tmp_path, monkeypatch)
    client.info.update(state=state, stopped=True)
    assert advance(client)[1]["status"] == "failed"
    expected = (
        "client.managed.error_state"
        if state in {"error", "missingFiles"}
        else "repair.client_busy"
        if state == "moving"
        else "client.managed.start_unconfirmed"
    )
    assert load_state()["topics"][0]["repair_pending"]["blocked"] == expected
    assert client.calls == ["resume"]


@pytest.mark.parametrize("code", ["client.managed.repair_changed", "client.managed.not_owned", "repair.overlap"])
def test_only_the_specific_after_dispatch_error_is_deferred(tmp_path, monkeypatch, code):
    client, _clock = world(tmp_path, monkeypatch)
    client.info["files"][0]["progress"] = 0.0

    def refused(*args, **kwargs):
        client.calls.append("resume")
        raise TowError(code)

    monkeypatch.setattr(client, "resume_repaired_torrent", refused)
    file_repair.request("synthetic")
    assert advance(client)[1]["status"] == "failed"
    assert load_state()["topics"][0]["repair_pending"]["blocked"] == code
    assert client.calls == ["resume"]


@pytest.mark.parametrize("state", ["downloading", "queuedDL"])
def test_legacy_false_failure_recovers_by_fresh_readback_without_new_authority_or_commands(
    tmp_path, monkeypatch, state
):
    client, clock, _requested = delayed_start(tmp_path, monkeypatch, legacy=True)
    previous = load_state()["topics"][0]["repair_pending"]
    clock["now"] += 600  # beyond the start-confirmation window, within the original overall deadline
    client.info["state"] = state
    client.info["files"][0]["progress"] = 0.4
    topic = load_state()["topics"][0]
    assert file_repair.active(topic) is False
    assert file_repair.start_confirmation_pending(topic) is True
    assert [t["id"] for t in repair.waiting_topics(load_state())] == ["synthetic"]
    assert _facts()["space_waiting"] is True

    handled, row, records = advance(client)

    assert handled is True
    assert row["repair_phase"] == "downloading"
    current = load_state()["topics"][0]["repair_pending"]
    assert not current.get("blocked")
    for key in ("id", "binding", "files", "requested_at", "resume_at", "deadline", "attempts"):
        assert current[key] == previous[key]
    assert [kind for kind, _fields in records] == ["files_repair_resumed"]
    assert client.calls == ["resume"]
    client.info["state"] = "stoppedDL"
    assert advance(client)[1]["status"] == "failed"
    assert load_state()["topics"][0]["repair_pending"]["blocked"] == "repair.client_stopped"
    assert repair.waiting_topics(load_state()) == []
    assert client.calls == ["resume"]


def test_legacy_stopped_cached_complete_is_not_recovered_and_scheduler_stops_at_its_original_deadline(
    tmp_path, monkeypatch
):
    client, clock, _requested = delayed_start(tmp_path, monkeypatch, legacy=True)
    for row in client.info["files"]:
        row["progress"] = 1.0
    original = deepcopy(load_state())
    clock["now"] += 600
    assert advance(client)[0] is False  # ordinary tracker watching remains available for this blocked receipt
    assert load_state() == original
    assert _facts()["space_waiting"] is True
    clock["now"] = original["topics"][0]["repair_pending"]["deadline"]
    assert file_repair.start_confirmation_pending(load_state()["topics"][0]) is False
    assert repair.waiting_topics(load_state()) == []
    assert _facts()["space_waiting"] is False
    assert advance(client)[0] is False
    assert client.calls == ["resume"]


@pytest.mark.parametrize(
    "change",
    [
        {"blocked": "repair.client_stopped"},
        {"resume_dispatched": False},
        {"running_seen": True},
        {"resume_at": None},
        {"resume_at": 99_999},
    ],
)
def test_legacy_observation_does_not_accept_another_failure_or_unproven_dispatch(tmp_path, monkeypatch, change):
    client, _clock, _requested = delayed_start(tmp_path, monkeypatch, legacy=True)
    state = load_state()
    state["topics"][0]["repair_pending"].update(change)
    save_state(state)
    client.info["state"] = "downloading"
    assert not file_repair.start_confirmation_pending(state["topics"][0])
    assert repair.waiting_topics(state) == []
    assert _facts()["space_waiting"] is False
    assert advance(client)[2] == []
    assert load_state() == state
    assert client.calls == ["resume"]


@pytest.mark.parametrize("change", [{"paused": True}, {"selection_dirty": True}, {"file_observation_revision": "new"}])
def test_legacy_context_change_fences_the_old_start_and_scheduler(tmp_path, monkeypatch, change):
    client, _clock, _requested = delayed_start(tmp_path, monkeypatch, legacy=True)
    state = load_state()
    state["topics"][0].update(change)
    save_state(state)
    client.info["state"] = "downloading"
    assert repair.waiting_topics(state) == []
    assert _facts()["space_waiting"] is False
    assert advance(client)[0] is False
    assert client.calls == ["resume"]


def test_legacy_observation_preview_does_not_probe_or_rewrite(tmp_path, monkeypatch):
    client, _clock, _requested = delayed_start(tmp_path, monkeypatch, legacy=True)
    state = load_state()
    topic = state["topics"][0]
    original = deepcopy(state)

    def forbidden(*args, **kwargs):
        pytest.fail("a preview must not probe or mutate a legacy confirmation")

    monkeypatch.setattr(client, "inspect_torrent", forbidden)
    run = SimpleNamespace(apply=False, state=state, get_client_for=forbidden)
    assert repair.recheck(topic, run, {}) is False
    assert load_state() == original
    assert client.calls == ["resume"]


@pytest.mark.parametrize("change", [{"tags": []}, {"save_path": "/other"}, {"files": []}])
def test_legacy_fresh_running_does_not_clear_changed_client_evidence(tmp_path, monkeypatch, change):
    client, _clock, _requested = delayed_start(tmp_path, monkeypatch, legacy=True)
    client.info.update(state="downloading", **change)
    assert advance(client)[1]["status"] == "failed"
    assert load_state()["topics"][0]["repair_pending"]["phase"] == "blocked"
    assert client.calls == ["resume"]


def test_legacy_completion_settles_through_normal_reconciliation_and_one_postcommit_event(tmp_path, monkeypatch):
    client, _clock, _requested = delayed_start(tmp_path, monkeypatch, legacy=True)
    (tmp_path / "S01E01.mkv").write_bytes(b"0123456789")
    client.info["files"][0]["progress"] = 1.0
    client.info["state"] = "stoppedUP"
    monkeypatch.setattr(check_run, "load_config", lambda: deepcopy(CONFIG))
    monkeypatch.setattr(check_run, "load_trackers", lambda config: {})
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
    kinds = [event["kind"] for event in log.read_events()]
    assert kinds.count("files_restored") == 1
    assert "files_repair_cancelled" not in kinds
    assert "files_repair_resumed" not in kinds
    assert client.calls == ["resume"]


def test_qbittorrent_adapter_delayed_start_readback_is_followed_without_another_start(tmp_path, monkeypatch):
    from test_client_file_recovery import RecoveryQbit, writes
    from test_clients_managed import TORRENT, H, K, Torrent, _qbittorrent

    world(tmp_path, monkeypatch)
    server = RecoveryQbit()
    client = _qbittorrent(server, monkeypatch)
    client.client_id = "main"
    torrent = Torrent(TORRENT, str(tmp_path), ["tow"], paused=True)
    torrent.progress = 0.4
    server.torrents[K] = torrent
    state = load_state()
    topic = state["topics"][0]
    topic.update(hash=H, selection_hash=H)
    topic.pop("files_missing", None)
    file_presence.observe(
        topic, {"missing_files": 1, "unverified_files": 0}, client_id="main", at="2026-09-12T20:00:00+00:00"
    )
    save_state(state)
    monkeypatch.setattr(file_repair.factory, "from_secrets", lambda *args, **kwargs: client)
    native_start = server.torrents_start

    def accepted_but_not_observable_yet(**kwargs):
        native_start(**kwargs)
        torrent.running = False
        torrent.state = "stoppedDL"

    monkeypatch.setattr(server, "torrents_start", accepted_but_not_observable_yet)
    file_repair.request("synthetic")
    server.calls.clear()
    assert advance(client)[1]["repair_phase"] == "resuming"
    assert writes(server) == ["torrents_start"]
    torrent.running, torrent.state = True, "downloading"
    assert advance(client)[1]["repair_phase"] == "downloading"
    assert advance(client)[2] == []
    assert writes(server) == ["torrents_start"]


def test_legacy_cancellation_during_fresh_readback_fences_confirmation_without_resurrection(tmp_path, monkeypatch):
    client, _clock, _requested = delayed_start(tmp_path, monkeypatch, legacy=True)
    client.info["state"] = "downloading"
    inspect = client.inspect_torrent

    def cancelled(h):
        info = inspect(h)
        assert file_repair.cancel("synthetic")
        return info

    monkeypatch.setattr(client, "inspect_torrent", cancelled)
    assert advance(client)[2] == []
    assert "repair_pending" not in load_state()["topics"][0]
    assert client.calls == ["resume"]


@pytest.mark.parametrize("remote", [False, True])
def test_legacy_fresh_running_cannot_bypass_volume_or_remote_path_uncertainty(tmp_path, monkeypatch, remote):
    client, _clock, _requested = delayed_start(tmp_path, monkeypatch, legacy=True)
    client.info["state"] = "downloading"
    if not remote:
        monkeypatch.setattr(platform, "volume_of", lambda path: platform.Volume("uuid:other-disk", "local"))
    state = load_state()
    records = []
    run = SimpleNamespace(
        apply=True,
        how="progress",
        state=state,
        remote_clients=frozenset({"main"}) if remote else frozenset(),
        get_client_for=lambda topic: ("main", client),
        record=lambda kind, **fields: records.append(kind),
    )
    repair.recheck(state["topics"][0], run, {})
    assert load_state()["topics"][0]["repair_pending"]["blocked"] == "repair.volume_unavailable"
    assert "files_repair_resumed" not in records
    assert client.calls == ["resume"]
