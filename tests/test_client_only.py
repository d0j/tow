"""Standalone handovers use fake native clients and never create watched topics."""

from __future__ import annotations

import json
from contextlib import contextmanager
from copy import deepcopy
from typing import ClassVar
from uuid import uuid4

import pytest
import test_clients_managed
from helpers import make_torrent, raises_code

from tow import client_only, client_only_store
from tow.clients.managed import AddMarkError, ManagedClient
from tow.errors import TowError
from tow.selection import normalize_policy
from tow.store import CheckBusyError, load_state, save_state, state_path
from tow.torrent import parse_torrent_metadata

BLOB = make_torrent(
    {
        b"name": b"Show",
        b"piece length": 16384,
        b"pieces": b"x" * 20,
        b"files": [{b"path": [name], b"length": 100} for name in (b"S01E01.mkv", b"S01E02.mkv", b"info.nfo")],
    },
    announce="https://tracker.example/secret-passkey/announce",
)
META = parse_torrent_metadata(BLOB)
CFG = {"clients": {"main": {"kind": "qbittorrent", "default": True}}}


class Native(ManagedClient):
    client_kind = "synthetic"
    capabilities: ClassVar[dict[str, bool]] = {**ManagedClient.capabilities, "recheck_files": True}
    POLLS = START_POLLS = 2
    PAUSE = INITIAL_CHECK_GRACE_SEC = 0

    def __init__(self):
        self.info = None
        self.calls = []
        self.checking = False
        self.lose_start_reply = False
        self.lose_add_reply = False
        self.on_owner = None
        self.overlap = False

    def ping(self):
        return "ok"

    def inspect_torrent(self, infohash):
        return deepcopy(self.info) if self.info and self.info["hash"].upper() == infohash.upper() else None

    def _owner_tags(self, infohash):
        if self.on_owner:
            callback, self.on_owner = self.on_owner, None
            callback()
        return super()._owner_tags(infohash)

    def _add_stopped(self, content, save_path, labels):
        self.calls.append("add")
        if self.lose_add_reply:
            raise TimeoutError("reply unknown")
        self.info = {
            "hash": META.client_hash,
            "save_path": save_path,
            "state": "checkingDL" if self.checking else "stoppedDL",
            "progress": 0,
            "tags": list(labels),
            "files": [
                {"index": f.index, "name": "Show/" + f.path, "size": f.size, "priority": 1, "progress": 0}
                for f in META.files
            ],
        }

    def _set_wanted(self, infohash, wanted, all_ids):
        self.calls.append("selection")
        for item in self.info["files"]:
            item["priority"] = 1 if item["index"] in wanted else 0

    def _start(self, infohash):
        self.calls.append("start")
        entries = client_only_store.read_entries()
        assert any(row["start_dispatched"] for row in entries.values())
        self.info["state"] = "queuedDL"
        if self.lose_start_reply:
            raise TimeoutError("reply lost")

    def _stop(self, infohash):
        self.calls.append("stop")
        self.info["state"] = "stoppedDL"

    def _set_labels(self, infohash, labels):
        self.calls.append("labels")
        self.info["tags"] = labels

    def _move(self, infohash, save_path):
        pytest.fail("no standalone move")

    def _defers_initial_check(self, info):
        return str(info.get("state") or "").startswith("checking")

    def has_active_file_overlap(self, infohash, **_kwargs):
        return self.overlap


@pytest.fixture
def world(tmp_path, monkeypatch):
    native = Native()
    clock = {"now": 1800000000.0}
    cfg = deepcopy(CFG)
    secrets = {"clients": {"main": {"host": "127.0.0.1", "port": 8080, "password": "synthetic"}}}
    dest = str(tmp_path / "downloads")
    monkeypatch.setattr(client_only.time, "time", lambda: clock["now"])
    monkeypatch.setattr(client_only, "load_config", lambda: deepcopy(cfg))
    monkeypatch.setattr(client_only, "load_secrets", lambda: deepcopy(secrets))
    monkeypatch.setattr(client_only.factory, "from_secrets", lambda *_a: native)
    monkeypatch.setattr(client_only, "volume_at", lambda _path: "test-volume")
    monkeypatch.setattr(client_only, "seen_from_here", lambda _path: True)
    monkeypatch.setattr(client_only.check, "free_space_problem", lambda *_a, **_k: None)
    save_state({"topics": [{"id": "existing-watch", "url": "https://example.test/topic/1"}], "mirrors": {}})
    return native, clock, cfg, secrets, dest


def send(world, *, policy=None, identity=None, request="same"):
    return client_only.send_to_client(
        BLOB, world[4], "main", policy or normalize_policy("all"), identity or uuid4().hex, request_fingerprint=request
    )


@pytest.mark.parametrize(("mode", "value", "count"), [("all", "", 3), ("episodes", "1", 1), ("files", "*E02*", 1)])
def test_confirmed_download_never_creates_or_changes_a_watch(world, mode, value, count):
    before = deepcopy(load_state())
    result = send(world, policy=normalize_policy(mode, value))
    assert result["status"] == "added"
    assert result["confirmed"]
    assert result["selected_file_count"] == count
    assert world[0].calls.count("add") == world[0].calls.count("start") == 1
    assert "stop" not in world[0].calls
    assert load_state() == before
    assert not client_only.pending()
    assert client_only.finished() == [result]


def test_repeated_nonce_is_idempotent_and_changed_payload_is_refused(world):
    identity = uuid4().hex
    first = send(world, identity=identity)
    calls = list(world[0].calls)
    assert send(world, identity=identity) == first
    assert world[0].calls == calls
    with raises_code("client_only.request_changed"):
        client_only.get_result(identity, request_fingerprint="changed")
    with raises_code("client_only.request_changed"):
        send(world, identity=identity, policy=normalize_policy("files", "*E02*"))


def test_existing_foreign_task_is_confirmed_without_any_mutation(world):
    native = world[0]
    native._add_stopped(BLOB, world[4], ["owner-label"])
    native.calls.clear()
    result = send(world)
    assert result["status"] == "existing"
    assert result["confirmed"]
    assert native.calls == []
    assert native.info["tags"] == ["owner-label"]


def test_existing_partial_selection_and_other_folder_are_never_changed(world):
    native = world[0]
    native._add_stopped(BLOB, world[4], ["owner-label"])
    native.info["files"][0]["priority"] = 0
    native.calls.clear()
    assert send(world)["status"] == "failed"
    assert native.calls == []
    native.info["save_path"] += "-other"
    assert send(world)["status"] == "failed"
    assert native.calls == []


def test_long_initial_check_finishes_from_receipt_after_restart_without_refetch(world):
    native, clock = world[:2]
    native.checking = True
    identity = uuid4().hex
    result = send(world, policy=normalize_policy("files", "*E02*"), identity=identity)
    assert result["status"] == "pending"
    assert native.calls == ["add"]
    receipt = client_only_store.read_entries()[identity]
    assert receipt["proof"]["start"] is False
    assert "blob" not in receipt
    native.info["state"] = "stoppedDL"
    clock["now"] += 11
    result = client_only.advance(identity)
    assert result["status"] == "added"
    assert native.calls.count("add") == native.calls.count("start") == 1
    assert native.calls.count("selection") == 1
    assert load_state()["topics"][0]["id"] == "existing-watch"


def test_lost_start_reply_is_observed_without_repeating_it(world):
    native, clock = world[:2]
    native.lose_start_reply = True
    result = send(world)
    assert result["status"] == "pending"
    assert native.calls.count("start") == 1
    clock["now"] += 11
    result = client_only.advance(result["operation_id"])
    assert result["status"] == "added"
    assert native.calls.count("add") == native.calls.count("start") == 1


def test_lost_add_reply_never_readds_when_not_yet_visible(world):
    native, clock = world[:2]
    native.lose_add_reply = True
    result = send(world)
    assert result["error"]["code"] == "client_only.connection_wait"
    assert result["error"]["cls"] == "qbit"
    for _attempt in range(3):
        clock["now"] += 100
        result = client_only.advance(result["operation_id"])
    assert result["status"] == "pending"
    assert native.calls == ["add"]


def test_changed_credentials_or_volume_cannot_start_pending_handover(world):
    native, clock, _cfg, secrets, _dest = world
    native.checking = True
    result = send(world)
    secrets["clients"]["main"]["password"] = "corrected"
    native.info["state"] = "stoppedDL"
    clock["now"] += 11
    result = client_only.advance(result["operation_id"])
    assert result["status"] == "failed"
    assert native.calls == ["add"]


@pytest.mark.parametrize("change", ["priority", "start", "owner", "path"])
def test_owner_changes_during_initial_check_prevent_later_mutations(world, change):
    native, clock = world[:2]
    native.checking = True
    result = send(world, policy=normalize_policy("files", "*E02*"))
    native.info["state"] = "stoppedDL"
    if change == "priority":
        native.info["files"][0]["priority"] = 0
    elif change == "start":
        native.info["state"] = "downloading"
    elif change == "owner":
        native.info["tags"] = []
    else:
        native.info["save_path"] += "-elsewhere"
    clock["now"] += 11
    result = client_only.advance(result["operation_id"])
    assert result["status"] == "failed"
    assert native.calls == ["add"]


def test_cancel_pending_native_check_revokes_selection_and_start(world):
    native, clock = world[:2]
    native.checking = True
    result = send(world)
    assert client_only.cancel(result["operation_id"])["status"] == "cancelled"
    native.info["state"] = "stoppedDL"
    clock["now"] += 20
    assert client_only.advance(result["operation_id"])["status"] == "cancelled"
    assert native.calls == ["add"]


def test_cancel_while_owner_http_is_in_flight_blocks_selection_and_cleanup_stop(world):
    native = world[0]
    identity = uuid4().hex
    native.on_owner = lambda: client_only.cancel(identity)
    assert send(world, identity=identity)["status"] == "cancelled"
    assert native.calls == ["add"]


def test_cancel_after_accepted_add_forbids_marking_recovery(world, monkeypatch):
    native = world[0]
    identity = uuid4().hex
    original = native._add_stopped

    def accepted_without_label(content, save_path, _labels):
        original(content, save_path, [])
        client_only.cancel(identity)
        raise AddMarkError(META.client_hash, TimeoutError("label acknowledgement lost"))

    monkeypatch.setattr(native, "_add_stopped", accepted_without_label)
    assert send(world, identity=identity)["status"] == "cancelled"
    assert native.calls == ["add"]
    assert native.info["tags"] == []
    assert native.info["state"] == "stoppedDL"


def test_unrelated_corrupt_watch_state_is_not_quarantined_or_replaced(world):
    original = b'{"topics": [this is invalid JSON'
    path = state_path()
    path.write_bytes(original)
    names = {item.name for item in path.parent.glob(path.name + "*")}
    assert send(world)["status"] == "failed"
    assert path.read_bytes() == original
    assert {item.name for item in path.parent.glob(path.name + "*")} == names
    assert world[0].calls == []


def test_busy_check_queues_encrypted_content_then_cancel_never_adds(world, monkeypatch):
    @contextmanager
    def busy(**_kwargs):
        raise CheckBusyError("busy")
        yield  # pragma: no cover

    monkeypatch.setattr(client_only, "check_run_lock", busy)
    result = send(world)
    assert result["status"] == "pending"
    assert result["phase"] == "queued"
    assert world[0].calls == []
    data = (client_only_store.paths.run_dir() / "client-only-adds.enc").read_bytes()
    assert BLOB not in data
    assert b"secret-passkey" not in data
    assert client_only.cancel(result["operation_id"])["status"] == "cancelled"
    assert world[0].calls == []


def test_overlap_and_shortfall_leave_new_native_task_stopped(world, monkeypatch):
    native = world[0]
    native.overlap = True
    assert send(world)["status"] == "failed"
    assert "start" not in native.calls


def test_expiry_and_journal_corruption_cannot_authorize_native_changes(world, monkeypatch):
    native, clock = world[:2]
    native.checking = True
    result = send(world)
    clock["now"] += client_only_store.LIFETIME + 1
    assert client_only.advance(result["operation_id"])["status"] == "failed"
    assert native.calls == ["add"]
    (client_only_store.paths.run_dir() / "client-only-adds.enc").write_bytes(b"not authenticated")
    with raises_code("client_only.store_invalid"):
        send(world)
    assert native.calls == ["add"]


def test_archive_and_update_snapshot_allowlists_never_include_handover_authority(world):
    from pathlib import Path

    from test_update import updater

    from tow import bundle
    from tow.snapshots.members import live_members

    send(world)
    receipt_path = client_only_store.paths.run_dir() / "client-only-adds.enc"
    assert receipt_path.is_file()
    assert receipt_path not in {path for _name, path in live_members()}
    snapshot = updater.data_files(client_only_store.paths.data_dir())
    assert "run/client-only-adds.enc" not in snapshot
    assert "run/client-only-compat.json" not in snapshot
    exported = bundle.export_bundle(Path(world[4]).parent / "transfer.towx", "synthetic-passphrase")
    assert exported["read_back"]
    assert all("client-only" not in name and not name.startswith("run/") for name in exported["members"])


@pytest.mark.parametrize("status", ["pending", "added", "existing", "failed", "cancelled"])
def test_rollback_projection_distinguishes_pending_from_every_terminal_outcome(world, status):
    from test_update import updater

    result = send(world)
    entries = client_only_store.read_entries()
    entries[result["operation_id"]]["status"] = status
    client_only_store.write_entries(entries)
    expected = status == "pending"
    assert client_only_store.has_pending_or_unknown() is expected
    assert updater.client_only_pending(client_only_store.paths.data_dir()) is expected
    projection = (client_only_store.paths.run_dir() / client_only_store.COMPATIBILITY).read_bytes()
    assert b"secret-passkey" not in projection
    assert json.loads(projection)["pending"] is expected


def test_old_authenticated_terminal_journal_can_repair_missing_projection(world):
    from test_update import updater

    send(world)
    marker = client_only_store.paths.run_dir() / client_only_store.COMPATIBILITY
    marker.unlink()
    assert updater.client_only_pending(client_only_store.paths.data_dir()) is True
    calls = list(world[0].calls)
    assert client_only_store.has_pending_or_unknown() is False
    assert updater.client_only_pending(client_only_store.paths.data_dir()) is False
    assert world[0].calls == calls


def test_background_producer_migrates_legacy_terminal_projection_without_a_web_read_write(world):
    from test_update import updater

    result = send(world)
    marker = client_only_store.paths.run_dir() / client_only_store.COMPATIBILITY
    marker.unlink()
    assert client_only.get_result(result["operation_id"]) == result
    assert client_only.finished() == [result]
    assert not marker.exists()
    calls = list(world[0].calls)
    assert updater.client_only_pending(client_only_store.paths.data_dir()) is True
    assert client_only.advance_pending() == []
    assert updater.client_only_pending(client_only_store.paths.data_dir()) is False
    assert world[0].calls == calls


def test_background_compatibility_write_failure_stays_monitor_visible(world, monkeypatch):
    send(world)
    marker = client_only_store.paths.run_dir() / client_only_store.COMPATIBILITY
    marker.unlink()
    calls = list(world[0].calls)
    monkeypatch.setattr(
        client_only_store, "atomic_write_bytes", lambda *_args: (_ for _ in ()).throw(OSError("projection unavailable"))
    )
    with pytest.raises(OSError, match="projection unavailable"):
        client_only.advance_pending()
    assert client_only_store.has_pending_or_unknown() is True
    assert not marker.exists()
    assert world[0].calls == calls


def test_torn_receipt_write_cannot_reuse_stale_terminal_projection(world, monkeypatch):
    from test_update import updater

    result = send(world)
    entries = client_only_store.read_entries()
    entries[result["operation_id"]]["status"] = "pending"
    original = client_only_store.atomic_write_bytes

    def crash_before_marker(path, content):
        if path.name == client_only_store.COMPATIBILITY:
            assert updater.client_only_pending(client_only_store.paths.data_dir()) is True
            raise OSError("power lost before projection")
        return original(path, content)

    with monkeypatch.context() as cut:
        cut.setattr(client_only_store, "atomic_write_bytes", crash_before_marker)
        with pytest.raises(OSError, match="power lost"):
            client_only_store.write_entries(entries)
    assert updater.client_only_pending(client_only_store.paths.data_dir()) is True
    assert client_only_store.has_pending_or_unknown() is True
    entries[result["operation_id"]]["status"] = "cancelled"
    client_only_store.write_entries(entries)
    assert updater.client_only_pending(client_only_store.paths.data_dir()) is False


@pytest.mark.parametrize("fault", ["journal_missing", "journal_corrupt", "journal_schema", "unreadable"])
def test_unknown_handoff_authority_never_allows_old_rollback(world, monkeypatch, fault):
    from test_update import updater

    send(world)
    journal = client_only_store.paths.run_dir() / client_only_store.JOURNAL
    if fault == "journal_missing":
        journal.unlink()
    elif fault == "journal_corrupt":
        journal.write_bytes(b"damaged ciphertext")
    elif fault == "journal_schema":
        journal.write_bytes(client_only_store.master_fernet().encrypt(b'{"schema": 99, "entries": {}}'))
    else:
        monkeypatch.setattr(client_only_store, "_read_file", lambda *_args: (_ for _ in ()).throw(PermissionError()))
        monkeypatch.setattr(updater, "_handoff_file", lambda *_args: (_ for _ in ()).throw(PermissionError()))
    assert client_only_store.has_pending_or_unknown() is True
    assert updater.client_only_pending(client_only_store.paths.data_dir()) is True


def test_no_handoff_journal_does_not_ratchet_rollback_floor(world):
    from test_update import updater

    assert client_only_store.has_pending_or_unknown() is False
    assert updater.client_only_pending(client_only_store.paths.data_dir()) is False


def test_orphan_projection_blocks_new_handoff_and_monitor_read_without_erasing_evidence(world):
    send(world)
    folder = client_only_store.paths.run_dir()
    journal = folder / client_only_store.JOURNAL
    journal.unlink()
    marker = folder / client_only_store.COMPATIBILITY
    evidence = marker.read_bytes()
    world[0].calls.clear()
    with raises_code("client_only.store_invalid"):
        client_only.finished()
    with raises_code("client_only.store_invalid"):
        send(world, request="a fresh explicit request")
    assert world[0].calls == []
    assert not journal.exists()
    assert marker.read_bytes() == evidence


def test_cancel_duplicate_busy_request_revokes_the_single_original_add(world, monkeypatch):
    @contextmanager
    def busy(**_kwargs):
        raise CheckBusyError("busy")
        yield  # pragma: no cover

    with monkeypatch.context() as blocked:
        blocked.setattr(client_only, "check_run_lock", busy)
        first = send(world)
        second = send(world)
    assert first["operation_id"] != second["operation_id"]
    assert client_only.cancel(second["operation_id"])["status"] == "cancelled"
    assert client_only.get_result(first["operation_id"])["status"] == "cancelled"
    assert client_only.advance_pending() == []
    assert world[0].calls == []


def test_new_nonce_after_lost_add_is_readonly_even_when_task_appears_later(world):
    native, clock = world[:2]
    native.lose_add_reply = True
    original = send(world)
    duplicate = send(world)
    assert original["status"] == duplicate["status"] == "pending"
    assert native.calls == ["add"]
    native.lose_add_reply = False
    native._add_stopped(BLOB, world[4], ["tow"])
    native.calls.clear()
    clock["now"] += 11
    assert client_only.advance(duplicate["operation_id"])["status"] == "existing"
    assert native.calls == []


def test_cancel_between_start_reservation_and_native_rpc_spends_but_never_issues_start(world, monkeypatch):
    original = client_only._dispatch

    def dispatch(row, key):
        original(row, key)
        if key == "start_dispatched":
            client_only.cancel(row["operation_id"])

    monkeypatch.setattr(client_only, "_dispatch", dispatch)
    result = send(world)
    assert result["status"] == "cancelled"
    assert "start" not in world[0].calls
    assert client_only_store.read_entries()[result["operation_id"]]["start_dispatched"] is True


def test_cancel_after_native_preflight_prevents_add(world, monkeypatch):
    identity = uuid4().hex
    monkeypatch.setattr(world[0], "_prepare_add", lambda: client_only.cancel(identity))
    assert send(world, identity=identity)["status"] == "cancelled"
    assert world[0].calls == []


def test_clock_rollback_never_extends_queued_authority(world, monkeypatch):
    @contextmanager
    def busy(**_kwargs):
        raise CheckBusyError("busy")
        yield  # pragma: no cover

    with monkeypatch.context() as blocked:
        blocked.setattr(client_only, "check_run_lock", busy)
        result = send(world)
    world[1]["now"] -= 100
    result = client_only.advance(result["operation_id"])
    assert result["status"] == "failed"
    assert result["error"]["code"] == "client_only.expired"
    assert world[0].calls == []


def test_new_server_pause_before_start_does_not_spend_or_issue_start(world, monkeypatch):
    from tow import client_access

    def overlap(_hash, **_kwargs):
        context = client_access.context(world[2], world[3], "main")
        state = load_state()
        client_access.observe(
            state, context, TowError("client.managed.http_error", code=429, retry_after=60), now=world[1]["now"]
        )
        save_state(state)
        return False

    monkeypatch.setattr(world[0], "has_active_file_overlap", overlap)
    result = send(world)
    assert result["status"] == "pending"
    assert "start" not in world[0].calls
    assert client_only_store.read_entries()[result["operation_id"]]["start_dispatched"] is False


def test_original_disk_replacement_during_owner_query_prevents_selection(world, monkeypatch):
    observed = {"volume": "test-volume"}
    monkeypatch.setattr(client_only, "volume_at", lambda _path: observed["volume"])
    world[0].on_owner = lambda: observed.update(volume="another-volume")
    result = send(world)
    assert result["status"] == "failed"
    assert result["error"]["code"] == "client_only.volume_unavailable"
    assert world[0].calls == ["add"]


def test_remote_or_container_path_uses_native_evidence_without_local_disk_guess(world, monkeypatch):
    monkeypatch.setattr(client_only.factory, "on_this_computer", lambda *_args: True)
    monkeypatch.setattr(client_only, "seen_from_here", lambda _path: False)
    monkeypatch.setattr(
        client_only, "volume_at", lambda _path: pytest.fail("no local volume lookup for an unseen container folder")
    )
    result = client_only.send_to_client(BLOB, "/srv/downloads", "main", normalize_policy("all"), uuid4().hex)
    assert result["status"] == "added"
    assert world[0].info["save_path"] == "/srv/downloads"


def test_existing_magnet_metadata_wait_is_readonly_and_never_readded(world):
    native, clock = world[:2]
    native._add_stopped(BLOB, world[4], ["owner"])
    expected = deepcopy(native.info["files"])
    native.info.update(state="metaDL", files=[])
    native.calls.clear()
    result = send(world)
    assert result["status"] == "pending"
    native.info.update(state="downloading", files=expected)
    clock["now"] += 11
    assert client_only.advance(result["operation_id"])["status"] == "existing"
    assert native.calls == []


def test_exact_selection_is_bound_to_the_prepared_revision(world):
    selected = [{"path": META.files[1].path, "size": META.files[1].size}]
    policy = normalize_policy("exact", files=selected, source_hash=META.infohash)
    assert send(world, policy=policy)["status"] == "added"
    assert world[0].info["files"][1]["priority"] > 0
    before = list(world[0].calls)
    policy["source_hash"] = "a" * 40
    with raises_code("selection.exact_changed"):
        send(world, policy=policy)
    assert world[0].calls == before


def test_shortfall_is_refused_before_any_native_add(world, monkeypatch):
    from tow.check.space import Shortfall

    monkeypatch.setattr(
        client_only.check, "free_space_problem", lambda *_a, **_k: Shortfall(needed=1000, free=0, path=world[4])
    )
    result = send(world)
    assert result["status"] == "failed"
    assert result["error"]["code"] == "check.waiting_space"
    assert world[0].calls == []
    assert not client_only.pending()


def test_bounded_store_refuses_another_queued_operation_before_rpc(world, monkeypatch):
    @contextmanager
    def busy(**_kwargs):
        raise CheckBusyError("busy")
        yield  # pragma: no cover

    monkeypatch.setattr(client_only, "check_run_lock", busy)
    monkeypatch.setattr(client_only_store, "MAX_RECEIPTS", 1)
    assert send(world)["status"] == "pending"
    with raises_code("client_only.capacity"):
        send(world)
    assert world[0].calls == []


@pytest.fixture(params=["qbittorrent", "transmission", "deluge"])
def native_adapter(request, monkeypatch):
    return test_clients_managed.client.__wrapped__(request, monkeypatch)


def test_standalone_handover_uses_each_real_adapter_contract(world, native_adapter, monkeypatch):
    adapter, server = native_adapter
    adapter.client_id = "main"
    world[2]["clients"]["main"]["kind"] = adapter.client_kind
    monkeypatch.setattr(client_only.factory, "from_secrets", lambda *_args: adapter)
    if adapter.client_kind == "qbittorrent":
        original_row = server._row
        monkeypatch.setattr(server, "_row", lambda torrent: {**original_row(torrent), "force_start": False})
    if adapter.client_kind == "deluge":
        monkeypatch.setattr(
            server,
            "m_core_get_torrents_status",
            lambda _filters, keys: {
                identity: server.m_core_get_torrent_status(identity, keys) for identity in server.torrents
            },
            raising=False,
        )
    before = deepcopy(load_state())
    result = send(world, policy=normalize_policy("files", "*E02*"))
    assert result["status"] == "added", result.get("error")
    assert result["confirmed"]
    info = adapter.inspect_torrent(META.client_hash)
    assert sum(item["priority"] > 0 for item in info["files"]) == 1
    assert load_state() == before


@pytest.mark.parametrize("native_adapter", ["transmission", "deluge"], indirect=True)
def test_cancel_after_real_native_add_forbids_followup_label_and_selection(world, native_adapter, monkeypatch):
    adapter, server = native_adapter
    adapter.client_id = "main"
    world[2]["clients"]["main"]["kind"] = adapter.client_kind
    monkeypatch.setattr(client_only.factory, "from_secrets", lambda *_args: adapter)
    identity = uuid4().hex
    if adapter.client_kind == "deluge":
        original = server.m_core_add_torrent_file

        def add(*args):
            result = original(*args)
            client_only.cancel(identity)
            return result

        monkeypatch.setattr(server, "m_core_add_torrent_file", add)
    else:
        original = adapter._rpc

        def rpc(method, **arguments):
            result = original(method, **arguments)
            if method == "torrent-add":
                client_only.cancel(identity)
            return result

        monkeypatch.setattr(adapter, "_rpc", rpc)
    assert send(world, identity=identity)["status"] == "cancelled"
    assert len(server.torrents) == 1
    assert not next(iter(server.torrents.values())).running
    forbidden = {
        "label.set_torrent",
        "core.set_torrent_options",
        "core.resume_torrents",
        "core.pause_torrents",
        "torrent-set",
        "torrent-start",
        "torrent-stop",
    }
    assert forbidden.isdisjoint(server.calls)
