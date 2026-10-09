"""The shared ManagedClient contract against a minimal in-memory client.

Transmission and Deluge are exercised end to end in test_clients_managed.py; these tests drive
the contract's rarer branches (rollback of a failed selection change, refusals, read-back
timeouts) through a fake that implements only the primitives and records every call.
"""

from __future__ import annotations

import copy
import math
from typing import Any

import pytest
from helpers import make_torrent, multi_file_torrent

from tow.clients import managed
from tow.clients.managed import OWNER, PENDING, ClientError, ManagedClient
from tow.i18n import t
from tow.torrent import parse_torrent_metadata

PIECE = 16384
SIZES = {"e01.mkv": 40000, "e02.mkv": 30000, "info.nfo": 100}
TORRENT = make_torrent(
    {
        b"name": "Show S01",
        b"piece length": PIECE,
        b"pieces": b"\0" * 20 * math.ceil(sum(SIZES.values()) / PIECE),
        b"files": [{b"length": size, b"path": [name]} for name, size in SIZES.items()],
    }
)
META = parse_torrent_metadata(TORRENT)
H = META.client_hash.upper()
E01, E02, NFO = (next(f.index for f in META.files if f.path.endswith(name)) for name in SIZES)


def _msg(key: str, **params: Any) -> str:
    return f"Fake: {t(key, **params)}"


class FakeClient(ManagedClient):
    """A client that keeps torrents in memory; client file ids are the torrent's in reverse."""

    title = "Fake"
    POLLS = 3
    PAUSE = 0

    def __init__(self) -> None:
        self.torrents: dict[str, dict[str, Any]] = {}
        self.calls: list[tuple] = []
        self.start_works = True
        self.stop_works = True
        self.labels_work = True
        self.add_running = False

    # --- primitives -------------------------------------------------------------------------

    def ping(self) -> str:
        return "fake 1.0"

    def inspect_torrent(self, infohash: str) -> dict[str, Any] | None:
        row = self.torrents.get(str(infohash).upper())
        return copy.deepcopy(row) if row is not None else None

    def _add_stopped(self, content: bytes, save_path: str, labels: list[str]) -> None:
        meta = parse_torrent_metadata(content)
        self.calls.append(("add", save_path, tuple(labels)))
        count = len(meta.files)
        self.torrents[meta.client_hash.upper()] = {
            "hash": meta.client_hash.upper(),
            "state": "downloading" if self.add_running else "stoppedDL",
            "progress": 0.0,
            "save_path": save_path,
            "tags": list(labels),
            "files": [
                {"index": count - 1 - f.index, "name": f"{meta.name}/{f.path}", "size": f.size, "priority": 1}
                for f in meta.files
            ],
        }

    def _set_wanted(self, infohash: str, wanted: set[int], all_ids: list[int]) -> None:
        self.calls.append(("set_wanted", frozenset(wanted), tuple(all_ids)))
        for row in self.torrents[infohash.upper()]["files"]:
            if row["index"] in all_ids:
                row["priority"] = 1 if row["index"] in wanted else 0

    def _stop(self, infohash: str) -> None:
        self.calls.append(("stop",))
        if self.stop_works:
            self.torrents[infohash.upper()]["state"] = "stoppedDL"

    def _start(self, infohash: str) -> None:
        self.calls.append(("start",))
        if self.start_works:
            self.torrents[infohash.upper()]["state"] = "downloading"

    def _set_labels(self, infohash: str, labels: list[str]) -> None:
        self.calls.append(("labels", tuple(labels)))
        if self.labels_work:
            self.torrents[infohash.upper()]["tags"] = list(labels)

    def _move(self, infohash: str, save_path: str) -> None:
        self.calls.append(("move", save_path))
        self.torrents[infohash.upper()]["save_path"] = save_path

    # --- helpers ----------------------------------------------------------------------------

    def seed(self, *, tags: list[str], state: str, wanted: set[int] | None = None, path: str = "D:/tv") -> None:
        """Put the test torrent in the client as if it were added earlier."""
        self._add_stopped(TORRENT, path, tags)
        row = self.torrents[H]
        row["state"] = state
        if wanted is not None:
            ids = {self.client_id_of(index) for index in wanted}
            for file in row["files"]:
                file["priority"] = 1 if file["index"] in ids else 0
        self.calls.clear()

    @staticmethod
    def client_id_of(torrent_index: int) -> int:
        return len(META.files) - 1 - torrent_index

    def priorities(self) -> dict[int, int]:
        return {row["index"]: row["priority"] for row in self.torrents[H]["files"]}

    def wanted_torrent_indices(self) -> set[int]:
        count = len(META.files)
        return {count - 1 - row["index"] for row in self.torrents[H]["files"] if row["priority"] > 0}


@pytest.fixture
def client() -> FakeClient:
    return FakeClient()


# The start, ownership and rollback waits every client shares: test_clients_managed.py.


def test_ownership_wait_preserves_inspection_error(client, monkeypatch):
    original = ClientError("client.managed.no_files", prefix="Fake")

    def inspect(_hash):
        raise original

    monkeypatch.setattr(client, "inspect_torrent", inspect)
    with pytest.raises(ClientError) as error:
        client._wait_owned(H)
    assert error.value is original


def test_rollback_reports_unconfirmed_restart_without_hiding_original(client, monkeypatch, caplog):
    client.seed(tags=[OWNER], state="downloading", wanted={E01})
    client.start_works = False
    real_set_wanted = client._set_wanted
    writes = 0

    def set_wanted(infohash, wanted, all_ids):
        nonlocal writes
        writes += 1
        if writes > 1:
            real_set_wanted(infohash, wanted, all_ids)

    monkeypatch.setattr(client, "_set_wanted", set_wanted)
    with pytest.raises(ClientError) as error:
        client.configure_torrent_selection(TORRENT, H, [E02])
    assert error.value.code == "client.managed.wrong_selection"
    assert client.wanted_torrent_indices() == {E01}
    assert _msg("client.managed.start_unconfirmed") in caplog.text


def test_rollback_waits_for_delayed_stop_before_restoring_files(client, monkeypatch):
    client.seed(tags=[OWNER], state="downloading", wanted={E01})
    real_stop = client._stop
    real_inspect = client.inspect_torrent
    real_set = client._set_wanted
    stops = writes = waiting_reads = 0

    def stop(infohash):
        nonlocal stops
        stops += 1
        real_stop(infohash)
        if stops == 2:
            client.torrents[H]["state"] = "downloading"

    def inspect(infohash):
        nonlocal waiting_reads
        if stops == 2 and waiting_reads < 2:
            waiting_reads += 1
            if waiting_reads == 2:
                client.torrents[H]["state"] = "stoppedDL"
        return real_inspect(infohash)

    def set_wanted(infohash, wanted, ids):
        nonlocal writes
        writes += 1
        if writes > 1:
            assert client.torrents[H]["state"] == "stoppedDL"
            real_set(infohash, wanted, ids)

    monkeypatch.setattr(client, "_stop", stop)
    monkeypatch.setattr(client, "inspect_torrent", inspect)
    monkeypatch.setattr(client, "_set_wanted", set_wanted)
    with pytest.raises(ClientError) as error:
        client.configure_torrent_selection(TORRENT, H, [E02])
    assert error.value.code == "client.managed.wrong_selection"
    assert waiting_reads == 2
    assert client.wanted_torrent_indices() == {E01}
    assert client.torrents[H]["state"] == "downloading"


# --- configure_torrent_selection: refusals ----------------------------------------------------


def test_selection_change_for_another_torrent_is_refused_before_touching_the_client(client):
    client.seed(tags=[OWNER], state="downloading")
    with pytest.raises(ClientError) as error:
        client.configure_torrent_selection(TORRENT, "0" * 40, [E01])
    assert str(error.value) == _msg("client.managed.hash_changed_selection")
    assert client.calls == []


def test_selection_change_of_a_pure_v2_torrent_accepts_only_its_own_id(client):
    pure_v2 = make_torrent(
        {
            b"file tree": {b"S01E01.mkv": {b"": {b"length": 10, b"pieces root": b"r" * 32}}},
            b"meta version": 2,
            b"name": b"Show",
            b"piece length": PIECE,
        }
    )
    assert parse_torrent_metadata(pure_v2).hash_v1 is None
    with pytest.raises(ClientError) as error:
        client.configure_torrent_selection(pure_v2, H, [0])
    assert str(error.value) == _msg("client.managed.hash_changed_selection")


def test_selection_change_of_a_foreign_torrent_is_refused_and_nothing_changes(client):
    client.seed(tags=["manual"], state="downloading", wanted={E01, E02, NFO})
    before = client.priorities()
    with pytest.raises(ClientError) as error:
        client.configure_torrent_selection(TORRENT, H, [E01])
    assert str(error.value) == _msg("client.managed.not_owned")
    assert client.calls == []
    assert client.priorities() == before
    assert client.torrents[H]["state"] == "downloading"


def test_selection_change_of_a_torrent_whose_files_never_appear_fails_closed(client):
    client.seed(tags=[OWNER], state="downloading")
    client.torrents[H]["files"] = []
    with pytest.raises(ClientError) as error:
        client.configure_torrent_selection(TORRENT, H, [E01])
    assert str(error.value) == _msg("client.managed.no_files")
    assert client.calls == []


# --- configure_torrent_selection: success branches --------------------------------------------


def test_stopped_owned_torrent_gets_the_new_selection_and_stays_stopped(client):
    client.seed(tags=[OWNER], state="stoppedDL", wanted={E01})
    info = client.configure_torrent_selection(TORRENT, H, [E02, NFO])
    assert client.wanted_torrent_indices() == {E02, NFO}
    assert client.torrents[H]["state"] == "stoppedDL"
    assert ("start",) not in client.calls
    assert info["state"] == "stoppedDL"


def test_stopped_owned_torrent_is_started_when_asked(client):
    client.seed(tags=[OWNER], state="pausedDL", wanted={E01})
    info = client.configure_torrent_selection(TORRENT, H, [E02], ensure_started=True)
    assert info["state"] == "downloading"
    assert client.calls[-1] == ("start",)


def test_pending_torrent_is_started_and_released(client):
    client.seed(tags=[OWNER, PENDING], state="stoppedDL", wanted={E01})
    info = client.configure_torrent_selection(TORRENT, H, [E01, E02])
    assert info["tags"] == [OWNER]
    assert info["state"] == "downloading"
    assert client.wanted_torrent_indices() == {E01, E02}


def test_a_pending_torrent_whose_files_do_not_fit_is_released_but_stays_stopped(client):
    # An unfinished add of TOW's was always started when its add was finished: one added
    # stopped because its files did not fit started without the room.
    client.seed(tags=[OWNER, PENDING], state="stoppedDL", wanted={E01})
    info = client.configure_torrent_selection(TORRENT, H, [E01, E02], keep_stopped=True)
    assert info["tags"] == [OWNER]  # the add is finished...
    assert info["state"] == "stoppedDL"  # ...and waits for room
    assert ("start",) not in client.calls
    assert client.wanted_torrent_indices() == {E01, E02}


# --- configure_torrent_selection: rollback ----------------------------------------------------


def test_failed_selection_of_a_running_torrent_restores_previous_files_and_restarts(client, monkeypatch):
    client.seed(tags=[OWNER], state="downloading", wanted={E01, NFO})
    before = client.priorities()
    real_set_wanted = client._set_wanted
    attempts: list[frozenset] = []

    def set_wanted(infohash, wanted, all_ids):
        attempts.append(frozenset(wanted))
        if len(attempts) == 1:
            client.calls.append(("set_wanted ignored", frozenset(wanted), tuple(all_ids)))
            return  # the client ignores the new selection: read-back mismatch
        real_set_wanted(infohash, wanted, all_ids)

    monkeypatch.setattr(client, "_set_wanted", set_wanted)
    with pytest.raises(ClientError) as error:
        client.configure_torrent_selection(TORRENT, H, [E02])
    assert str(error.value) == _msg("client.managed.wrong_selection")
    # The rollback stops, re-applies the previous wanted set over every client id, then
    # restarts because the torrent was running before.
    previous_ids = frozenset(cid for cid, priority in before.items() if priority > 0)
    assert attempts == [frozenset({client.client_id_of(E02)}), previous_ids]
    assert client.calls[-3:] == [("stop",), ("set_wanted", previous_ids, tuple(sorted(before))), ("start",)]
    assert client.priorities() == before
    assert client.torrents[H]["state"] == "downloading"


def test_failed_start_of_a_stopped_torrent_rolls_back_without_starting_it(client):
    client.seed(tags=[OWNER], state="stoppedDL", wanted={E01})
    before = client.priorities()
    client.start_works = False  # the client accepts the start but never starts
    with pytest.raises(ClientError) as error:
        client.configure_torrent_selection(TORRENT, H, [E02, NFO], ensure_started=True)
    assert str(error.value) == _msg("client.managed.start_unconfirmed")
    assert client.priorities() == before
    rollback = client.calls[client.calls.index(("start",)) + 1 :]
    assert rollback == [("stop",), ("set_wanted", frozenset({client.client_id_of(E01)}), tuple(sorted(before)))]
    assert client.torrents[H]["state"] == "stoppedDL"


def test_selection_that_points_outside_the_torrent_is_refused_and_rolled_back(client):
    client.seed(tags=[OWNER], state="downloading", wanted={E01})
    before = client.priorities()
    with pytest.raises(ClientError) as error:
        client.configure_torrent_selection(TORRENT, H, [])
    assert str(error.value) == _msg("client.managed.bad_selection")
    assert client.priorities() == before
    assert client.calls[-1] == ("start",)  # it was running


def test_failure_after_release_was_requested_is_not_rolled_back(client):
    client.seed(tags=[OWNER, PENDING], state="stoppedDL", wanted={E01})
    client.labels_work = False  # the pending marker never goes away
    with pytest.raises(ClientError) as error:
        client.configure_torrent_selection(TORRENT, H, [E02])
    assert str(error.value) == _msg("client.managed.pending_not_cleared")
    # The new selection was confirmed and the torrent started: no stop, no restore.
    assert client.wanted_torrent_indices() == {E02}
    assert client.torrents[H]["state"] == "downloading"
    after_labels = client.calls[client.calls.index(next(c for c in client.calls if c[0] == "labels")) :]
    assert ("stop",) not in after_labels
    assert not any(call[0] == "set_wanted" for call in after_labels)


def test_a_failing_rollback_does_not_hide_the_original_error(client, monkeypatch):
    client.seed(tags=[OWNER], state="downloading", wanted={E01})
    stops = []

    def stop(infohash):
        stops.append(infohash)
        if len(stops) == 1:
            client.torrents[H]["state"] = "stoppedDL"
            return
        raise ConnectionError("client went away during the rollback")

    monkeypatch.setattr(client, "_stop", stop)
    monkeypatch.setattr(client, "_set_wanted", lambda *_a: None)  # mismatch on read-back
    with pytest.raises(ClientError) as error:
        client.configure_torrent_selection(TORRENT, H, [E02])
    assert str(error.value) == _msg("client.managed.wrong_selection")
    assert len(stops) == 2


def test_an_unsafe_state_during_the_change_is_reported_and_rolled_back(client, monkeypatch):
    client.seed(tags=[OWNER], state="downloading", wanted={E01})

    def stop(infohash):
        client.calls.append(("stop",))
        client.torrents[H]["state"] = "missingFiles"

    monkeypatch.setattr(client, "_stop", stop)
    with pytest.raises(ClientError) as error:
        client.configure_torrent_selection(TORRENT, H, [E02])
    assert str(error.value) == _msg("client.managed.error_state", state="missingfiles")
    assert ("start",) not in client.calls  # an unsafe rollback is never resumed


def test_an_unconfirmed_selection_rollback_never_restarts(client, monkeypatch):
    client.seed(tags=[OWNER], state="downloading", wanted={E01})

    def set_wanted(*_args):
        client.calls.append(("set_wanted ignored",))
        for row in client.torrents[H]["files"]:
            row["priority"] = 0

    monkeypatch.setattr(client, "_set_wanted", set_wanted)
    with pytest.raises(ClientError) as error:
        client.configure_torrent_selection(TORRENT, H, [E02])
    assert str(error.value) == _msg("client.managed.wrong_selection")
    assert ("start",) not in client.calls


# --- add_torrent_selected: rarer branches ----------------------------------------------------


def test_add_refuses_a_torrent_whose_hash_changed(client):
    with pytest.raises(ClientError) as error:
        client.add_torrent_selected(TORRENT, "D:/tv", "0" * 40, [E01])
    assert str(error.value) == _msg("client.managed.hash_changed_add")
    assert client.calls == []


def test_add_that_the_client_started_anyway_is_stopped_before_selection(client):
    client.add_running = True
    info = client.add_torrent_selected(TORRENT, "D:/tv", H, [E02])
    names = [call[0] for call in client.calls]
    assert names.index("stop") < names.index("set_wanted") < names.index("start")
    assert info["tags"] == [OWNER]
    assert client.wanted_torrent_indices() == {E02}


def test_add_with_category_and_extra_tags_labels_them_once(client):
    client.add_category = " tv "
    client.add_tags = ["kids", " ", OWNER]
    client.add_torrent_selected(TORRENT, "D:/tv", H, [E01])
    assert client.calls[0] == ("add", "D:/tv", (OWNER, PENDING, "tv", "kids"))


def test_add_that_never_becomes_visible_is_not_confirmed(client, monkeypatch):
    monkeypatch.setattr(client, "_add_stopped", lambda *_a: client.calls.append(("add",)))
    with pytest.raises(ClientError) as error:
        client.add_torrent_selected(TORRENT, "D:/tv", H, [E01])
    assert str(error.value) == _msg("client.managed.not_visible")


def test_add_without_the_owner_mark_is_not_confirmed(client, monkeypatch):
    real_add = client._add_stopped
    monkeypatch.setattr(client, "_add_stopped", lambda content, path, _labels: real_add(content, path, []))
    with pytest.raises(ClientError) as error:
        client.add_torrent_selected(TORRENT, "D:/tv", H, [E01])
    assert str(error.value) == _msg("client.managed.no_owner_mark")
    assert not any(call[0] in {"stop", "start", "set_wanted", "labels"} for call in client.calls)


def test_selection_readback_cannot_hide_an_unwanted_file(client, monkeypatch):
    client.seed(tags=[OWNER], state="stoppedDL")
    real_inspect = client.inspect_torrent

    def inspect(infohash):
        info = real_inspect(infohash)
        if any(call[0] == "set_wanted" for call in client.calls):
            info["files"] = [row for row in info["files"] if row["index"] == client.client_id_of(E01)]
        return info

    monkeypatch.setattr(client, "inspect_torrent", inspect)
    with pytest.raises(ClientError):
        client._apply_selection(H, META.files, {E01}, META.name)


def test_selection_failure_after_ownership_loss_never_restores_or_restarts(client, monkeypatch):
    client.seed(tags=[OWNER], state="downloading", wanted={E01})

    def set_wanted(*_args):
        client.calls.append(("set_wanted",))
        client.torrents[H]["tags"] = ["manual"]
        raise ConnectionError("selection connection failed")

    monkeypatch.setattr(client, "_set_wanted", set_wanted)
    with pytest.raises(ConnectionError, match="selection connection failed"):
        client.configure_torrent_selection(TORRENT, H, [E02])
    assert client.calls == [("stop",), ("set_wanted",)]


def test_add_whose_stop_is_not_confirmed_is_stopped_again_and_fails(client):
    client.add_running = True
    client.stop_works = False
    with pytest.raises(ClientError) as error:
        client.add_torrent_selected(TORRENT, "D:/tv", H, [E01])
    assert str(error.value) == _msg("client.managed.stop_unconfirmed")
    assert client.calls[-1] == ("stop",)  # cleanup after the failure


def test_add_cleanup_failure_keeps_the_original_error(client, monkeypatch):
    def stop(_infohash):
        raise ConnectionError("gone")

    client.add_running = True
    monkeypatch.setattr(client, "_stop", stop)
    with pytest.raises(ConnectionError, match="gone"):
        client.add_torrent_selected(TORRENT, "D:/tv", H, [E01])


def test_add_whose_marker_is_not_cleared_is_not_stopped_after_the_start(client):
    client.labels_work = False
    with pytest.raises(ClientError) as error:
        client.add_torrent_selected(TORRENT, "D:/tv", H, [E01])
    assert str(error.value) == _msg("client.managed.pending_not_cleared")
    assert client.torrents[H]["state"] == "downloading"
    assert client.calls[-1][0] == "labels"


def test_add_skips_the_padding_flag_of_unselected_padding_files(client):
    content = multi_file_torrent(
        b"Show",
        [
            {b"length": 10, b"path": [b"S01E01.mkv"]},
            {b"length": 16374, b"path": [b".pad", b"16374"], b"attr": b"p"},
            {b"length": 20, b"path": [b"S01E02.mkv"]},
        ],
        pieces=b"x" * 40,
    )
    meta = parse_torrent_metadata(content)
    real_set_wanted = client._set_wanted

    def set_wanted(infohash, wanted, all_ids):
        real_set_wanted(infohash, wanted, all_ids)
        for row in client.torrents[infohash.upper()]["files"]:
            if "/.pad/" in row["name"]:
                row["priority"] = 1  # some clients keep the padding flag as it was

    client._set_wanted = set_wanted  # type: ignore[method-assign]
    info = client.add_torrent_selected(content, "D:/tv", meta.client_hash, [meta.files[-1].index])
    assert info["state"] == "downloading"


def test_start_confirmation_needs_a_state(client):
    client.seed(tags=[OWNER], state="", wanted={E01})
    with pytest.raises(ClientError) as error:
        client._wait_started(H)
    assert str(error.value) == _msg("client.managed.start_unconfirmed")


def test_read_back_polls_sleep_between_attempts(client, monkeypatch):
    sleeps: list[float] = []
    monkeypatch.setattr(managed.time, "sleep", sleeps.append)
    client.PAUSE = 0.25
    with pytest.raises(ClientError):
        client._wait_stopped(H)  # never there
    assert sleeps == [0.25] * (client.POLLS - 1)
    sleeps.clear()
    assert client._visible_alias(["A" * 40, "B" * 40]) == "A" * 40
    assert sleeps == [0.25] * (client.POLLS - 1)


def test_clear_pending_never_reclaims_a_torrent_without_ownership(client):
    client.seed(tags=[PENDING, "kids"], state="downloading")
    with pytest.raises(ClientError) as error:
        client._clear_pending(H)
    assert str(error.value) == _msg("client.managed.not_owned")
    assert client.calls == []


# --- stop / move / magnet ---------------------------------------------------------------------


def test_stop_of_a_missing_torrent_is_an_error(client):
    with pytest.raises(ClientError) as error:
        client.stop_owned_torrent(H)
    assert str(error.value) == _msg("client.managed.missing")


def test_stop_of_a_running_owned_torrent_waits_for_the_read_back(client):
    client.seed(tags=[OWNER], state="downloading")
    assert client.stop_owned_torrent(H)["state"] == "stoppedDL"
    assert client.calls == [("stop",)]


def test_move_needs_a_folder_and_passes_it_trimmed(client):
    client.seed(tags=[OWNER], state="downloading")
    with pytest.raises(ClientError) as error:
        client.set_location(H, "   ")
    assert str(error.value) == _msg("client.managed.no_folder")
    assert client.set_location(H, "  E:/moved ") == "ok"
    assert client.calls == [("move", "E:/moved")]
    assert client.has_hash(H)
    assert not client.has_hash("0" * 40)


def test_move_of_a_foreign_torrent_is_refused_before_mutation(client):
    client.seed(tags=["manual"], state="downloading")
    with pytest.raises(ClientError) as error:
        client.set_location(H, "E:/moved")
    assert str(error.value) == _msg("client.managed.not_owned")
    assert client.calls == []


def test_the_base_class_primitives_must_be_implemented():
    base = ManagedClient()
    calls = [
        base.ping,
        lambda: base.inspect_torrent(H),
        lambda: base._add_stopped(b"", "D:/", []),
        lambda: base._set_wanted(H, set(), []),
        lambda: base._stop(H),
        lambda: base._start(H),
        lambda: base._set_labels(H, []),
        lambda: base._move(H, "D:/"),
    ]
    for call in calls:
        with pytest.raises(NotImplementedError):
            call()
    with pytest.raises(ClientError) as error:
        base.materialize_magnet(f"magnet:?xt=urn:btih:{H}", "D:/", H)
    assert str(error.value) == f"client: {t('client.managed.no_magnet')}"
