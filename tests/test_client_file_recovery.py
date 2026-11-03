"""Native repair requests preserve identity, selection, pause and queue policy."""

from __future__ import annotations

import json
from copy import deepcopy
from typing import Any

import httpx
import pytest
from helpers import raises_code
from test_clients_managed import (
    TORRENT,
    FakeDeluge,
    FakeQbit,
    FakeTransmission,
    H,
    K,
    Torrent,
    _deluge,
    _qbittorrent,
    _transmission,
)

from tow.clients import files
from tow.clients.managed import ManagedClient

FOLDER = "/downloads/synthetic"


class RecoveryQbit(FakeQbit):
    def __init__(self):
        super().__init__()
        self.force_start: Any = False

    def _row(self, torrent):
        row = super()._row(torrent)
        row.update(state=torrent.state or row["state"], progress=torrent.progress, force_start=self.force_start)
        return row

    def torrents_info(self, torrent_hashes=None, limit=None, status_filter=None):
        rows = super().torrents_info(torrent_hashes=torrent_hashes, limit=limit)
        if status_filter is None:
            return rows
        return [row for row in rows if not self.torrents[row.hash].running]

    def torrents_recheck(self, *, torrent_hashes):
        self.calls.append("torrents_recheck")
        assert torrent_hashes == K
        self.torrents[K].state = "checkingDL"


class RecoveryTransmission(FakeTransmission):
    def handler(self, request):
        body = json.loads(request.content)
        if body["method"] == "torrent-verify" and request.headers.get("X-Transmission-Session-Id") == self.SESSION:
            assert body["arguments"] == {"ids": [K]}
            self.calls.append("torrent-verify")
            if getattr(self, "refuse_verify", False):
                return httpx.Response(200, json={"result": "verification refused"})
            self.torrents[K].state = "checkingDL"
            return httpx.Response(200, json={"result": "success", "arguments": {}})
        return super().handler(request)

    @staticmethod
    def _row(torrent):
        row = FakeTransmission._row(torrent)
        row["percentDone"] = torrent.progress
        if torrent.state:
            row["status"] = {"stoppedDL": 0, "checkingDL": 2, "queuedDL": 3}[torrent.state]
        return row


class RecoveryDeluge(FakeDeluge):
    def m_core_get_torrents_status(self, filters, keys):
        assert filters == {}
        return {identity: self.m_core_get_torrent_status(identity, keys) for identity in self.torrents}

    def m_core_force_recheck(self, ids):
        assert ids == [K]
        self.torrents[K].state = "Checking"


@pytest.fixture(params=["qbittorrent", "transmission", "deluge"])
def adapter(request, monkeypatch):
    if request.param == "qbittorrent":
        server = RecoveryQbit()
        client = _qbittorrent(server, monkeypatch)
        command = "torrents_recheck"
    elif request.param == "transmission":
        server = RecoveryTransmission()
        client = _transmission(server)
        command = "torrent-verify"
    else:
        server = RecoveryDeluge()
        client = _deluge(server)
        command = "core.force_recheck"
    torrent = Torrent(TORRENT, FOLDER, ["tow"], paused=True)
    torrent.progress = 0.4
    torrent.wanted[-1] = False
    server.torrents[K] = torrent
    fingerprint = files.recovery_fingerprint(client.inspect_torrent(H)["files"])
    server.calls.clear()
    return client, server, torrent, fingerprint, command


def perform(client, fingerprint, *, resume=False):
    method = client.resume_repaired_torrent if resume else client.recheck_owned_torrent
    return method(H, expected_save_path=FOLDER, expected_files_fingerprint=fingerprint)


def writes(server):
    return [
        call
        for call in server.calls
        if call
        in {
            "torrents_recheck",
            "torrent-verify",
            "core.force_recheck",
            "torrents_start",
            "torrents_stop",
            "torrent-start",
            "torrent-stop",
            "torrent-set",
            "core.resume_torrents",
            "core.pause_torrents",
            "core.set_torrent_options",
            "torrents_file_priority",
            "core.move_storage",
            "torrent-set-location",
            "torrents_set_location",
            "label.set_torrent",
            "torrents_add_tags",
            "torrents_remove_tags",
        }
    ]


def test_native_recheck_targets_only_owned_exact_torrent_and_preserves_selection(adapter):
    client, server, torrent, fingerprint, command = adapter
    before = deepcopy((torrent.wanted, torrent.labels, torrent.path))

    assert perform(client, fingerprint) is None

    assert writes(server) == [command]
    assert (torrent.wanted, torrent.labels, torrent.path) == before
    assert torrent.running is False
    assert client.inspect_torrent(H)["state"].startswith("checking")


@pytest.mark.parametrize("resume", [False, True])
@pytest.mark.parametrize(
    ("change", "error"),
    [
        ({"hash": "B" * 40, "infohash_v1": "", "infohash_v2": ""}, "client.managed.repair_changed"),
        ({"tags": ["manual"]}, "client.managed.not_owned"),
        ({"tags": ["tow", "tow-pending"]}, "client.managed.repair_changed"),
        ({"save_path": "/downloads/other"}, "client.managed.wrong_folder"),
        ({"files": []}, "client.managed.repair_changed"),
        ({"files": None}, "client.managed.repair_changed"),
        ({"state": "moving"}, "client.managed.recheck_requires_stopped"),
        ({"state": "checkingDL"}, "client.managed.recheck_requires_stopped"),
        ({"state": "queuedDL"}, "client.managed.recheck_requires_stopped"),
        ({"state": "downloading"}, "client.managed.recheck_requires_stopped"),
        ({"state": "error", "stopped": True}, "client.managed.recheck_requires_stopped"),
        ({"state": "missingFiles", "stopped": True}, "client.managed.recheck_requires_stopped"),
        ({"state": "unknown"}, "client.managed.recheck_requires_stopped"),
        ({"state": "stoppedGarbage"}, "client.managed.recheck_requires_stopped"),
    ],
)
def test_native_repair_refuses_changed_or_unsafe_evidence_without_any_write(
    adapter, monkeypatch, resume, change, error
):
    client, server, _torrent, fingerprint, _command = adapter
    inspect = client.inspect_torrent
    monkeypatch.setattr(client, "inspect_torrent", lambda h: {**inspect(h), **change})

    with raises_code(error):
        perform(client, fingerprint, resume=resume)

    assert writes(server) == []


@pytest.mark.parametrize("resume", [False, True])
def test_native_repair_refuses_disappeared_task(adapter, resume):
    client, server, _torrent, fingerprint, _command = adapter
    server.torrents.clear()

    with raises_code("client.managed.missing"):
        perform(client, fingerprint, resume=resume)

    assert writes(server) == []


@pytest.mark.parametrize("resume", [False, True])
@pytest.mark.parametrize("disabled", ["read_only", "capability"])
def test_preview_or_unsupported_adapter_never_dispatches_repair(adapter, disabled, resume):
    client, server, _torrent, fingerprint, _command = adapter
    if disabled == "read_only":
        client.read_only = True
    else:
        client.capabilities = {**client.capabilities, "recheck_files": False}

    with raises_code("client.managed.recheck_unavailable"):
        perform(client, fingerprint, resume=resume)

    assert server.calls == []


@pytest.mark.parametrize("field", ["index", "name", "size", "priority"])
def test_changed_native_selection_cannot_be_rechecked_or_started(adapter, monkeypatch, field):
    client, server, _torrent, fingerprint, _command = adapter
    inspect = client.inspect_torrent

    def changed(h):
        info = inspect(h)
        info["files"][0][field] = {"index": 99, "name": "Show/renamed.mkv", "size": 1, "priority": 0}[field]
        return info

    monkeypatch.setattr(client, "inspect_torrent", changed)
    for resume in (False, True):
        with raises_code("client.managed.repair_changed"):
            perform(client, fingerprint, resume=resume)
    assert writes(server) == []


def test_resume_uses_one_ordinary_start_and_keeps_selection(adapter):
    client, server, torrent, fingerprint, _command = adapter
    before = deepcopy((torrent.wanted, torrent.labels, torrent.path))

    result = perform(client, fingerprint, resume=True)

    assert result["state"] == "downloading"
    assert len(writes(server)) == 1
    assert (torrent.wanted, torrent.labels, torrent.path) == before


def test_resume_accepts_queue_without_force_start(adapter, monkeypatch):
    client, server, torrent, fingerprint, _command = adapter
    start = client._start

    def queued(h):
        start(h)
        torrent.state = "Queued" if isinstance(server, RecoveryDeluge) else "queuedDL"

    monkeypatch.setattr(client, "_start", queued)

    assert perform(client, fingerprint, resume=True)["state"] == "queuedDL"
    assert len(writes(server)) == 1


@pytest.mark.parametrize("transition", ["progress", "queue_to_downloading"])
def test_qbit_start_readback_uses_fresh_advancing_control_state(monkeypatch, transition):
    server = RecoveryQbit()
    client = _qbittorrent(server, monkeypatch)
    torrent = Torrent(TORRENT, FOLDER, ["tow"], paused=True)
    torrent.progress = 0.4
    server.torrents[K] = torrent
    fingerprint = files.recovery_fingerprint(client.inspect_torrent(H)["files"])
    read_files, start = server.torrents_files, server.torrents_start

    def started(**kwargs):
        start(**kwargs)
        if transition == "queue_to_downloading":
            torrent.state = "queuedDL"

    def files_then_progress(**kwargs):
        result = read_files(**kwargs)
        if torrent.running:
            torrent.progress = 0.6
            torrent.state = "downloading"
        return result

    monkeypatch.setattr(server, "torrents_start", started)
    monkeypatch.setattr(server, "torrents_files", files_then_progress)
    server.calls.clear()

    result = perform(client, fingerprint, resume=True)

    assert result["state"] == "downloading"
    assert result["progress"] == 0.6
    assert writes(server) == ["torrents_start"]


@pytest.mark.parametrize("latest", ["stoppedDL", "error", "unknown"])
def test_qbit_latest_stop_or_error_during_start_readback_is_never_stale_success(monkeypatch, latest):
    server = RecoveryQbit()
    client = _qbittorrent(server, monkeypatch)
    torrent = Torrent(TORRENT, FOLDER, ["tow"], paused=True)
    torrent.progress = 0.4
    server.torrents[K] = torrent
    fingerprint = files.recovery_fingerprint(client.inspect_torrent(H)["files"])
    read_files = server.torrents_files

    def files_then_stopped(**kwargs):
        result = read_files(**kwargs)
        if torrent.running:
            torrent.running = False
            torrent.state = latest
            torrent.progress = 1.0
        return result

    monkeypatch.setattr(server, "torrents_files", files_then_stopped)
    server.calls.clear()

    with raises_code("client.managed.repair_start_unconfirmed"):
        perform(client, fingerprint, resume=True)

    assert writes(server) == ["torrents_start"]


def test_resume_never_accepts_stopped_plus_cached_one_hundred_percent(adapter, monkeypatch):
    client, server, torrent, fingerprint, _command = adapter
    inspect = client.inspect_torrent

    def stale_complete(h):
        info = inspect(h)
        if torrent.running:
            info.update(state="stoppedUP", progress=1.0)
        return info

    monkeypatch.setattr(client, "inspect_torrent", stale_complete)
    # This test is about read-back; qBit's separate fresh control evidence also refuses it.
    monkeypatch.setattr(client, "_repair_options", lambda *_args, **_kwargs: None)

    with raises_code("client.managed.repair_start_unconfirmed"):
        perform(client, fingerprint, resume=True)

    assert len(writes(server)) == 1


@pytest.mark.parametrize("value", [1.0, None, True, "0.4", float("nan"), float("inf"), -0.1])
def test_resume_requires_fresh_incomplete_progress(adapter, monkeypatch, value):
    client, server, _torrent, fingerprint, _command = adapter
    inspect = client.inspect_torrent
    monkeypatch.setattr(client, "inspect_torrent", lambda h: {**inspect(h), "progress": value})
    monkeypatch.setattr(client, "_repair_options", lambda *_args, **_kwargs: None)

    with raises_code("client.managed.repair_changed"):
        perform(client, fingerprint, resume=True)

    assert writes(server) == []


@pytest.mark.parametrize("phase", ["recheck", "resume"])
def test_lost_native_response_is_never_retried(adapter, monkeypatch, phase):
    client, _server, _torrent, fingerprint, _command = adapter
    dispatched = []

    def lost(h):
        dispatched.append(h)
        raise ConnectionError("response lost after acceptance")

    monkeypatch.setattr(client, "_recheck" if phase == "recheck" else "_start", lost)
    with pytest.raises(ConnectionError, match="response lost"):
        perform(client, fingerprint, resume=phase == "resume")
    assert dispatched == [H]


def test_base_adapter_does_not_advertise_a_missing_native_primitive():
    assert ManagedClient.capabilities["recheck_files"] is False


def test_a_missing_qbit_native_check_method_refuses_without_other_mutation(monkeypatch):
    server = RecoveryQbit()
    server.torrents_recheck = None
    client = _qbittorrent(server, monkeypatch)
    with raises_code("client.managed.recheck_unavailable"):
        client._recheck(H)
    assert writes(server) == []


def test_a_negative_native_recheck_reply_is_not_accepted_as_success(adapter, monkeypatch):
    client, server, _torrent, fingerprint, command = adapter
    if isinstance(server, RecoveryQbit):

        def refused(**kwargs):
            server.calls.append(command)
            return False

        monkeypatch.setattr(server, "torrents_recheck", refused)
        error = "client.qbittorrent.api_malformed"
    elif isinstance(server, RecoveryDeluge):
        monkeypatch.setattr(server, "m_core_force_recheck", lambda *_args: False)
        error = "client.managed.recheck_unavailable"
    else:
        server.refuse_verify = True
        error = "client.managed.rpc_refused"
    with raises_code(error):
        perform(client, fingerprint)
    assert writes(server) == [command]


@pytest.mark.parametrize("layout", ["same", "parent", "nested"])
def test_all_client_tasks_block_shared_file_writers_even_without_tow_mark(adapter, layout):
    client, server, torrent, fingerprint, _command = adapter
    other = Torrent(TORRENT, FOLDER, ["manual"], paused=False)
    other.hash = "b" * 40
    name, size = torrent.files[0]
    if layout == "parent":
        other.path = "/downloads"
        name = "synthetic/" + name
    elif layout == "nested":
        folder, name = name.split("/", 1)
        other.path = FOLDER + "/" + folder
    other.files = [(name, size)]
    other.wanted = [True]
    server.torrents[other.hash] = other

    assert client.has_active_file_overlap(H, expected_save_path=FOLDER, expected_files_fingerprint=fingerprint) is True
    assert writes(server) == []


@pytest.mark.parametrize("difference", ["folder", "file", "stopped", "skipped"])
def test_different_paths_and_explicit_stopped_or_skipped_tasks_do_not_block_repair(adapter, difference, monkeypatch):
    client, server, torrent, fingerprint, _command = adapter
    other = Torrent(TORRENT, FOLDER, ["manual"], paused=difference == "stopped")
    other.hash = "b" * 40
    other.files = [torrent.files[0]]
    other.wanted = [difference != "skipped"]
    if difference == "folder":
        other.path = "/other-downloads"
    elif difference == "file":
        other.files = [("Different/other.mkv", torrent.files[0][1])]
    server.torrents[other.hash] = other
    inspect = client.inspect_torrent
    asked = []

    def inspect_and_note(identity):
        asked.append(identity.casefold())
        return inspect(identity)

    monkeypatch.setattr(client, "inspect_torrent", inspect_and_note)

    assert client.has_active_file_overlap(H, expected_save_path=FOLDER, expected_files_fingerprint=fingerprint) is False
    assert writes(server) == []
    if difference in {"folder", "stopped"}:
        assert set(asked) == {K}, "an unrelated or stopped task needs no full file-list request"


@pytest.mark.parametrize("malformed", ["identity", "folder", "files"])
def test_uncertain_active_candidate_never_claims_no_overlap(adapter, malformed):
    client, server, _torrent, fingerprint, _command = adapter
    other = Torrent(TORRENT, FOLDER, ["manual"], paused=False)
    other.hash = "b" * 40
    server.torrents[other.hash] = other
    if malformed == "identity":
        other.hash = "all"
    elif malformed == "folder":
        other.path = ""
    else:
        other.files, other.wanted = [], []

    with raises_code("client.managed.repair_changed"):
        client.has_active_file_overlap(H, expected_save_path=FOLDER, expected_files_fingerprint=fingerprint)
    assert writes(server) == []


def test_unknown_candidate_state_does_not_implicitly_mean_stopped(adapter, monkeypatch):
    client, server, _torrent, fingerprint, _command = adapter
    own = client.inspect_torrent(H)
    other = {**own, "hash": "b" * 40, "state": "unknown", "tags": ["manual"]}
    monkeypatch.setattr(client, "_other_torrents", lambda *_args: [other])

    with raises_code("client.managed.repair_changed"):
        client.has_active_file_overlap(H, expected_save_path=FOLDER, expected_files_fingerprint=fingerprint)
    assert writes(server) == []


def test_a_move_cannot_be_excluded_by_its_destination_alone(adapter, monkeypatch):
    client, server, _torrent, fingerprint, _command = adapter
    own = client.inspect_torrent(H)
    other = {**own, "hash": "b" * 40, "state": "moving", "save_path": "/other-downloads"}
    assert client._repair_candidate(other, H, FOLDER) is True
    monkeypatch.setattr(client, "_other_torrents", lambda *_args: [other])

    with raises_code("client.managed.repair_changed"):
        client.has_active_file_overlap(H, expected_save_path=FOLDER, expected_files_fingerprint=fingerprint)
    assert writes(server) == []


@pytest.mark.parametrize("change", [{"save_path": "/moved"}, {"hash": "c" * 40}, None])
def test_changed_candidate_readback_is_not_safely_ignored(adapter, change):
    client, _server, _torrent, _fingerprint, _command = adapter
    candidate = {"hash": "b" * 40, "save_path": FOLDER}
    info = {**candidate, **change} if change is not None else None
    with raises_code("client.managed.repair_changed"):
        client._repair_other_readback(candidate, info)


def test_empty_or_duplicated_native_listing_is_not_evidence_of_no_other_tasks(adapter):
    client, server, _torrent, _fingerprint, _command = adapter
    row = {"hash": H, "state": "stoppedDL", "save_path": FOLDER}
    for listing in ([], [row, row]):
        with raises_code("client.managed.repair_changed"):
            client._repair_candidates(listing, H, FOLDER)
    assert writes(server) == []


@pytest.mark.parametrize("bad_hash", ["all", "", "a" * 40 + "|" + "b" * 40, " a" + "a" * 39])
def test_batch_or_invalid_identity_never_reaches_a_native_repair_command(adapter, bad_hash):
    client, server, _torrent, fingerprint, _command = adapter
    for method in (client.recheck_owned_torrent, client.resume_repaired_torrent):
        with raises_code("client.managed.repair_changed"):
            method(bad_hash, expected_save_path=FOLDER, expected_files_fingerprint=fingerprint)
    assert server.calls == []


def test_physical_path_comparison_keeps_windows_case_rules_and_posix_case():
    row = [{"index": 1, "name": "Show/E01.mkv", "size": 10, "priority": 1}]
    assert files.recovery_paths(r"D:\TV", row) == files.recovery_paths("d:/tv/Show", [{**row[0], "name": "e01.MKV"}])
    assert files.recovery_paths("/downloads", row) != files.recovery_paths("/Downloads", row)
    assert files.recovery_folders_overlap("/downloads", "/downloads/show")
    assert not files.recovery_folders_overlap("/downloads", "/downloads-other")


@pytest.mark.parametrize(
    "name", ["../outside.mkv", "/outside.mkv", "C:/outside.mkv", "Show/../../outside", "Show:stream.mkv"]
)
def test_unsafe_client_paths_never_prove_safe_non_overlap(name):
    row = [{"index": 0, "name": name, "size": 10, "priority": 1}]
    with pytest.raises(ValueError, match="invalid client file path"):
        files.recovery_paths(r"D:\TV", row)


@pytest.mark.parametrize("resume", [False, True])
@pytest.mark.parametrize("flag", [True, None, 0, 1, "false"])
def test_qbit_forced_or_unknown_queue_policy_is_never_changed_by_repair(monkeypatch, flag, resume):
    server = RecoveryQbit()
    server.force_start = flag
    client = _qbittorrent(server, monkeypatch)
    torrent = Torrent(TORRENT, FOLDER, ["tow"], paused=True)
    server.torrents[K] = torrent
    fingerprint = files.recovery_fingerprint(client.inspect_torrent(H)["files"])
    server.calls.clear()

    with raises_code("client.managed.recheck_unavailable"):
        perform(client, fingerprint, resume=resume)

    assert writes(server) == []
    assert server.force_start == flag


@pytest.mark.parametrize("resume", [False, True])
@pytest.mark.parametrize("change", ["folder", "owner", "identity", "running", "progress"])
def test_qbit_repair_rechecks_control_after_the_file_list_request(monkeypatch, resume, change):
    server = RecoveryQbit()
    client = _qbittorrent(server, monkeypatch)
    torrent = Torrent(TORRENT, FOLDER, ["tow"], paused=True)
    torrent.progress = 0.4
    server.torrents[K] = torrent
    fingerprint = files.recovery_fingerprint(client.inspect_torrent(H)["files"])
    read_files = server.torrents_files

    def files_then_changed(**kwargs):
        result = read_files(**kwargs)
        if change == "folder":
            torrent.path = "/downloads/moved"
        elif change == "owner":
            torrent.labels = ["manual"]
        elif change == "identity":
            torrent.hash = "b" * 40
        elif change == "running":
            torrent.running = True
        else:
            torrent.progress = 0.6
        return result

    monkeypatch.setattr(server, "torrents_files", files_then_changed)
    server.calls.clear()

    with raises_code("client.managed.not_owned" if change == "owner" else "client.managed.repair_changed"):
        perform(client, fingerprint, resume=resume)

    assert writes(server) == []


@pytest.mark.parametrize("change", ["owner", "folder", "priority", "name", "state"])
def test_changed_readback_never_confirms_or_repeats_a_repair_start(adapter, monkeypatch, change):
    client, server, torrent, fingerprint, _command = adapter
    start = client._start

    def changed(h):
        start(h)
        if change == "owner":
            torrent.labels = ["manual"]
        elif change == "folder":
            torrent.path = "/downloads/changed"
        elif change == "priority":
            torrent.wanted[0] = False
        elif change == "name":
            torrent.files[0] = ("Show/renamed.mkv", torrent.files[0][1])
        else:
            torrent.state = "Paused" if isinstance(server, RecoveryDeluge) else "stoppedDL"
            torrent.running = False

    monkeypatch.setattr(client, "_start", changed)
    error = {
        "owner": "client.managed.not_owned",
        "folder": "client.managed.wrong_folder",
        "state": "client.managed.repair_start_unconfirmed",
    }.get(change, "client.managed.repair_changed")

    with raises_code(error):
        perform(client, fingerprint, resume=True)

    assert len(writes(server)) == 1


@pytest.mark.parametrize("field", ["index", "name", "size", "priority"])
def test_recovery_fingerprint_includes_every_native_field(field):
    before = [
        {"index": 5, "name": "Show/a.mkv", "size": 0, "priority": 7},
        {"index": 9, "name": "Show/b.mkv", "size": 10, "priority": 0},
    ]
    changed = deepcopy(before)
    changed[1][field] = {"index": 10, "name": "Show/c.mkv", "size": 11, "priority": 1}[field]

    assert files.recovery_fingerprint(before) != files.recovery_fingerprint(changed)
    assert files.recovery_fingerprint(before) == files.recovery_fingerprint(list(reversed(before)))
    assert files.recovery_fingerprint(before) == files.recovery_fingerprint([{**r, "progress": 1.0} for r in before])


@pytest.mark.parametrize(
    "bad",
    [
        [],
        [None],
        [{}],
        [{"index": True, "name": "a", "size": 1, "priority": 1}],
        [{"index": 1, "name": "", "size": 1, "priority": 1}],
        [{"index": 1, "name": "a", "size": -1, "priority": 1}],
        [{"index": 1, "name": "a", "size": 1, "priority": False}],
        [{"index": 1, "name": "a", "size": 1, "priority": 1}] * 2,
    ],
)
def test_malformed_native_file_snapshot_grants_no_permission(bad):
    with pytest.raises(ValueError, match="invalid client file snapshot"):
        files.recovery_fingerprint(bad)
