"""Native RPC stop evidence, local health and unknown states stay separate for every client."""

from __future__ import annotations

import pytest
from test_clients_managed import META, TORRENT, FakeDeluge, FakeTransmission, H, K, Torrent, _deluge, _transmission

from tow.check.client_ops import live_previous_overlap
from tow.clients.managed import ClientError, ManagedClient
from tow.clients.state import is_stopped

_MISSING = object()
_UNCHANGED = object()


@pytest.fixture(autouse=True)
def _short_waits(monkeypatch):
    monkeypatch.setattr(ManagedClient, "PAUSE", 0)
    monkeypatch.setattr(ManagedClient, "POLLS", 3)
    monkeypatch.setattr(ManagedClient, "START_POLLS", 3)


class LocalErrorTransmission(FakeTransmission):
    def __init__(self, *, status=_UNCHANGED, error=3):
        super().__init__()
        self.status, self.error = status, error

    def _row(self, torrent):
        row = super()._row(torrent)
        if self.status is _MISSING:
            row.pop("status")
        elif self.status is not _UNCHANGED:
            row["status"] = self.status
        if self.error is _MISSING:
            row.pop("error")
        else:
            row["error"] = self.error
        return row


def _transmission_error(*, stopped=True, status=_UNCHANGED, error=3):
    server = LocalErrorTransmission(status=status, error=error)
    server.torrents[K] = Torrent(TORRENT, "D:/TV", ["tow"], paused=stopped)
    return _transmission(server), server


def test_transmission_stopped_local_error_is_not_a_running_previous_revision():
    client, server = _transmission_error()
    info = client.inspect_torrent(H)
    assert info["state"] == "error"
    assert info["stopped"] is True
    assert is_stopped(info)
    assert live_previous_overlap(client, [H], META.files) == ""
    assert client.stop_owned_torrent(H)["state"] == "error"
    assert not ({"torrent-stop", "torrent-start", "torrent-set"} & set(server.calls))


def test_transmission_stop_confirms_status_zero_without_claiming_file_health():
    client, server = _transmission_error(stopped=False)
    assert live_previous_overlap(client, [H], META.files)
    info = client.stop_owned_torrent(H)
    assert info["state"] == "error"
    assert info["stopped"] is True
    assert server.calls.count("torrent-stop") == 1
    assert "torrent-start" not in server.calls
    assert client._verification_state(H)["stopped"] is True
    assert client._verification_state(H)["state"] == "error"


@pytest.mark.parametrize("status", [_MISSING, None, False, True, "0", "invalid", 0.0, -1, 7, [], {}])
@pytest.mark.parametrize("error", [0, 3])
def test_transmission_unknown_status_never_coerces_to_stopped(status, error):
    client, server = _transmission_error(status=status, error=error)
    info = client.inspect_torrent(H)
    assert info["state"] == ("error" if error == 3 else "unknown")
    assert info["stopped"] is False
    assert not is_stopped(info)
    brief = client._verification_state(H)
    assert brief["state"] == info["state"]
    assert brief["stopped"] is False
    assert live_previous_overlap(client, [H], META.files)
    with pytest.raises(ClientError) as failure:
        client.start_owned_torrent(H)
    assert failure.value.code == "client.managed.error_state"
    assert "torrent-start" not in server.calls


@pytest.mark.parametrize("error", [_MISSING, None, False, True, "0", "3", 0.0, 4, -1])
def test_transmission_malformed_health_does_not_confirm_an_ordinary_stopped_state(error):
    client, _server = _transmission_error(status=0, error=error)
    info = client.inspect_torrent(H)
    assert info["state"] == "unknown"
    assert not is_stopped(info)


@pytest.mark.parametrize(
    ("status", "state"),
    [(1, "checkingDL"), (2, "checkingDL"), (3, "queuedDL"), (4, "downloading"), (5, "queuedUP"), (6, "uploading")],
)
def test_transmission_queue_and_preparation_are_not_administratively_stopped(status, state):
    client, _server = _transmission_error(status=status, error=0)
    assert client.inspect_torrent(H)["state"] == state
    assert not is_stopped(client.inspect_torrent(H))


@pytest.mark.parametrize("operation", ["start", "selection", "add"])
def test_transmission_local_error_stays_unsafe_for_start_selection_and_add(operation):
    client, server = _transmission_error()
    if operation == "add":
        server.torrents.clear()
    action = {
        "start": lambda: client.start_owned_torrent(H),
        "selection": lambda: client.configure_torrent_selection(TORRENT, H, [0]),
        "add": lambda: client.add_torrent_selected(TORRENT, "D:/TV", H, [0]),
    }[operation]
    with pytest.raises(ClientError) as failure:
        action()
    assert failure.value.code == "client.managed.error_state"
    assert "torrent-start" not in server.calls
    if operation != "add":
        assert "torrent-set" not in server.calls


class DelugeWithPausedFlags(FakeDeluge):
    def __init__(self, *, paused=True, auto_managed=False, state="Error"):
        super().__init__()
        self.paused, self.auto_managed, self.state = paused, auto_managed, state
        self.torrents[K] = Torrent(TORRENT, "D:/TV", ["tow"], paused=True)

    def m_core_get_torrent_status(self, torrent_id, keys):
        row = super().m_core_get_torrent_status(torrent_id, keys)
        # Supply all available raw RPC evidence, including fields not requested by
        # today's adapter: none proves native non-auto-management in Deluge 2.2.
        row.update(
            state=self.state, paused=self.paused, auto_managed=self.auto_managed, is_auto_managed=self.auto_managed
        )
        return row


@pytest.mark.parametrize(
    ("paused", "auto_managed"),
    [(True, False), (True, True), (True, None), (False, False), (None, False), (1, False), (True, 0)],
)
def test_deluge_error_and_paused_flags_alone_never_invent_native_stop_proof(paused, auto_managed):
    server = DelugeWithPausedFlags(paused=paused, auto_managed=auto_managed)
    client = _deluge(server)
    info = client.inspect_torrent(H)
    assert info["state"] == "error"
    assert not is_stopped(info)
    assert live_previous_overlap(client, [H], META.files)
    with pytest.raises(ClientError) as failure:
        client.stop_owned_torrent(H)
    assert failure.value.code == "client.managed.error_state"


@pytest.mark.parametrize("state", ["Queued", "Checking", "Allocating", "Moving", "Unknown", "", None])
def test_deluge_queue_maintenance_and_unknown_states_do_not_become_stopped_from_paused_flags(state):
    client = _deluge(DelugeWithPausedFlags(state=state))
    assert not is_stopped(client.inspect_torrent(H))
    assert live_previous_overlap(client, [H], META.files)


@pytest.mark.parametrize("operation", ["start", "selection", "add"])
def test_deluge_paused_error_stays_unsafe_for_start_selection_and_add(operation):
    server = DelugeWithPausedFlags()
    client = _deluge(server)
    if operation == "add":
        server.torrents.clear()
    action = {
        "start": lambda: client.start_owned_torrent(H),
        "selection": lambda: client.configure_torrent_selection(TORRENT, H, [0]),
        "add": lambda: client.add_torrent_selected(TORRENT, "D:/TV", H, [0]),
    }[operation]
    with pytest.raises(ClientError) as failure:
        action()
    assert failure.value.code == "client.managed.error_state"
    assert "core.resume_torrents" not in server.calls
    assert "core.set_torrent_options" not in server.calls
