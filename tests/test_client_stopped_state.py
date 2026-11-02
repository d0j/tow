"""qBittorrent's administrative stop survives a displayed file error, never an uncertain read."""

from __future__ import annotations

import pytest
from qbittorrentapi import Client
from qbittorrentapi.torrents import TorrentFilesList, TorrentInfoList
from test_qbittorrent import TORRENT, _SelectionAPI

from tow.check.client_ops import live_previous_overlap
from tow.check.space import is_stopped as space_stopped
from tow.clients import qbittorrent
from tow.clients.managed import ClientError, ManagedClient
from tow.clients.state import is_stopped, matches_hash
from tow.errors import TowError
from tow.status import TRACKER_WARNING_CLASSES, error_tone
from tow.torrent import TorrentFile, parse_torrent_metadata

H = parse_torrent_metadata(TORRENT).client_hash


@pytest.fixture(autouse=True)
def _fast_stops(monkeypatch):
    monkeypatch.setattr(ManagedClient, "PAUSE", 0)
    monkeypatch.setattr(ManagedClient, "POLLS", 3)


class ErroredAPI(_SelectionAPI):
    """Like qBittorrent: health state masks an independent stopped flag."""

    def __init__(self, state="missingFiles", *, stopped=True):
        super().__init__()
        self.present = True
        self.state = state
        self.tags = "tow"
        self.priorities = {4: 1, 9: 1}
        self.stopped = stopped
        self.filtered = []
        self.drift = None

    def torrents_info(self, *, torrent_hashes, status_filter=None):
        rows = super().torrents_info(torrent_hashes=torrent_hashes)
        if status_filter is not None:
            self.filtered.append((status_filter, torrent_hashes))
            if not self.stopped:
                return []
            if self.drift is not None:
                return self.drift(rows)
        return rows

    def torrents_stop(self, *, torrent_hashes):
        self.calls.append("stop")
        self.stopped = True  # displayed error remains


def _client(monkeypatch, api):
    monkeypatch.setattr(qbittorrent, "Client", lambda **_kwargs: api)
    return qbittorrent.QBittorrentClient("http://qbit", 8080, "user", "password")


@pytest.mark.parametrize("state", ["missingFiles", "error"])
def test_stopped_file_error_is_not_an_active_previous_revision(monkeypatch, state):
    api = ErroredAPI(state)
    client = _client(monkeypatch, api)
    info = client.inspect_torrent(H)
    assert info["state"] == state
    assert info["stopped"] is True
    assert is_stopped(info)
    assert space_stopped(info)
    assert ManagedClient._stopped(info)
    assert not live_previous_overlap(client, [H], (TorrentFile(0, "Show/S01E01.mkv", 10),))
    assert client.stop_owned_torrent(H)["state"] == state
    assert api.calls == []  # already stopped: neither stop nor restart to clear the error


@pytest.mark.parametrize("state", ["missingFiles", "error"])
def test_stop_readback_can_confirm_containment_while_health_error_remains(monkeypatch, state):
    api = ErroredAPI(state, stopped=False)
    client = _client(monkeypatch, api)
    monkeypatch.setattr(ManagedClient, "PAUSE", 0)
    monkeypatch.setattr(ManagedClient, "POLLS", 3)
    assert not is_stopped(client.inspect_torrent(H))
    confirmed = client.stop_owned_torrent(H)
    assert is_stopped(confirmed)
    assert confirmed["state"] == state
    assert api.calls == ["stop"]


@pytest.mark.parametrize("drift", ["hash", "owner", "folder", "duplicate", "gone", "running", "unknown"])
def test_uncertain_filtered_answer_cannot_confirm_stopped(monkeypatch, drift):
    api = ErroredAPI()

    def changed(rows):
        if drift == "gone":
            return []
        if drift == "duplicate":
            return rows * 2
        if drift in {"running", "unknown"}:
            rows[0].state = "downloading" if drift == "running" else "unknown"
            return rows
        setattr(
            rows[0],
            {"hash": "hash", "owner": "tags", "folder": "save_path"}[drift],
            {"hash": "B" * 40, "owner": "manual", "folder": "M:/other"}[drift],
        )
        return rows

    api.drift = changed
    client = _client(monkeypatch, api)
    assert not is_stopped(client.inspect_torrent(H))
    with pytest.raises(ClientError) as error:
        client.stop_owned_torrent(H)
    assert error.value.code == "client.managed.error_state"


def test_filtered_transport_failure_never_becomes_stopped_evidence(monkeypatch):
    api = ErroredAPI()

    def unavailable(_rows):
        raise ConnectionError("synthetic filtered read failure")

    api.drift = unavailable
    client = _client(monkeypatch, api)
    with pytest.raises(ConnectionError, match="filtered read"):
        client.inspect_torrent(H)
    assert api.calls == []


@pytest.mark.parametrize(
    ("drift", "code"),
    [
        ("owner", "client.managed.not_owned_stop"),
        ("folder", "client.managed.wrong_folder"),
        ("hash", "client.managed.stop_unconfirmed"),
    ],
)
def test_a_stop_does_not_confirm_a_different_owner_folder_or_identity(monkeypatch, drift, code):
    api = ErroredAPI(stopped=False)
    stop = api.torrents_stop
    inspect = api.torrents_info

    def changed(torrent_hashes):
        stop(torrent_hashes=torrent_hashes)
        if drift == "owner":
            api.tags = "manual"

    def rows(**kwargs):
        result = inspect(**kwargs)
        if api.calls and result:
            if drift == "folder":
                result[0].save_path = "M:/different"
            elif drift == "hash":
                result[0].hash = "C" * 40
        return result

    monkeypatch.setattr(api, "torrents_stop", changed)
    monkeypatch.setattr(api, "torrents_info", rows)
    client = _client(monkeypatch, api)
    with pytest.raises(ClientError) as error:
        client.stop_owned_torrent(H)
    assert error.value.code == code
    assert api.calls == ["stop"]


@pytest.mark.parametrize("alias", ["v2", "short_v2"])
def test_filtered_evidence_uses_the_canonical_hash_for_a_hybrid_alias(monkeypatch, alias):
    api = ErroredAPI()
    v2 = "F" * 64
    inspect = api.torrents_info

    def rows(**kwargs):
        result = inspect(**kwargs)
        for row in result:
            row.hash, row.infohash_v1, row.infohash_v2 = H, H, v2
        return result

    monkeypatch.setattr(api, "torrents_info", rows)
    client = _client(monkeypatch, api)
    assert is_stopped(client.inspect_torrent(v2 if alias == "v2" else v2[:40]))
    assert api.filtered == [("stopped", H.lower())]


@pytest.mark.parametrize("state", ["unknown", "downloading", "checkingDL", "moving", ""])
def test_a_stopped_flag_cannot_disguise_an_unknown_or_running_state(state):
    assert not is_stopped({"state": state, "stopped": True})


@pytest.mark.parametrize("state", ["", "unknown"])
def test_uncertain_previous_state_still_blocks_overlapping_files(monkeypatch, state):
    client = _client(monkeypatch, ErroredAPI(state))
    assert live_previous_overlap(client, [H], (TorrentFile(0, "Show/S01E01.mkv", 10),))


@pytest.mark.parametrize("missing_hash", [None, ""])
def test_a_stopped_row_without_hash_evidence_cannot_be_stopped_or_replaced(monkeypatch, missing_hash):
    api = ErroredAPI("stoppedUP")
    inspect = api.torrents_info

    def rows(**kwargs):
        result = inspect(**kwargs)
        result[0].hash = missing_hash
        return result

    monkeypatch.setattr(api, "torrents_info", rows)
    client = _client(monkeypatch, api)
    assert client.inspect_torrent(H)["hash"] == ""
    with pytest.raises(ClientError) as error:
        client.stop_owned_torrent(H)
    assert error.value.code == "client.managed.stop_unconfirmed"
    assert api.calls == []


@pytest.mark.parametrize("missing", ["identity", "files", "name", "priority"])
def test_uncertain_previous_identity_or_files_cannot_be_treated_as_absent(missing):
    row = {"hash": H, "state": "downloading", "files": [{"name": "Show/S01E01.mkv", "priority": 1}]}
    if missing == "identity":
        row["hash"] = "C" * 40
    elif missing == "files":
        row["files"] = []
    elif missing == "name":
        row["files"][0]["name"] = ""
    else:
        row["files"][0]["priority"] = "invalid"

    class UncertainClient:
        def inspect_torrent(self, _h):
            return row

    with pytest.raises(TowError) as error:
        live_previous_overlap(UncertainClient(), [H], (TorrentFile(0, "Show/S01E01.mkv", 10),))
    assert error.value.code == "check.previous_revision_unconfirmed"
    assert error.value.error_class == "qbit"
    assert error.value.error_class not in TRACKER_WARNING_CLASSES


def test_previous_revision_uncertainty_is_an_actionable_client_error():
    error = TowError("check.previous_revision_unconfirmed")
    assert error.error_class == "qbit"
    assert error_tone(error.error_class) == "bad"


@pytest.mark.parametrize("field", ["hash", "infohash_v1", "infohash_v2"])
def test_only_an_explicit_v2_identity_authorizes_its_short_client_id(field):
    v2 = "F" * 64
    info = {"hash": "A" * 40, field: v2}
    assert matches_hash(info, v2)
    assert matches_hash(info, v2[:40]) is (field == "infohash_v2")


@pytest.mark.parametrize("field", ["hash", "infohash_v1", "infohash_v2"])
def test_verification_receipts_use_the_same_explicit_v2_identity_boundary(field):
    v2 = "F" * 64
    files = [{"index": 0, "name": "Show/e01.mkv", "size": 10, "priority": 1}]
    info = {"hash": "A" * 40, "state": "stoppedDL", "tags": ["tow"], "save_path": "D:/TV", "files": files, field: v2}
    receipt = {"hash": v2[:40], "files": files, "wanted": [0], "ignored": []}

    class ReportedClient(ManagedClient):
        def inspect_torrent(self, _h):
            return info

    client = ReportedClient()
    if field == "infohash_v2":
        assert client._verify_receipt(v2[:40], "D:/TV", receipt, require_pending=False) == info
    else:
        with pytest.raises(ClientError) as error:
            client._verify_receipt(v2[:40], "D:/TV", receipt, require_pending=False)
        assert error.value.code == "client.managed.hash_changed_selection"


@pytest.mark.parametrize("operation", ["add", "start", "selection"])
def test_independent_stop_does_not_approve_an_unsafe_add_start_or_selection(monkeypatch, operation):
    api = ErroredAPI()
    client = _client(monkeypatch, api)
    monkeypatch.setattr(ManagedClient, "PAUSE", 0)
    if operation == "add":
        api.present = False
    operations = {
        "add": lambda: client.add_torrent_selected(TORRENT, r"M:\TV", H, [0]),
        "start": lambda: client.start_owned_torrent(H),
        "selection": lambda: client.configure_torrent_selection(TORRENT, H, [0]),
    }
    with pytest.raises(ClientError) as error:
        operations[operation]()
    assert error.value.code == "client.managed.error_state"
    assert "start" not in api.calls
    assert not any(isinstance(call, tuple) and call[0] == "priority" for call in api.calls)


@pytest.mark.parametrize(("version", "expected"), [("2.8.3", "paused"), ("2.11.0", "stopped"), ("2.15.1", "stopped")])
def test_pinned_api_library_uses_the_supported_native_filter(monkeypatch, version, expected):
    api = Client(host="http://qbit")
    monkeypatch.setattr(api, "app_web_api_version", lambda: version)
    row = {"hash": H.lower(), "state": "missingFiles", "tags": "tow", "save_path": "M:/TV", "progress": 0}
    filters = []

    def post(**kwargs):
        if kwargs["_method"] == "files":
            return TorrentFilesList([], client=None)
        assert kwargs["_method"] == "info"
        filters.append(kwargs["data"]["filter"])
        return TorrentInfoList([dict(row)], client=None)

    monkeypatch.setattr(api, "_post_cast", post)
    client = _client(monkeypatch, api)
    assert client.inspect_torrent(H)["stopped"] is True
    assert filters == [None, expected]
