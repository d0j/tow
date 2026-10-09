"""A client add or change that the client's read-back does not confirm is never a success.

Every path of a check that changes (or relies on) a torrent in the client ends in a read-back;
each test here lets that read-back disagree - another folder, no TOW mark, another torrent, no
torrent at all - and requires a failed row with its own error code, the saved revision kept and
no "added" record. The unit tests pin the read-back helpers of ``tow.check.client_ops`` that
those paths rely on. Clients and trackers are fakes; nothing leaves the test's temp folder.
"""

from __future__ import annotations

from types import SimpleNamespace
from typing import Any, ClassVar

import pytest

from tow import check
from tow.check import client_ops
from tow.check import reconcile as check_reconcile
from tow.check import run as check_run
from tow.check import topic as topic_step
from tow.clients import factory as client_factory
from tow.store import load_state, save_download_history, save_state
from tow.torrent import TorrentFile


@pytest.fixture(autouse=True)
def _room_for_every_add(monkeypatch):
    r"""These tests are about the add, not about disk space: the files always fit. On a machine
    whose drive had less than 512 MiB free (the folder "M:\TV" is a relative path on Linux
    and macOS) the add waited for space instead, and the outcomes were not the ones tested."""
    from tow.check import apply as check_apply

    monkeypatch.setattr(check_apply, "free_space_problem", lambda *_args, **_kwargs: None)


V1 = "A" * 40
V2 = "B" * 64
NEW = "D" * 40
OTHER = "C" * 40
FOLDER = r"M:\TV"
ELSEWHERE = r"N:\Elsewhere"


class Client:
    """A torrent client that keeps its torrents in a dictionary; ``after_add`` and
    ``after_configure`` change what it reports once TOW has asked it to add or reconfigure."""

    client_id = "main"
    client_kind = "fake"
    capabilities: ClassVar[dict[str, bool]] = {
        "inspect": True,
        "add": True,
        "stopped_add": True,
        "file_selection": True,
        "priority_readback": True,
        "start_stop": True,
    }

    def __init__(self, torrents: dict[str, dict[str, Any]] | None = None) -> None:
        self.torrents = {key: dict(value) for key, value in (torrents or {}).items()}
        self.register_as: str | None = None  # the hash the client files a new torrent under
        self.after_add: dict[str, Any] = {}
        self.after_configure: dict[str, Any] = {}
        self.adds = 0
        self.configures = 0

    def ping(self) -> str:
        return "ok"

    def has_hash(self, infohash: str) -> bool:
        return infohash.upper() in self.torrents

    def inspect_torrent(self, infohash: str) -> dict[str, Any] | None:
        info = self.torrents.get(infohash.upper())
        return None if info is None else {"hash": infohash.upper(), "files": [], **info}

    def add_torrent_selected(self, content, save_path, infohash, selected_indices, *, start=True):
        self.adds += 1
        registered = (self.register_as or infohash).upper()
        self.torrents[registered] = {"hash": registered, "save_path": save_path, "tags": ["tow"], **self.after_add}
        return {"hash": registered}

    def configure_torrent_selection(self, content, infohash, selected_indices, *, ensure_started=False):
        self.configures += 1
        self.torrents[infohash.upper()].update(self.after_configure)
        return self.inspect_torrent(infohash)


def _metadata(client_hash: str = NEW, **hashes: str) -> SimpleNamespace:
    return SimpleNamespace(
        infohash=client_hash, client_hash=client_hash, name="Show", files=(TorrentFile(0, "Show.mkv", 1),), **hashes
    )


@pytest.fixture
def wired(monkeypatch):
    """Wire a check to one fake tracker and the client the test sets; returns the recorded
    events and a setter for the client, the tracker and the parsed .torrent."""
    events: list[tuple[str, dict[str, Any]]] = []
    tracker = SimpleNamespace(
        name="fake",
        fetch_torrent=lambda *_a, **_k: b"torrent",
        fetch_title=lambda *_a, **_k: "Show",
    )
    holder: dict[str, Any] = {"metadata": _metadata()}
    cfg = {"trackers": {}, "client": {"id": "main", "kind": "fake"}}
    monkeypatch.setattr(check_run, "load_config", lambda: cfg)
    monkeypatch.setattr(check_run, "load_trackers", lambda _cfg: {"fake": tracker})
    monkeypatch.setattr(check_run, "log_event", lambda kind, **fields: events.append((kind, fields)))
    monkeypatch.setattr(topic_step, "match_tracker", lambda _trackers, _url: tracker)
    monkeypatch.setattr(topic_step, "parse_torrent_metadata", lambda _blob: holder["metadata"])
    monkeypatch.setattr(client_factory, "default_client_id", lambda _cfg: "main")
    monkeypatch.setattr(client_factory, "from_secrets", lambda _cfg, _secrets, client_id=None: holder["client"])
    monkeypatch.setattr(check_reconcile, "reconcile_topic", lambda *_a, **_k: {"events": [], "summary": {}})
    save_download_history({"schema_version": 1, "topics": {}})
    return SimpleNamespace(events=events, tracker=tracker, holder=holder)


def _topic(**fields: Any) -> dict[str, Any]:
    save_state({"topics": [{"id": "t1", "title": "Show", "url": "https://tracker/1", "save_path": FOLDER, **fields}]})
    return fields


def _run(wired, client: Client, *, metadata: SimpleNamespace | None = None) -> dict[str, Any]:
    wired.holder["client"] = client
    if metadata is not None:
        wired.holder["metadata"] = metadata
    rows: list[dict[str, Any]] = check.run_check(apply=True, notify=False, how="test")["results"]
    [row] = rows
    return row


def _refused(wired, row: dict[str, Any], code: str, *, kept_hash: str | None) -> None:
    """The row failed with ``code``, the topic keeps its revision, nothing counts as added."""
    assert row["ok"] is False
    assert row["status"] == "failed"
    assert row["error_record"]["code"] == code
    assert row.get("added") is not True
    topic = load_state()["topics"][0]
    assert topic.get("hash") == kept_hash
    assert topic["last_ok"] is False
    assert not topic.get("once_done")
    kinds = [kind for kind, _fields in wired.events]
    assert "client_added" not in kinds
    assert "client_selection_updated" not in kinds


# --- a new add (check/apply.py: _add_new_revision) ------------------------------------------


@pytest.mark.parametrize(
    "reported",
    [{"save_path": ELSEWHERE}, {"tags": ["manual"]}, {"tags": "tow"}, {"save_path": ""}],
    ids=["another-folder", "without-the-mark", "tags-not-a-list", "no-folder"],
)
def test_an_add_the_read_back_does_not_confirm_is_a_failure(wired, reported):
    _topic(hash=V1)
    client = Client()
    client.after_add = reported

    row = _run(wired, client)

    _refused(wired, row, "check.add_unconfirmed", kept_hash=V1)
    assert client.adds == 1


def test_an_add_the_client_files_under_a_hash_of_another_torrent_is_a_failure(wired):
    _topic(hash=V1)
    client = Client()
    client.register_as = OTHER

    row = _run(wired, client)

    _refused(wired, row, "check.unexpected_identity", kept_hash=V1)


def test_a_hybrid_filed_under_its_v1_hash_is_confirmed_under_that_hash(wired):
    v2_id = V2[:40]
    metadata = _metadata(v2_id, hash_v1=V1, hash_v2=V2)
    _topic(hash=NEW)
    client = Client()
    client.register_as = V1

    row = _run(wired, client, metadata=metadata)

    assert row["ok"] is True
    assert row["added"] is True
    assert row["hash"] == V1
    assert row["client_hash_legacy_v1"] is True
    assert load_state()["topics"][0]["hash"] == V1


def test_a_hybrid_filed_under_its_v1_hash_in_another_folder_is_a_failure(wired):
    metadata = _metadata(V2[:40], hash_v1=V1, hash_v2=V2)
    _topic(hash=NEW)
    client = Client()
    client.register_as = V1
    client.after_add = {"save_path": ELSEWHERE}

    row = _run(wired, client, metadata=metadata)

    _refused(wired, row, "check.add_unconfirmed", kept_hash=NEW)


# --- a new file selection of TOW's torrent (check/apply.py: _update_client_selection) -------


def test_a_selection_change_of_a_torrent_without_the_mark_is_refused_untouched(wired):
    _topic(hash=NEW, selection_dirty=True)
    client = Client({NEW: {"save_path": FOLDER, "tags": ["manual"]}})

    row = _run(wired, client)

    _refused(wired, row, "check.not_owned_priorities", kept_hash=NEW)
    assert client.configures == 0
    assert load_state()["topics"][0]["selection_dirty"] is True


def test_a_selection_change_of_a_torrent_in_another_folder_is_refused_untouched(wired):
    _topic(hash=NEW, selection_dirty=True)
    client = Client({NEW: {"save_path": ELSEWHERE, "tags": ["tow"]}})

    row = _run(wired, client)

    _refused(wired, row, "check.save_path_unconfirmed", kept_hash=NEW)
    assert client.configures == 0


@pytest.mark.parametrize("reported", [{"tags": []}, {"save_path": ELSEWHERE}], ids=["mark-lost", "moved"])
def test_a_selection_change_the_read_back_does_not_confirm_is_a_failure(wired, reported):
    _topic(hash=NEW, selection_dirty=True)
    client = Client({NEW: {"save_path": FOLDER, "tags": ["tow"]}})
    client.after_configure = reported

    row = _run(wired, client)

    _refused(wired, row, "check.selection_unconfirmed", kept_hash=NEW)
    assert client.configures == 1
    assert load_state()["topics"][0]["selection_dirty"] is True


# --- only the hash label changed (check/apply.py: _accept_existing_torrent) -----------------


def test_a_new_hash_label_of_a_torrent_in_another_folder_is_a_failure(wired):
    v2_id = V2[:40]
    _topic(hash=V1)
    client = Client({v2_id: {"save_path": ELSEWHERE, "tags": ["tow"]}})

    row = _run(wired, client, metadata=_metadata(v2_id, hash_v1=V1, hash_v2=V2))

    _refused(wired, row, "check.migration_unconfirmed", kept_hash=V1)
    assert client.adds == client.configures == 0


# --- "download once": the torrent must be in the client (check/topic.py) ------------------


@pytest.mark.parametrize(
    "torrents",
    [{}, {NEW: {"save_path": ELSEWHERE, "tags": ["tow"]}}],
    ids=["torrent-gone", "another-folder"],
)
def test_a_download_once_revision_the_client_does_not_confirm_is_not_done(wired, torrents):
    _topic(hash=NEW, tracking_mode="once")
    client = Client(torrents)

    row = _run(wired, client)

    _refused(wired, row, "check.once_unconfirmed", kept_hash=NEW)
    assert client.adds == client.configures == 0


def _torrent_link_gone(wired, magnet: str) -> None:
    def refused(*_args, **_kwargs):
        raise RuntimeError("fake: no download link on page")

    wired.tracker.fetch_torrent = refused
    wired.tracker.fetch_magnet = lambda *_args, **_kwargs: (magnet, V1)


@pytest.mark.parametrize(
    "torrents",
    [{}, {V1: {"save_path": ELSEWHERE, "tags": ["tow"]}}],
    ids=["torrent-gone", "another-folder"],
)
def test_a_download_once_revision_confirmed_by_its_magnet_still_needs_the_client(wired, torrents):
    _topic(hash=V1, tracking_mode="once")
    _torrent_link_gone(wired, f"magnet:?xt=urn:btih:{V1}")
    client = Client(torrents)

    row = _run(wired, client)

    _refused(wired, row, "check.once_unconfirmed", kept_hash=V1)


# --- the tracker's magnet stands in for a missing .torrent (check/topic.py) -----------------


@pytest.mark.parametrize(
    "magnet", ["", "magnet:?dn=Show", f"https://tracker/{V1}"], ids=["none", "no-hash", "no-magnet"]
)
def test_without_a_magnet_naming_the_revision_the_missing_torrent_link_is_the_result(wired, magnet):
    _topic(hash=V1)
    _torrent_link_gone(wired, magnet)

    row = _run(wired, Client({V1: {"save_path": FOLDER, "tags": ["tow"]}}))

    assert row["ok"] is False
    assert row.get("source") != "matching_magnet"
    assert "no download link" in row["error"]
    assert not any(fields.get("source") == "matching_magnet" for _kind, fields in wired.events)


@pytest.mark.parametrize(
    "torrents",
    [{}, {V1: {"save_path": FOLDER, "tags": ["tow"], "infohash_v1": V1}}, {OTHER: {"infohash_v2": V2}}],
    ids=["client-lacks-it", "client-knows-no-v2", "another-torrent"],
)
def test_a_hybrid_magnet_the_client_does_not_confirm_is_not_the_same_revision(wired, torrents):
    _topic(hash=V1)
    _torrent_link_gone(wired, f"magnet:?xt=urn:btih:{V1}&xt=urn:btmh:1220{V2}")

    row = _run(wired, Client(torrents))

    assert row["ok"] is False
    assert row.get("source") != "matching_magnet"
    assert load_state()["topics"][0]["last_ok"] is False


# --- the read-back helpers themselves --------------------------------------------------------


class Inspected:
    def __init__(self, info: dict[str, Any] | None) -> None:
        self.info = info

    def inspect_torrent(self, _infohash: str) -> dict[str, Any] | None:
        return self.info


def test_a_magnet_without_a_hash_matches_nothing():
    assert client_ops.magnet_matches_saved_hash("", V1, None) is False
    assert client_ops.magnet_matches_saved_hash("magnet:?dn=Show", V1, None) is False
    assert client_ops.magnet_matches_saved_hash(f"magnet:?xt=urn:btih:{V1}", "", None) is False
    assert client_ops.magnet_matches_saved_hash(f"magnet:?xt=urn:btih:{V1}", V1.lower(), None) is True


def test_a_hybrid_magnet_matches_only_when_the_client_reports_its_v2():
    magnet = f"magnet:?xt=urn:btih:{V1}&xt=urn:btmh:1220{V2}"
    assert client_ops.magnet_matches_saved_hash(magnet, V1, None) is False
    assert client_ops.magnet_matches_saved_hash(magnet, V1, Inspected(None)) is False
    assert client_ops.magnet_matches_saved_hash(magnet, V1, Inspected({"hash": OTHER, "infohash_v2": V2})) is False
    assert client_ops.magnet_matches_saved_hash(magnet, V1, Inspected({"hash": V1, "infohash_v2": "E" * 64})) is False
    assert client_ops.magnet_matches_saved_hash(magnet, V1, Inspected({"hash": V1, "infohash_v2": V2})) is True
    assert client_ops.magnet_matches_saved_hash(magnet, V2[:40], None) is True  # the v2 id itself


def test_a_client_report_is_of_this_torrent_only_under_one_of_its_hashes():
    assert client_ops.info_owned_by_tow({"hash": OTHER, "infohash_v2": V2, "tags": ["tow"]}, V2[:40]) is True  # v2 id
    assert client_ops.info_owned_by_tow({"hash": V2, "tags": ["tow"]}, V2[:40]) is False  # only infohash_v2 is cut
    assert client_ops.info_owned_by_tow({"hash": OTHER, "infohash_v1": V1, "tags": ["tow"]}, V1) is True
    assert client_ops.info_owned_by_tow({"hash": OTHER, "infohash_v2": V2, "tags": ["tow"]}, V2) is True
    assert client_ops.info_owned_by_tow({"hash": V2[:39], "tags": ["tow"]}, V2[:39]) is True
    assert client_ops.info_owned_by_tow({"hash": "", "tags": ["tow"]}, "") is False
    assert client_ops.info_owned_by_tow({"hash": OTHER, "infohash_v2": V2[:63], "tags": ["tow"]}, V2[:40]) is False
    assert client_ops.info_owned_by_tow({"hash": OTHER, "infohash_v2": V2 + "B", "tags": ["tow"]}, V2[:40]) is False


def test_ownership_needs_this_torrent_and_the_mark_in_a_list():
    assert client_ops.info_owned_by_tow(None, V1) is False
    assert client_ops.info_owned_by_tow({}, V1) is False
    assert client_ops.info_owned_by_tow({"hash": OTHER, "tags": ["tow"]}, V1) is False
    assert client_ops.info_owned_by_tow({"hash": V1, "tags": "tow"}, V1) is False
    assert client_ops.info_owned_by_tow({"hash": V1, "tags": None}, V1) is False
    assert client_ops.info_owned_by_tow({"hash": V1, "tags": ["manual"]}, V1) is False
    assert client_ops.info_owned_by_tow({"hash": V1, "tags": [" TOW "]}, V1) is True
    assert client_ops.info_owned_by_tow({"hash": V1, "tags": ("x", "tow")}, V1) is True


def test_a_read_back_that_needs_the_mark_refuses_a_torrent_without_it():
    info = {"hash": V1, "save_path": FOLDER, "tags": ["manual"]}
    assert client_ops.info_confirms(info, V1, FOLDER) is True
    assert client_ops.info_confirms(info, V1, FOLDER, require_tow_ownership=True) is False
    owned = {**info, "tags": ["tow"]}
    assert client_ops.info_confirms(owned, V1, FOLDER, require_tow_ownership=True) is True
    assert client_ops.info_confirms(owned, OTHER, FOLDER) is False
    assert client_ops.info_confirms(None, V1, FOLDER) is False
    assert client_ops.info_confirms({**owned, "save_path": ""}, V1, "") is False
    assert client_ops.info_confirms({**owned, "save_path": ELSEWHERE}, V1, FOLDER) is False


def test_a_confirmation_asks_for_the_mark_only_when_told_to():
    client = Inspected({"hash": V1, "save_path": FOLDER, "tags": []})
    assert client_ops.confirm_client_add(client, V1, FOLDER) is True
    assert client_ops.confirm_client_add(client, V1, FOLDER, require_tow_ownership=True) is False
    assert client_ops.client_owned_by_tow(client, V1) is False


def test_a_pending_add_needs_both_marks_on_this_torrent():
    # The check reads the torrent once and asks about that report (no second client read).
    assert client_ops.info_is_pending_tow_add({"hash": V1, "tags": ["tow", "tow-pending"]}, V1) is True
    assert client_ops.info_is_pending_tow_add({"hash": V1, "tags": ["tow"]}, V1) is False
    assert client_ops.info_is_pending_tow_add({"hash": V1, "tags": ["tow-pending"]}, V1) is False
    assert client_ops.info_is_pending_tow_add({"hash": OTHER, "tags": ["tow", "tow-pending"]}, V1) is False
    assert client_ops.info_is_pending_tow_add(None, V1) is False


class Moving:
    """A client whose move reports ``states`` one read after another (the last one stays)."""

    def __init__(self, *states: dict[str, Any] | None) -> None:
        self.states = list(states)
        self.reads = 0

    def inspect_torrent(self, _infohash: str) -> dict[str, Any] | None:
        self.reads += 1
        return self.states.pop(0) if len(self.states) > 1 else self.states[0]


def test_a_relocation_is_done_only_when_read_back_in_the_new_folder_with_the_mark(monkeypatch):
    monkeypatch.setattr(client_ops.time, "sleep", lambda _seconds: None)
    there = {"hash": V1, "save_path": FOLDER, "tags": ["tow"], "state": "stoppedUP"}
    assert client_ops.await_relocation(Moving(there), V1, FOLDER, timeout=0) == "done"
    assert client_ops.await_relocation(Moving({**there, "tags": []}), V1, FOLDER, timeout=0) == "failed"
    moving = {**there, "save_path": ELSEWHERE, "state": "moving"}
    assert client_ops.await_relocation(Moving(moving), V1, FOLDER, timeout=0) == "moving"
    assert client_ops.await_relocation(Moving({**moving, "state": "Moving"}), V1, FOLDER, timeout=0) == "moving"
    assert client_ops.await_relocation(Moving({**moving, "state": "stalledUP"}), V1, FOLDER, timeout=0) == "failed"
    assert client_ops.await_relocation(Moving(None), V1, FOLDER, timeout=0) == "failed"


def test_a_relocation_is_read_again_until_it_lands_or_the_time_is_up(monkeypatch):
    clock = {"now": 100.0}
    monkeypatch.setattr(client_ops.time, "monotonic", lambda: clock["now"])
    monkeypatch.setattr(client_ops.time, "sleep", lambda seconds: clock.update(now=clock["now"] + seconds))
    there = {"hash": V1, "save_path": FOLDER, "tags": ["tow"]}
    moving = {**there, "save_path": ELSEWHERE, "state": "moving"}
    client = Moving(moving, moving, there)
    assert client_ops.await_relocation(client, V1, FOLDER, timeout=5) == "done"
    assert client.reads == 3
    stuck = Moving(moving)
    assert client_ops.await_relocation(stuck, V1, FOLDER, timeout=2) == "moving"
    assert stuck.reads == 5  # at 0, 0.5, 1, 1.5 and 2 seconds: the deadline itself is read too
    monkeypatch.setattr(client_ops, "RELOCATION_WAIT_SEC", 1.0)
    default = Moving(moving)
    assert client_ops.await_relocation(default, V1, FOLDER) == "moving"
    assert default.reads == 3
