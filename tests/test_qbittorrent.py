from __future__ import annotations

from types import SimpleNamespace

import pytest
from helpers import make_torrent, multi_file_torrent, raises_code

from tow.clients import qbittorrent
from tow.torrent import parse_torrent_metadata

TORRENT = multi_file_torrent(
    b"Show",
    [
        {b"length": 10, b"path": [b"S01E01.mkv"]},
        {b"length": 20, b"path": [b"S01E02.mkv"]},
    ],
)
NON_UTF_TORRENT = multi_file_torrent(b"Show", [{b"length": 10, b"path": ["Серия.avi".encode("cp1251")]}])


class _FakeAPI:
    def __init__(self):
        self.app = SimpleNamespace(version="5.2.3", web_api_version="2.15.1")

    def torrents_info(self, *, torrent_hashes):
        return [
            SimpleNamespace(
                hash=torrent_hashes.upper(),
                progress=1.0,
                added_on=1,
                completion_on=2,
                save_path=r"M:\\TV",
                content_path=r"M:\\TV\\show",
                tags="other, tow",
            )
        ]

    def torrents_files(self, *, torrent_hash):
        return []


def test_inspect_torrent_reads_normalized_tags(monkeypatch):
    api = _FakeAPI()
    monkeypatch.setattr(qbittorrent, "Client", lambda **kwargs: api)
    client = qbittorrent.QBittorrentClient("http://qbit", 8080, "user", "password")

    info = client.inspect_torrent("aa" * 20)

    assert info["tags"] == ["other", "tow"]


class _SelectionAPI:
    def __init__(self, *, mismatch=False):
        self.app = SimpleNamespace(version="5.2.3", web_api_version="2.15.1")
        self.present = False
        self.state = "stoppedDL"
        self.priorities = {4: 0, 9: 0}
        self.mismatch = mismatch
        self.calls = []
        self.tags = "tow,tow-pending"

    def torrents_add(self, **kwargs):
        assert kwargs["is_stopped"] is True
        assert kwargs["use_auto_torrent_management"] is False
        assert "use_auto_tmm" not in kwargs
        self.present = True
        self.calls.append("add-stopped")
        return "Ok."

    def torrents_info(self, *, torrent_hashes):
        if not self.present:
            return []
        return [
            SimpleNamespace(
                hash=torrent_hashes.upper(),
                progress=0,
                added_on=1,
                completion_on=0,
                save_path=r"M:\TV",
                content_path=r"M:\TV\Show",
                tags=self.tags,
                state=self.state,
            )
        ]

    def torrents_files(self, *, torrent_hash):
        return [
            SimpleNamespace(index=4, name="Show/S01E01.mkv", size=10, progress=0, priority=self.priorities[4]),
            SimpleNamespace(index=9, name="Show/S01E02.mkv", size=20, progress=0, priority=self.priorities[9]),
        ]

    def torrents_file_priority(self, *, torrent_hash, file_ids, priority):
        self.calls.append(("priority", tuple(file_ids), priority))
        for index in file_ids:
            self.priorities[index] = priority
        if self.mismatch and priority == 1:
            self.priorities[file_ids[0]] = 0

    def torrents_stop(self, *, torrent_hashes):
        self.state = "stoppedDL"
        self.calls.append("stop")

    def torrents_start(self, *, torrent_hashes):
        self.state = "downloading"
        self.calls.append("start")

    def torrents_remove_tags(self, *, tags, torrent_hashes):
        self.tags = "tow"
        self.calls.append("clear-pending")


@pytest.mark.parametrize("operation", ["selection", "move", "release"])
def test_foreign_torrent_mutations_are_refused(monkeypatch, operation):
    api = _SelectionAPI()
    api.present = True
    api.tags = "manual"
    api.torrents_set_location = lambda **_kwargs: api.calls.append("move")
    monkeypatch.setattr(qbittorrent, "Client", lambda **_kwargs: api)
    client = qbittorrent.QBittorrentClient("http://qbit", 8080, "user", "password")
    infohash = parse_torrent_metadata(TORRENT).client_hash
    with raises_code("client.managed.not_owned", RuntimeError):
        if operation == "selection":
            client.configure_torrent_selection(TORRENT, infohash, [0])
        elif operation == "move":
            client.set_location(infohash, "E:/moved")
        else:
            client._clear_pending(infohash)
    assert api.calls == []


def test_ownership_checks_read_one_row_without_the_file_list(monkeypatch):
    api = _SelectionAPI()
    api.present = True
    api.state = "downloading"
    api.tags = "tow"
    file_lists = []
    real_files = api.torrents_files
    monkeypatch.setattr(api, "torrents_files", lambda **kwargs: file_lists.append(1) or real_files(**kwargs))
    api.torrents_set_location = lambda **_kwargs: api.calls.append("move")
    monkeypatch.setattr(qbittorrent, "Client", lambda **_kwargs: api)
    client = qbittorrent.QBittorrentClient("http://qbit", 8080, "user", "password")
    infohash = parse_torrent_metadata(TORRENT).client_hash
    assert client.set_location(infohash, "E:/moved") == "ok"
    assert file_lists == [], "an ownership check must not read the whole file list"
    client.configure_torrent_selection(TORRENT, infohash, [1])
    # Selection read-backs only: before, after the priorities, and the start confirmation.
    assert len(file_lists) <= 5, len(file_lists)


def test_partial_readback_cannot_confirm_skipped_files(monkeypatch):
    api = _SelectionAPI()
    api.present = True
    real_files = api.torrents_files

    def files(*, torrent_hash):
        rows = real_files(torrent_hash=torrent_hash)
        if any(isinstance(call, tuple) and call[0] == "priority" for call in api.calls):
            return rows[:1]
        return rows

    monkeypatch.setattr(api, "torrents_files", files)
    monkeypatch.setattr(qbittorrent, "Client", lambda **_kwargs: api)
    client = qbittorrent.QBittorrentClient("http://qbit", 8080, "user", "password")
    meta = parse_torrent_metadata(TORRENT)
    with raises_code("client.managed.wrong_selection", RuntimeError):
        client._apply_selection(meta.client_hash, meta.files, {0}, meta.name)


@pytest.mark.parametrize("adding", [False, True])
def test_ownership_loss_during_selection_does_not_trigger_cleanup_mutations(monkeypatch, adding):
    api = _SelectionAPI()
    api.present = not adding
    api.state = "downloading" if not adding else "stoppedDL"
    monkeypatch.setattr(qbittorrent, "Client", lambda **_kwargs: api)
    monkeypatch.setattr(qbittorrent.time, "sleep", lambda _seconds: None)

    def priority(**_kwargs):
        api.calls.append("priority failed")
        api.tags = "manual"
        raise ConnectionError("priority connection failed")

    monkeypatch.setattr(api, "torrents_file_priority", priority)
    client = qbittorrent.QBittorrentClient("http://qbit", 8080, "user", "password")
    infohash = parse_torrent_metadata(TORRENT).client_hash
    operation = (
        (lambda: client.add_torrent_selected(TORRENT, r"M:\TV", infohash, [0]))
        if adding
        else lambda: client.configure_torrent_selection(TORRENT, infohash, [0])
    )
    with pytest.raises(ConnectionError, match="priority connection failed"):
        operation()
    assert api.calls[-1] == "priority failed"


def test_selected_add_is_stopped_configured_verified_then_started(monkeypatch):
    api = _SelectionAPI()
    monkeypatch.setattr(qbittorrent, "Client", lambda **kwargs: api)
    client = qbittorrent.QBittorrentClient("http://qbit", 8080, "user", "password")
    metadata = parse_torrent_metadata(TORRENT)

    client.add_torrent_selected(TORRENT, r"M:\TV", metadata.infohash, [1])

    assert api.priorities == {4: 0, 9: 1}
    assert api.calls[-1] == "clear-pending"


def test_selected_add_waits_for_delayed_visibility_and_tags(monkeypatch):
    class DelayedAPI(_SelectionAPI):
        def __init__(self):
            super().__init__()
            self.reads = 0

        def torrents_info(self, *, torrent_hashes):
            self.reads += 1
            if self.reads <= 2:
                return []
            rows = super().torrents_info(torrent_hashes=torrent_hashes)
            if self.reads <= 4:
                rows[0].tags = ""
            return rows

    api = DelayedAPI()
    monkeypatch.setattr(qbittorrent, "Client", lambda **kwargs: api)
    monkeypatch.setattr(qbittorrent.time, "sleep", lambda _seconds: None)
    client = qbittorrent.QBittorrentClient("http://qbit", 8080, "user", "password")
    metadata = parse_torrent_metadata(TORRENT)

    result = client.add_torrent_selected(TORRENT, r"M:\TV", metadata.client_hash, [1])

    assert result["tags"] == ["tow"]
    assert api.reads > 4
    assert api.state == "downloading"


def test_tag_clear_readback_failure_does_not_stop_started_torrent(monkeypatch):
    class StaleTagAPI(_SelectionAPI):
        def torrents_remove_tags(self, *, tags, torrent_hashes):
            self.calls.append("clear-pending")

    api = StaleTagAPI()
    monkeypatch.setattr(qbittorrent, "Client", lambda **kwargs: api)
    monkeypatch.setattr(qbittorrent.time, "sleep", lambda _seconds: None)
    client = qbittorrent.QBittorrentClient("http://qbit", 8080, "user", "password")
    metadata = parse_torrent_metadata(TORRENT)

    with raises_code("client.managed.pending_not_cleared", RuntimeError):
        client.add_torrent_selected(TORRENT, r"M:\TV", metadata.client_hash, [1])

    assert api.state == "downloading"
    assert api.calls.count("stop") == 1


def test_selected_add_checks_save_path_before_start(monkeypatch):
    class WrongPathAPI(_SelectionAPI):
        def torrents_info(self, *, torrent_hashes):
            rows = super().torrents_info(torrent_hashes=torrent_hashes)
            if rows:
                rows[0].save_path = r"D:\wrong"
            return rows

    api = WrongPathAPI()
    monkeypatch.setattr(qbittorrent, "Client", lambda **kwargs: api)
    client = qbittorrent.QBittorrentClient("http://qbit", 8080, "user", "password")
    metadata = parse_torrent_metadata(TORRENT)

    with raises_code("client.managed.wrong_folder", RuntimeError):
        client.add_torrent_selected(TORRENT, r"M:\TV", metadata.client_hash, [1])

    assert api.state == "stoppedDL"
    assert "start" not in api.calls


def test_unconfirmed_add_never_stops_possibly_foreign_torrent(monkeypatch):
    api = _SelectionAPI()
    api.tags = "manual"
    monkeypatch.setattr(qbittorrent, "Client", lambda **kwargs: api)
    monkeypatch.setattr(qbittorrent.time, "sleep", lambda _seconds: None)
    client = qbittorrent.QBittorrentClient("http://qbit", 8080, "user", "password")
    metadata = parse_torrent_metadata(TORRENT)

    with raises_code("client.managed.no_owner_mark", RuntimeError):
        client.add_torrent_selected(TORRENT, r"M:\TV", metadata.client_hash, [1])

    assert "stop" not in api.calls


@pytest.mark.parametrize(
    "reply",
    [
        {"success_count": 0, "pending_count": 0, "failure_count": 1},
        {"success_count": 0, "pending_count": 0, "failure_count": 0},
        {"success_count": "broken", "pending_count": 0, "failure_count": 0},
    ],
)
def test_selected_add_rejects_unsuccessful_json_reply(monkeypatch, reply):
    class JsonReplyAPI(_SelectionAPI):
        def torrents_add(self, **kwargs):
            self.calls.append("add-rejected")
            return reply

    api = JsonReplyAPI()
    monkeypatch.setattr(qbittorrent, "Client", lambda **kwargs: api)
    client = qbittorrent.QBittorrentClient("http://qbit", 8080, "user", "password")
    metadata = parse_torrent_metadata(TORRENT)

    with pytest.raises(RuntimeError) as caught:
        client.add_torrent_selected(TORRENT, r"M:\TV", metadata.client_hash, [1])
    assert caught.value.code in {"client.qbittorrent.api_counts", "client.qbittorrent.api_malformed"}

    assert "stop" not in api.calls


def test_selected_add_accepts_pending_json_reply_and_waits(monkeypatch):
    class PendingReplyAPI(_SelectionAPI):
        def torrents_add(self, **kwargs):
            assert kwargs["use_auto_torrent_management"] is False
            self.present = True
            self.calls.append("add-pending")
            return {
                "success_count": 0,
                "pending_count": 1,
                "failure_count": 0,
                "added_torrent_ids": [],
            }

    api = PendingReplyAPI()
    monkeypatch.setattr(qbittorrent, "Client", lambda **kwargs: api)
    client = qbittorrent.QBittorrentClient("http://qbit", 8080, "user", "password")
    metadata = parse_torrent_metadata(TORRENT)

    client.add_torrent_selected(TORRENT, r"M:\TV", metadata.client_hash, [1])

    assert api.state == "downloading"
    assert api.tags == "tow"


def test_start_confirmation_accepts_completed_torrent_stopped_by_share_policy(monkeypatch):
    class CompleteAPI(_SelectionAPI):
        def torrents_info(self, *, torrent_hashes):
            rows = super().torrents_info(torrent_hashes=torrent_hashes)
            if rows:
                rows[0].progress = 1
                rows[0].state = "stoppedUP"
            return rows

    api = CompleteAPI()
    api.present = True
    monkeypatch.setattr(qbittorrent, "Client", lambda **kwargs: api)
    client = qbittorrent.QBittorrentClient("http://qbit", 8080, "user", "password")

    confirmed = client._wait_started("AA" * 20)

    assert confirmed["progress"] == 1
    assert confirmed["state"] == "stoppedUP"


def test_priority_readback_mismatch_fails_and_leaves_torrent_stopped(monkeypatch):
    api = _SelectionAPI(mismatch=True)
    monkeypatch.setattr(qbittorrent, "Client", lambda **kwargs: api)
    client = qbittorrent.QBittorrentClient("http://qbit", 8080, "user", "password")
    metadata = parse_torrent_metadata(TORRENT)

    with raises_code("client.managed.wrong_selection", RuntimeError):
        client.add_torrent_selected(TORRENT, r"M:\TV", metadata.infohash, [0])

    assert api.state == "stoppedDL"
    assert api.calls[-1] == "stop"


def test_pending_failed_add_is_started_and_marker_cleared_on_retry(monkeypatch):
    api = _SelectionAPI(mismatch=True)
    monkeypatch.setattr(qbittorrent, "Client", lambda **kwargs: api)
    client = qbittorrent.QBittorrentClient("http://qbit", 8080, "user", "password")
    metadata = parse_torrent_metadata(TORRENT)
    with pytest.raises(RuntimeError):
        client.add_torrent_selected(TORRENT, r"M:\TV", metadata.client_hash, [0])

    api.mismatch = False
    client.configure_torrent_selection(TORRENT, metadata.client_hash, [0])

    assert api.state == "downloading"
    assert api.tags == "tow"
    assert api.calls[-1] == "clear-pending"


def test_retry_tag_clear_readback_failure_does_not_stop_started_torrent(monkeypatch):
    class StaleTagAPI(_SelectionAPI):
        def torrents_remove_tags(self, *, tags, torrent_hashes):
            self.calls.append("clear-pending")

    api = StaleTagAPI()
    api.present = True
    monkeypatch.setattr(qbittorrent, "Client", lambda **kwargs: api)
    monkeypatch.setattr(qbittorrent.time, "sleep", lambda _seconds: None)
    client = qbittorrent.QBittorrentClient("http://qbit", 8080, "user", "password")
    metadata = parse_torrent_metadata(TORRENT)

    with raises_code("client.managed.pending_not_cleared", RuntimeError):
        client.configure_torrent_selection(TORRENT, metadata.client_hash, [0], ensure_started=True)

    assert api.state == "downloading"
    assert api.calls.count("stop") == 1


def test_tow_only_stopped_recovery_is_explicitly_started(monkeypatch):
    api = _SelectionAPI()
    api.present = True
    api.tags = "tow"
    monkeypatch.setattr(qbittorrent, "Client", lambda **kwargs: api)
    client = qbittorrent.QBittorrentClient("http://qbit", 8080, "user", "password")
    metadata = parse_torrent_metadata(TORRENT)

    client.configure_torrent_selection(TORRENT, metadata.client_hash, [0], ensure_started=True)

    assert api.state == "downloading"
    assert api.calls[-1] == "start"


def test_magnet_metadata_is_stopped_exported_and_hash_verified(monkeypatch):
    metadata = parse_torrent_metadata(TORRENT)

    class MagnetAPI(_SelectionAPI):
        def torrents_add(self, **kwargs):
            assert kwargs["stop_condition"] == "MetadataReceived"
            assert kwargs["tags"] == "tow,tow-pending"
            assert kwargs["urls"] == f"magnet:?xt=urn:btih:{metadata.client_hash}"
            self.present = True
            self.state = "stoppedDL"
            self.calls.append("add-magnet-metadata")
            return "Ok."

        def torrents_export(self, *, torrent_hash):
            assert torrent_hash == metadata.client_hash.lower()
            self.calls.append("export")
            return TORRENT

    api = MagnetAPI()
    monkeypatch.setattr(qbittorrent, "Client", lambda **kwargs: api)
    client = qbittorrent.QBittorrentClient("http://qbit", 8080, "user", "password")

    content = client.materialize_magnet(
        f"magnet:?xt=urn:btih:{metadata.client_hash}",
        r"M:\TV",
        metadata.client_hash,
    )

    assert content == TORRENT
    assert api.calls == ["add-magnet-metadata", "stop", "export"]


def test_magnet_metadata_accepts_base32_btih(monkeypatch):
    import base64

    metadata = parse_torrent_metadata(TORRENT)
    base32_hash = base64.b32encode(bytes.fromhex(metadata.client_hash)).decode("ascii")

    class MagnetAPI(_SelectionAPI):
        def torrents_add(self, **kwargs):
            self.present = True
            self.state = "stoppedDL"
            return "Ok."

        def torrents_export(self, *, torrent_hash):
            return TORRENT

    monkeypatch.setattr(qbittorrent, "Client", lambda **kwargs: MagnetAPI())
    client = qbittorrent.QBittorrentClient("http://qbit", 8080, "user", "password")

    assert (
        client.materialize_magnet(
            f"magnet:?xt=urn:btih:{base32_hash}",
            r"M:\TV",
            metadata.client_hash,
        )
        == TORRENT
    )


def test_magnet_metadata_refuses_old_webapi_before_add(monkeypatch):
    metadata = parse_torrent_metadata(TORRENT)
    api = _SelectionAPI()
    api.app.web_api_version = "2.8.14"
    monkeypatch.setattr(qbittorrent, "Client", lambda **kwargs: api)
    client = qbittorrent.QBittorrentClient("http://qbit", 8080, "user", "password")

    with raises_code("client.qbittorrent.webapi_too_old", RuntimeError):
        client.materialize_magnet(
            f"magnet:?xt=urn:btih:{metadata.hash_v1}",
            r"M:\TV",
            metadata.hash_v1,
        )

    assert api.calls == []


def test_magnet_metadata_fails_if_qbit_downloaded_payload_bytes(monkeypatch):
    metadata = parse_torrent_metadata(TORRENT)

    class UnsafeAPI(_SelectionAPI):
        def torrents_add(self, **kwargs):
            self.present = True
            self.state = "stoppedDL"
            return "Ok."

        def torrents_info(self, *, torrent_hashes):
            rows = super().torrents_info(torrent_hashes=torrent_hashes)
            if rows:
                rows[0].downloaded = 1
            return rows

        def torrents_export(self, *, torrent_hash):
            return TORRENT

    api = UnsafeAPI()
    monkeypatch.setattr(qbittorrent, "Client", lambda **kwargs: api)
    client = qbittorrent.QBittorrentClient("http://qbit", 8080, "user", "password")

    with raises_code("client.qbittorrent.payload_downloaded", RuntimeError):
        client.materialize_magnet(
            f"magnet:?xt=urn:btih:{metadata.hash_v1}",
            r"M:\TV",
            metadata.hash_v1,
        )


def test_stopped_owned_pending_magnet_is_safely_recreated_on_retry(monkeypatch):
    metadata = parse_torrent_metadata(TORRENT)

    class PendingAPI(_SelectionAPI):
        def __init__(self):
            super().__init__()
            self.present = True
            self.metadata_available = False

        def torrents_files(self, *, torrent_hash):
            return super().torrents_files(torrent_hash=torrent_hash) if self.metadata_available else []

        def torrents_delete(self, *, delete_files, torrent_hashes):
            assert delete_files is False
            self.present = False
            self.calls.append("delete-pending")

        def torrents_add(self, **kwargs):
            assert kwargs["stop_condition"] == "MetadataReceived"
            self.present = True
            self.metadata_available = True
            self.state = "stoppedDL"
            self.calls.append("add-magnet-metadata")
            return "Ok."

        def torrents_export(self, *, torrent_hash):
            return TORRENT

    api = PendingAPI()
    monkeypatch.setattr(qbittorrent, "Client", lambda **kwargs: api)
    client = qbittorrent.QBittorrentClient("http://qbit", 8080, "user", "password")

    assert (
        client.materialize_magnet(
            f"magnet:?xt=urn:btih:{metadata.hash_v1}",
            r"M:\TV",
            metadata.hash_v1,
        )
        == TORRENT
    )
    assert api.calls == ["delete-pending", "add-magnet-metadata", "stop"]


def test_hybrid_btih_magnet_resolves_to_qbit_v2_torrent_id(monkeypatch):
    info = {
        b"file tree": {b"S01E01.mkv": {b"": {b"length": 10, b"pieces root": b"r" * 32}}},
        b"files": [{b"length": 10, b"path": [b"S01E01.mkv"]}],
        b"meta version": 2,
        b"name": b"Show",
        b"piece length": 16384,
        b"pieces": b"x" * 20,
    }
    hybrid = make_torrent(info)
    metadata = parse_torrent_metadata(hybrid)

    class HybridAPI(_SelectionAPI):
        def torrents_info(self, *, torrent_hashes):
            rows = super().torrents_info(torrent_hashes=torrent_hashes)
            if rows:
                rows[0].hash = metadata.client_hash
                rows[0].infohash_v1 = metadata.hash_v1
                rows[0].infohash_v2 = metadata.hash_v2
            return rows

        def torrents_add(self, **kwargs):
            self.present = True
            self.state = "stoppedDL"
            return "Ok."

        def torrents_export(self, *, torrent_hash):
            assert torrent_hash == metadata.client_hash.lower()
            return hybrid

    api = HybridAPI()
    monkeypatch.setattr(qbittorrent, "Client", lambda **kwargs: api)
    client = qbittorrent.QBittorrentClient("http://qbit", 8080, "user", "password")

    assert (
        client.materialize_magnet(
            f"magnet:?xt=urn:btih:{metadata.hash_v1}",
            r"M:\TV",
            metadata.hash_v1,
        )
        == hybrid
    )


def test_pure_v2_btmh_magnet_uses_truncated_v2_qbit_id(monkeypatch):
    info = {
        b"file tree": {b"S01E01.mkv": {b"": {b"length": 10, b"pieces root": b"r" * 32}}},
        b"meta version": 2,
        b"name": b"Show",
        b"piece length": 16384,
    }
    pure_v2 = make_torrent(info)
    metadata = parse_torrent_metadata(pure_v2)

    class V2API(_SelectionAPI):
        def __init__(self):
            super().__init__()
            self.priorities = {4: 0}

        def torrents_info(self, *, torrent_hashes):
            rows = super().torrents_info(torrent_hashes=torrent_hashes)
            if rows:
                rows[0].hash = metadata.client_hash
            return rows

        def torrents_files(self, *, torrent_hash):
            return [
                SimpleNamespace(
                    index=4,
                    name="Show/S01E01.mkv",
                    size=10,
                    progress=0,
                    priority=self.priorities[4],
                )
            ]

        def torrents_add(self, **kwargs):
            self.present = True
            self.state = "stoppedDL"
            return "Ok."

        def torrents_export(self, *, torrent_hash):
            assert torrent_hash == metadata.client_hash.lower()
            return pure_v2

    api = V2API()
    monkeypatch.setattr(qbittorrent, "Client", lambda **kwargs: api)
    client = qbittorrent.QBittorrentClient("http://qbit", 8080, "user", "password")

    assert (
        client.materialize_magnet(
            f"magnet:?xt=urn:btmh:1220{metadata.hash_v2}",
            r"M:\TV",
            metadata.hash_v2,
        )
        == pure_v2
    )


def test_partial_selection_disables_normal_file_named_like_padding(monkeypatch):
    content = multi_file_torrent(
        b"Show",
        [
            {b"length": 10, b"path": [b".pad", b"123"]},
            {b"length": 20, b"path": [b"S01E01.mkv"]},
        ],
    )

    class PaddingNameAPI(_SelectionAPI):
        def torrents_files(self, *, torrent_hash):
            return [
                SimpleNamespace(index=4, name="Show/.pad/123", size=10, progress=0, priority=self.priorities[4]),
                SimpleNamespace(index=9, name="Show/S01E01.mkv", size=20, progress=0, priority=self.priorities[9]),
            ]

    api = PaddingNameAPI()
    monkeypatch.setattr(qbittorrent, "Client", lambda **kwargs: api)
    client = qbittorrent.QBittorrentClient("http://qbit", 8080, "user", "password")
    metadata = parse_torrent_metadata(content)

    client.add_torrent_selected(content, r"M:\TV", metadata.client_hash, [1])

    assert api.priorities == {4: 0, 9: 1}


def test_all_selection_keeps_normal_file_named_like_padding(monkeypatch):
    content = multi_file_torrent(
        b"Show",
        [
            {b"length": 10, b"path": [b".pad", b"123"]},
            {b"length": 20, b"path": [b"S01E01.mkv"]},
        ],
    )

    class PaddingNameAPI(_SelectionAPI):
        def torrents_files(self, *, torrent_hash):
            return [
                SimpleNamespace(index=4, name="Show/.pad/123", size=10, progress=0, priority=self.priorities[4]),
                SimpleNamespace(index=9, name="Show/S01E01.mkv", size=20, progress=0, priority=self.priorities[9]),
            ]

    api = PaddingNameAPI()
    monkeypatch.setattr(qbittorrent, "Client", lambda **kwargs: api)
    client = qbittorrent.QBittorrentClient("http://qbit", 8080, "user", "password")
    metadata = parse_torrent_metadata(content)

    client.add_torrent_selected(content, r"M:\TV", metadata.client_hash, [0, 1])

    assert api.priorities == {4: 1, 9: 1}


def test_hybrid_existing_v1_id_can_receive_selection_update(monkeypatch):
    info = {
        b"file tree": {
            b"S01E01.mkv": {b"": {b"length": 10, b"pieces root": b"a" * 32}},
            b"S01E02.mkv": {b"": {b"length": 20, b"pieces root": b"b" * 32}},
        },
        b"files": [
            {b"length": 10, b"path": [b"S01E01.mkv"]},
            {b"attr": b"p", b"length": 16374, b"path": [b".pad", b"16374"]},
            {b"length": 20, b"path": [b"S01E02.mkv"]},
        ],
        b"meta version": 2,
        b"name": b"Show",
        b"piece length": 16384,
        b"pieces": b"x" * 40,
    }
    hybrid = make_torrent(info)
    metadata = parse_torrent_metadata(hybrid)
    api = _SelectionAPI()
    api.present = True
    monkeypatch.setattr(qbittorrent, "Client", lambda **kwargs: api)
    client = qbittorrent.QBittorrentClient("http://qbit", 8080, "user", "password")

    client.configure_torrent_selection(hybrid, metadata.hash_v1, [1])

    assert api.priorities == {4: 0, 9: 1}


def test_hybrid_first_add_resolves_libtorrent_v1_identity(monkeypatch):
    info = {
        b"file tree": {
            b"S01E01.mkv": {b"": {b"length": 10, b"pieces root": b"a" * 32}},
            b"S01E02.mkv": {b"": {b"length": 20, b"pieces root": b"b" * 32}},
        },
        b"files": [
            {b"length": 10, b"path": [b"S01E01.mkv"]},
            {b"attr": b"p", b"length": 16374, b"path": [b".pad", b"16374"]},
            {b"length": 20, b"path": [b"S01E02.mkv"]},
        ],
        b"meta version": 2,
        b"name": b"Show",
        b"piece length": 16384,
        b"pieces": b"x" * 40,
    }
    hybrid = make_torrent(info)
    metadata = parse_torrent_metadata(hybrid)

    class V1IdentityAPI(_SelectionAPI):
        def torrents_info(self, *, torrent_hashes):
            if not self.present:
                return []
            return [
                SimpleNamespace(
                    hash=metadata.hash_v1,
                    infohash_v1=metadata.hash_v1,
                    infohash_v2="",
                    progress=0,
                    added_on=1,
                    completion_on=0,
                    save_path=r"M:\TV",
                    content_path=r"M:\TV\Show",
                    tags=self.tags,
                    state=self.state,
                )
            ]

    api = V1IdentityAPI()
    monkeypatch.setattr(qbittorrent, "Client", lambda **kwargs: api)
    client = qbittorrent.QBittorrentClient("http://qbit", 8080, "user", "password")

    result = client.add_torrent_selected(hybrid, r"M:\TV", metadata.client_hash, [metadata.files[-1].index])

    assert result["hash"] == metadata.hash_v1
    assert api.priorities == {4: 0, 9: 1}


def test_all_mode_does_not_require_decoded_path_to_match_qbit(monkeypatch):
    class SanitizedAPI(_SelectionAPI):
        def __init__(self):
            super().__init__()
            self.priorities = {4: 0}

        def torrents_files(self, *, torrent_hash):
            return [
                SimpleNamespace(
                    index=4,
                    name="Show/_____.avi",
                    size=10,
                    progress=0,
                    priority=self.priorities[4],
                )
            ]

    api = SanitizedAPI()
    monkeypatch.setattr(qbittorrent, "Client", lambda **kwargs: api)
    client = qbittorrent.QBittorrentClient("http://qbit", 8080, "user", "password")
    metadata = parse_torrent_metadata(NON_UTF_TORRENT)

    client.add_torrent_selected(NON_UTF_TORRENT, r"M:\TV", metadata.client_hash, [0])

    assert api.priorities == {4: 1}
    assert api.state == "downloading"


def test_file_mapping_matches_client_sanitized_windows_names():
    from tow.clients.qbittorrent import QBittorrentClient
    from tow.torrent import TorrentFile

    source = (
        TorrentFile(0, "E01: Pilot.mkv", 10),
        TorrentFile(1, "E02? Next.mkv", 20),
    )
    client_files = [
        {"index": 0, "name": "Space Show_ Part A/E01_ Pilot.mkv", "size": 10},
        {"index": 1, "name": "Space Show_ Part A/E02_ Next.mkv", "size": 20},
    ]

    client = QBittorrentClient.__new__(QBittorrentClient)
    assert client._map_files(source, client_files, "Space Show: Part A") == {0: 0, 1: 1}


def test_add_applies_the_configured_category_and_extra_tags(monkeypatch):
    # G8: optional qBittorrent category (created if missing) and extra tags.
    class CategoryAPI(_SelectionAPI):
        def __init__(self):
            super().__init__()
            self.add_kwargs = {}
            self.created = []

        def torrents_create_category(self, *, name):
            self.created.append(name)

        def torrents_add(self, **kwargs):
            self.add_kwargs = kwargs
            return super().torrents_add(**kwargs)

    api = CategoryAPI()
    monkeypatch.setattr(qbittorrent, "Client", lambda **kwargs: api)
    client = qbittorrent.QBittorrentClient("http://qbit", 8080, "user", "password")
    client.add_category = "tv"
    client.add_tags = ["serials", "tow"]
    metadata = parse_torrent_metadata(TORRENT)

    client.add_torrent_selected(TORRENT, r"M:\TV", metadata.infohash, [1])

    assert api.created == ["tv"]
    assert api.add_kwargs["category"] == "tv"
    assert api.add_kwargs["tags"] == "tow,tow-pending,serials"


def test_without_configuration_the_add_is_unchanged(monkeypatch):
    api = _SelectionAPI()
    seen = {}
    original = api.torrents_add
    api.torrents_add = lambda **kw: seen.update(kw) or original(**kw)
    monkeypatch.setattr(qbittorrent, "Client", lambda **kwargs: api)
    client = qbittorrent.QBittorrentClient("http://qbit", 8080, "user", "password")

    client.add_torrent_selected(TORRENT, r"M:\TV", parse_torrent_metadata(TORRENT).infohash, [1])

    assert seen["tags"] == "tow,tow-pending"
    assert "category" not in seen


def test_factory_passes_category_and_tags_from_the_client_config(monkeypatch):
    from tow.clients import factory

    monkeypatch.setattr(qbittorrent, "Client", lambda **kwargs: _SelectionAPI())
    cfg = {"client": {"kind": "qbittorrent", "category": " tv ", "tags": "serials, kids"}}
    secrets = {"qbittorrent": {"host": "http://qbit", "port": 8080, "username": "u", "password": "p"}}

    adapter = factory.from_secrets(cfg, secrets)

    assert (adapter.add_category, adapter.add_tags) == ("tv", ["serials", "kids"])


class _ListingAPI:
    """qbittorrent-api's shape where it matters here: ``app.web_api_version`` asks
    ``app_web_api_version()``, and so does every start and stop."""

    def __init__(self):
        self.calls: list[object] = []
        self.rows = {
            h: SimpleNamespace(hash=h.lower(), state="stoppedUP", progress=1.0, tags="tow")
            for h in ("A" * 40, "B" * 40)
        }
        api = self

        class App:
            version = "5.1.2"

            @property
            def web_api_version(self):
                return api.app_web_api_version()

        self.app = App()

    def app_web_api_version(self):
        self.calls.append("webapiVersion")
        return "2.11.4"

    def torrents_info(self, torrent_hashes=None, **_kwargs):
        self.calls.append(("info", torrent_hashes))
        if torrent_hashes is None:
            return list(self.rows.values())
        row = self.rows.get(torrent_hashes.upper())
        return [row] if row else []

    def torrents_files(self, *, torrent_hash):
        self.calls.append(("files", torrent_hash))
        return [SimpleNamespace(index=0, name="Show/e01.mkv", size=1, progress=1.0, priority=1)]

    def torrents_stop(self, *, torrent_hashes):
        self.app_web_api_version()  # qbittorrent-api: "stop" or "pause" by the version
        self.calls.append(("stop", torrent_hashes))
        self.rows[torrent_hashes.upper()].state = "stoppedDL"


def test_observations_of_a_run_read_one_listing_and_read_backs_stay_fresh(monkeypatch):
    # Soak: about 311 torrents/info and 30 webapiVersion requests per check of 200 topics.
    api = _ListingAPI()
    monkeypatch.setattr(qbittorrent, "Client", lambda **_kwargs: api)
    client = qbittorrent.QBittorrentClient("http://qbit", 8080, "user", "password")
    client.ping()
    assert client.observe_torrent("A" * 40)["hash"] == "a" * 40
    assert client.observe_torrent("B" * 40)["files"][0]["name"] == "Show/e01.mkv"
    listings = [call for call in api.calls if call == ("info", None)]
    assert len(listings) == 1  # one listing for both, no request per torrent
    assert ("info", "a" * 40) not in api.calls

    assert client.inspect_torrent("A" * 40) is not None  # a read-back always asks the client
    assert ("info", "a" * 40) in api.calls

    assert client.observe_torrent("C" * 40) is None  # not listed: asked on its own before "gone"
    assert ("info", "c" * 40) in api.calls

    client._stop("A" * 40)  # a change TOW makes drops the listing
    assert client.observe_torrent("A" * 40)["state"] == "stoppedDL"
    assert [call for call in api.calls if call == ("info", None)] == [("info", None)] * 2

    client._stop("B" * 40)
    assert api.calls.count("webapiVersion") == 1  # asked by the ping, then kept


def test_the_listing_is_read_again_after_a_minute(monkeypatch):
    api = _ListingAPI()
    monkeypatch.setattr(qbittorrent, "Client", lambda **_kwargs: api)
    now = [100.0]
    monkeypatch.setattr(qbittorrent.time, "monotonic", lambda: now[0])
    client = qbittorrent.QBittorrentClient("http://qbit", 8080, "user", "password")
    client.observe_torrent("A" * 40)
    now[0] += qbittorrent.LISTING_SEC + 1
    client.observe_torrent("A" * 40)
    assert [call for call in api.calls if call == ("info", None)] == [("info", None)] * 2
