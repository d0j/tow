"""Behaviour tests for the CLI boundary, the qBittorrent adapter and server-local restore points.

qBittorrent is always a stateful fake in place of the qbittorrent-api client; the CLI's heavy
operations (doctor, uvicorn, snapshots, watchdog) are stubbed; restore points run on temp state.
"""

from __future__ import annotations

import itertools
import json
import os
import re
import runpy
import stat
import sys
import warnings
from pathlib import Path
from types import SimpleNamespace

import pytest
import yaml
from cryptography.fernet import Fernet
from helpers import multi_file_torrent, raises_code

from tow import cli, restore_points
from tow.bundle import ExportImportError
from tow.clients import qbittorrent
from tow.config import load_config, save_config
from tow.i18n import t
from tow.restore_points import (
    RestorePointError,
    check_portable_bundle,
    create_restore_point,
    export_portable_bundle,
    list_restore_points,
    restore_from_point,
    restore_points_dir,
)
from tow.store import load_state, save_secrets, save_state
from tow.torrent import parse_torrent_metadata

# --------------------------------------------------------------------------- torrents


TORRENT = multi_file_torrent(
    b"Show", [{b"length": 10, b"path": [b"S01E01.mkv"]}, {b"length": 20, b"path": [b"S01E02.mkv"]}]
)
OTHER_TORRENT = multi_file_torrent(b"Other", [{b"length": 7, b"path": [b"movie.mkv"]}])
PAD_TORRENT = multi_file_torrent(
    b"Pad",
    [
        {b"length": 10, b"path": [b"a.mkv"]},
        {b"length": 6, b"path": [b".pad", b"6"], b"attr": b"p"},
        {b"length": 20, b"path": [b"b.mkv"]},
    ],
)
META = parse_torrent_metadata(TORRENT)
HASH = META.client_hash.upper()
SAVE = r"M:\TV"
SHOW_FILES = [
    {"index": 4, "name": "Show/S01E01.mkv", "size": 10, "progress": 0, "priority": 1},
    {"index": 9, "name": "Show/S01E02.mkv", "size": 20, "progress": 0, "priority": 1},
]


# --------------------------------------------------------------------------- fake qBittorrent


class FakeQbit:
    """Stateful stand-in for qbittorrentapi.Client: only what a Web API would answer."""

    def __init__(self, *, web_api: str = "2.15.1") -> None:
        self.app = SimpleNamespace(version="5.2.3", web_api_version=web_api)
        self.torrents: dict[str, dict] = {}
        self.calls: list = []
        self.info_queries: list = []
        self.add_reply: object = "Ok."
        self.on_add = None
        self.stop_state = "stoppedDL"
        self.start_state = "downloading"
        self.apply_priorities = True
        self.break_selection: str | None = None  # "once" | "always": a priority=1 write loses its first id
        self.stop_failures_after: int | None = None
        self.hidden_file_reads = 0
        self.export_failures = 0
        self.export_content = TORRENT
        self.delete_works = True

    def put(self, hash_: str, *, files, tags="tow,tow-pending", state="stoppedDL", save_path=SAVE, **extra) -> dict:
        row = {
            "hash": hash_.upper(),
            "tags": tags,
            "state": state,
            "save_path": save_path,
            "progress": 0,
            "downloaded": 0,
            "files": [dict(item) for item in files],
            **extra,
        }
        self.torrents[hash_.upper()] = row
        return row

    def priorities(self, hash_: str = HASH) -> dict:
        return {row["index"]: row["priority"] for row in self.torrents[hash_]["files"] if row["index"] is not None}

    # --- Web API surface used by the adapter
    def torrents_info(self, torrent_hashes=None):
        self.info_queries.append(torrent_hashes)
        rows = [
            row
            for row in self.torrents.values()
            if torrent_hashes is None or row["hash"].lower() == str(torrent_hashes).lower()
        ]
        return [SimpleNamespace(**{key: value for key, value in row.items() if key != "files"}) for row in rows]

    def torrents_files(self, *, torrent_hash):
        self.calls.append("files")
        if self.hidden_file_reads > 0:
            self.hidden_file_reads -= 1
            return []
        row = self.torrents.get(torrent_hash.upper())
        return [SimpleNamespace(**item) for item in (row["files"] if row else [])]

    def torrents_add(self, **kwargs):
        self.calls.append("add")
        self.added = kwargs
        if self.on_add is not None:
            self.on_add(kwargs)
        return self.add_reply

    def torrents_file_priority(self, *, torrent_hash, file_ids, priority):
        self.calls.append(("priority", tuple(file_ids), priority))
        if not self.apply_priorities:
            return
        for row in self.torrents[torrent_hash.upper()]["files"]:
            if row["index"] in file_ids:
                row["priority"] = priority
        if priority == 1 and self.break_selection and file_ids:
            first = file_ids[0]
            for row in self.torrents[torrent_hash.upper()]["files"]:
                if row["index"] == first:
                    row["priority"] = 0
            if self.break_selection == "once":
                self.break_selection = None

    def torrents_stop(self, *, torrent_hashes):
        self.calls.append("stop")
        if self.stop_failures_after is not None and self.calls.count("stop") > self.stop_failures_after:
            raise ConnectionError("qBit went away")
        if torrent_hashes.upper() in self.torrents:
            self.torrents[torrent_hashes.upper()]["state"] = self.stop_state

    def torrents_start(self, *, torrent_hashes):
        self.calls.append("start")
        if torrent_hashes.upper() in self.torrents:
            self.torrents[torrent_hashes.upper()]["state"] = self.start_state

    def torrents_remove_tags(self, *, tags, torrent_hashes):
        self.calls.append("remove-tags")
        row = self.torrents[torrent_hashes.upper()]
        row["tags"] = ",".join(tag for tag in row["tags"].split(",") if tag.strip() != tags)

    def torrents_set_location(self, *, location, torrent_hashes):
        self.calls.append(("set_location", location, torrent_hashes))

    def torrents_delete(self, *, delete_files, torrent_hashes):
        self.calls.append(("delete", delete_files))
        if self.delete_works:
            self.torrents.pop(torrent_hashes.upper(), None)

    def torrents_export(self, *, torrent_hash):
        self.calls.append("export")
        if self.export_failures > 0:
            self.export_failures -= 1
            raise RuntimeError("409 metadata not ready")
        return self.export_content


def _registers(api: FakeQbit, hash_: str = HASH, files=None, *, tags=None):
    """Make torrents_add create the torrent the way qBit does (stopped if asked)."""

    def on_add(kwargs):
        api.put(
            hash_,
            files=SHOW_FILES if files is None else files,
            tags=kwargs.get("tags", "") if tags is None else tags,
            state="stoppedDL" if kwargs.get("is_stopped") else "metaDL",
            save_path=kwargs.get("save_path"),
        )

    api.on_add = on_add


@pytest.fixture
def qbit(monkeypatch):
    api = FakeQbit()
    monkeypatch.setattr(qbittorrent, "Client", lambda **_kwargs: api)
    monkeypatch.setattr(qbittorrent.time, "sleep", lambda _seconds: None)
    return qbittorrent.QBittorrentClient("http://qbit", 8080, "user", "password"), api


# --------------------------------------------------------------------------- qBittorrent: basics


def test_ping_and_has_hash_report_what_the_client_answers(qbit):
    client, api = qbit
    api.put(HASH, files=SHOW_FILES)

    assert client.ping() == "5.2.3 webapi 2.15.1"
    assert client.has_hash(HASH) is True
    assert api.info_queries[-1] == HASH.lower()
    assert client.has_hash("BB" * 20) is False


def test_inspect_of_a_missing_torrent_is_none_without_listing_files(qbit):
    client, api = qbit

    assert client.inspect_torrent(HASH) is None
    assert "files" not in api.calls


def test_inspect_normalizes_list_tags_and_files(qbit):
    client, api = qbit
    api.put(HASH, files=SHOW_FILES, tags=[" tow", "tow", "", "manual"], state=None)

    info = client.inspect_torrent(HASH.lower())

    assert info["tags"] == ["manual", "tow"]
    assert info["state"] == ""
    assert [row["name"] for row in info["files"]] == ["Show/S01E01.mkv", "Show/S01E02.mkv"]
    assert info["files"][1] == {"index": 9, "name": "Show/S01E02.mkv", "size": 20, "progress": 0, "priority": 1}


def test_set_location_refuses_blank_path_and_sends_trimmed_location(qbit):
    client, api = qbit

    with raises_code("client.managed.no_folder", RuntimeError):
        client.set_location(HASH, "   ")
    assert api.calls == []

    assert client.set_location(HASH, r"  M:\new  ") == "ok"
    assert api.calls == [("set_location", r"M:\new", HASH.lower())]


def test_from_secrets_requires_host_and_defaults_port(monkeypatch):
    built = []
    monkeypatch.setattr(qbittorrent, "Client", lambda **kwargs: built.append(kwargs) or FakeQbit())

    with raises_code("client.managed.no_address", RuntimeError):
        qbittorrent.from_secrets({"qbittorrent": {"port": 8080}})
    with raises_code("client.managed.no_address", RuntimeError):
        qbittorrent.from_secrets({})
    assert built == []

    qbittorrent.from_secrets({"qbittorrent": {"host": "http://qbit"}})
    qbittorrent.from_secrets({"qbittorrent": {"host": "http://qbit", "port": "9090", "username": "u"}})

    assert built[0]["port"] == 8080
    assert built[0]["username"] == ""
    assert built[0]["password"] == ""
    assert built[0]["REQUESTS_ARGS"] == {"timeout": 8}
    assert built[1]["port"] == 9090
    assert built[1]["username"] == "u"


# --------------------------------------------------------------------------- qBittorrent: add


@pytest.mark.parametrize(
    ("infohash", "save_path", "selected", "message"),
    [
        ("AB" * 20, SAVE, [0], "client.managed.hash_changed_add"),
        (HASH, "  ", [0], "client.managed.no_folder"),
        (HASH, SAVE, [], "client.managed.bad_selection"),
        (HASH, SAVE, [0, 7], "client.managed.bad_selection"),
    ],
)
def test_add_refuses_invalid_requests_before_contacting_qbit(qbit, infohash, save_path, selected, message):
    client, api = qbit

    with raises_code(message, RuntimeError):
        client.add_torrent_selected(TORRENT, save_path, infohash, selected)

    assert api.calls == []


def test_add_rejected_by_qbit_text_reply_is_never_success(qbit):
    client, api = qbit
    api.add_reply = "Fails."

    with raises_code("client.qbittorrent.api_refused", RuntimeError):
        client.add_torrent_selected(TORRENT, SAVE, HASH, [0])

    assert api.calls == ["add"]


def test_add_that_never_becomes_visible_is_not_confirmed_and_nothing_is_stopped(qbit):
    client, api = qbit  # accepted, but nothing ever appears in the client

    with raises_code("client.managed.not_visible", RuntimeError):
        client.add_torrent_selected(TORRENT, SAVE, HASH, [0])

    assert "stop" not in api.calls
    assert None in api.info_queries  # the periodic full listing was consulted too


def test_add_listed_under_another_client_id_is_confirmed_by_the_full_listing(qbit):
    client, api = qbit
    client_id = "C3" * 20  # qBit answers the filtered query only for its own id
    _registers(api, client_id)
    original = api.on_add
    api.on_add = lambda kwargs: (original(kwargs), api.torrents[client_id].update(infohash_v1=HASH))

    result = client.add_torrent_selected(TORRENT, SAVE, HASH, [1])

    assert None in api.info_queries
    assert result["hash"] == client_id
    assert api.priorities(client_id) == {4: 0, 9: 1}
    assert api.torrents[client_id]["tags"] == "tow"


def test_successful_partial_add_ignores_unindexed_rows_and_releases_ownership(qbit):
    client, api = qbit
    _registers(
        api, files=[{"index": None, "name": "Show/extra.nfo", "size": 1, "progress": 0, "priority": 1}, *SHOW_FILES]
    )

    result = client.add_torrent_selected(TORRENT, SAVE, HASH, [1])

    assert api.added["is_stopped"] is True
    assert api.added["tags"] == "tow,tow-pending"
    assert api.priorities() == {4: 0, 9: 1}
    assert result["tags"] == ["tow"]
    assert api.torrents[HASH]["state"] == "downloading"


def test_add_through_older_pause_resume_api(qbit):
    client, api = qbit
    _registers(api)
    api.torrents_stop = None
    api.torrents_start = None
    api.torrents_pause = lambda *, torrent_hashes: api.calls.append("pause")
    api.torrents_resume = lambda *, torrent_hashes: (
        api.calls.append("resume"),
        api.torrents[torrent_hashes.upper()].update(state="downloading"),
    )

    result = client.add_torrent_selected(TORRENT, SAVE, HASH, [0, 1])

    assert {"pause", "resume"} <= set(api.calls)
    assert result["tags"] == ["tow"]


@pytest.mark.parametrize("cleanup_fails", [False, True])
def test_add_that_enters_an_unsafe_state_while_stopping_fails_with_the_original_error(qbit, cleanup_fails):
    client, api = qbit
    _registers(api)
    api.stop_state = "error"
    if cleanup_fails:
        api.stop_failures_after = 1

    with raises_code("client.managed.error_state", RuntimeError):
        client.add_torrent_selected(TORRENT, SAVE, HASH, [0])

    assert api.calls.count("stop") == 2  # the failed add is stopped again (or that is attempted)
    assert "start" not in api.calls
    assert api.torrents[HASH]["tags"] == "tow,tow-pending"


def test_add_whose_stop_is_not_confirmed_is_not_started(qbit):
    client, api = qbit
    _registers(api)
    api.stop_state = "downloading"

    with raises_code("client.managed.stop_unconfirmed", RuntimeError):
        client.add_torrent_selected(TORRENT, SAVE, HASH, [0])

    assert "start" not in api.calls
    assert not any(isinstance(call, tuple) and call[0] == "priority" for call in api.calls)


@pytest.mark.parametrize(
    ("start_state", "message"),
    [
        ("missingFiles", "client.managed.error_state"),
        ("stoppedDL", "client.managed.start_unconfirmed"),
    ],
)
def test_add_whose_start_is_not_confirmed_keeps_the_pending_marker(qbit, start_state, message):
    client, api = qbit
    _registers(api)
    api.start_state = start_state

    with raises_code(message, RuntimeError):
        client.add_torrent_selected(TORRENT, SAVE, HASH, [0])

    assert "remove-tags" not in api.calls
    assert api.calls[-1] == "stop"
    assert api.torrents[HASH]["tags"] == "tow,tow-pending"


@pytest.mark.parametrize(
    ("missing", "message"),
    [
        ("torrents_stop", "client.qbittorrent.no_stop"),
        ("torrents_start", "client.qbittorrent.no_start"),
        ("torrents_remove_tags", "client.qbittorrent.no_tag_removal"),
    ],
)
def test_add_fails_closed_when_the_web_api_lacks_an_operation(qbit, missing, message):
    client, api = qbit
    _registers(api)
    setattr(api, missing, None)

    with raises_code(message, RuntimeError):
        client.add_torrent_selected(TORRENT, SAVE, HASH, [0])

    assert api.torrents[HASH]["tags"] == "tow,tow-pending"


def test_add_whose_files_never_appear_is_not_configured(qbit):
    client, api = qbit
    _registers(api, files=[])

    with raises_code("client.managed.no_files", RuntimeError):
        client.add_torrent_selected(TORRENT, SAVE, HASH, [0])

    assert "start" not in api.calls
    assert api.torrents[HASH]["state"] == "stoppedDL"


def test_partial_add_with_unmatched_client_file_names_fails(qbit):
    client, api = qbit
    _registers(api, files=[{"index": 0, "name": "Other/x.mkv", "size": 10, "progress": 0, "priority": 1}])

    with raises_code("client.managed.file_unmatched", RuntimeError):
        client.add_torrent_selected(TORRENT, SAVE, HASH, [1])

    assert not any(isinstance(call, tuple) and call[0] == "priority" for call in api.calls)
    assert "start" not in api.calls


def test_all_file_add_with_an_extra_client_file_is_a_count_mismatch(qbit):
    client, api = qbit
    extra = {"index": 11, "name": "Show/S01E03.mkv", "size": 30, "progress": 0, "priority": 1}
    _registers(api, files=[*SHOW_FILES, extra])

    with raises_code("client.managed.wrong_selection", RuntimeError):
        client.add_torrent_selected(TORRENT, SAVE, HASH, [0, 1])

    assert "start" not in api.calls


def test_all_file_add_whose_priorities_do_not_stick_is_a_mismatch(qbit):
    client, api = qbit
    _registers(api, files=[{**row, "priority": 0} for row in SHOW_FILES])
    api.apply_priorities = False

    with raises_code("client.managed.wrong_selection", RuntimeError):
        client.add_torrent_selected(TORRENT, SAVE, HASH, [0, 1])

    assert "start" not in api.calls


PAD_FILES = [
    {"index": 0, "name": "Pad/a.mkv", "size": 10, "progress": 0, "priority": 1},
    {"index": 1, "name": "Pad/.pad/6", "size": 6, "progress": 0, "priority": 1},
    {"index": 2, "name": "Pad/b.mkv", "size": 20, "progress": 0, "priority": 1},
]


def test_all_file_add_of_padded_torrent_never_downloads_padding(qbit):
    client, api = qbit
    pad_hash = parse_torrent_metadata(PAD_TORRENT).client_hash.upper()
    _registers(api, pad_hash, PAD_FILES)

    client.add_torrent_selected(PAD_TORRENT, SAVE, pad_hash, [0, 2])

    assert api.priorities(pad_hash) == {0: 1, 1: 0, 2: 1}


def test_all_file_add_of_padded_torrent_with_unapplied_priorities_fails(qbit):
    client, api = qbit
    pad_hash = parse_torrent_metadata(PAD_TORRENT).client_hash.upper()
    _registers(api, pad_hash, [{**row, "priority": 0} for row in PAD_FILES])
    api.apply_priorities = False

    with raises_code("client.managed.wrong_selection", RuntimeError):
        client.add_torrent_selected(PAD_TORRENT, SAVE, pad_hash, [0, 2])

    assert "start" not in api.calls


# --------------------------------------------------------------------------- qBittorrent: selection update


def test_selection_update_refuses_a_different_torrent(qbit):
    client, api = qbit
    api.put(HASH, files=SHOW_FILES, tags="tow", state="downloading")

    with raises_code("client.managed.hash_changed_selection", RuntimeError):
        client.configure_torrent_selection(TORRENT, "AB" * 20, [0])

    assert api.calls == []


def test_failed_selection_update_restores_priorities_and_restarts(qbit):
    client, api = qbit
    api.put(
        HASH, files=[{**SHOW_FILES[0], "priority": 1}, {**SHOW_FILES[1], "priority": 0}], tags="tow", state="uploading"
    )
    api.break_selection = "once"

    with raises_code("client.managed.wrong_selection", RuntimeError):
        client.configure_torrent_selection(TORRENT, HASH, [1])

    assert api.priorities() == {4: 1, 9: 0}
    assert api.torrents[HASH]["state"] == "downloading"
    assert [call for call in api.calls if call in ("stop", "start")][-1] == "start"


def test_empty_selection_update_is_refused_and_running_torrent_is_restarted(qbit):
    client, api = qbit
    api.put(HASH, files=SHOW_FILES, tags="tow", state="downloading")

    with raises_code("client.managed.bad_selection", RuntimeError):
        client.configure_torrent_selection(TORRENT, HASH, [])

    assert api.priorities() == {4: 1, 9: 1}
    assert api.torrents[HASH]["state"] == "downloading"


def test_unverifiable_selection_rollback_leaves_torrent_stopped(qbit):
    client, api = qbit
    api.put(
        HASH,
        files=[{**SHOW_FILES[0], "priority": 1}, {**SHOW_FILES[1], "priority": 0}],
        tags="tow",
        state="downloading",
    )
    api.break_selection = "always"

    with raises_code("client.managed.wrong_selection", RuntimeError):
        client.configure_torrent_selection(TORRENT, HASH, [1])

    assert "start" not in api.calls
    assert api.torrents[HASH]["state"] == "stoppedDL"


# --------------------------------------------------------------------------- qBittorrent: magnet metadata

MAGNET = f"magnet:?xt=urn:btih:{HASH}"


@pytest.mark.parametrize(
    ("magnet", "save_path", "infohash", "message"),
    [
        (MAGNET, "", HASH, "client.managed.no_folder"),
        ("magnet:?dn=nothing", SAVE, HASH, "client.qbittorrent.magnet_invalid"),
        (MAGNET, SAVE, "AB" * 20, "client.qbittorrent.magnet_hash_mismatch"),
    ],
)
def test_magnet_refuses_invalid_requests_before_adding(qbit, magnet, save_path, infohash, message):
    client, api = qbit

    with raises_code(message, RuntimeError):
        client.materialize_magnet(magnet, save_path, infohash)

    assert api.calls == []


def test_magnet_refuses_unparsable_web_api_version(qbit):
    client, api = qbit
    api.app.web_api_version = "unknown"

    with raises_code("client.qbittorrent.webapi_too_old", RuntimeError):
        client.materialize_magnet(MAGNET, SAVE, HASH)

    assert "add" not in api.calls


def test_magnet_with_ambiguous_client_identity_is_refused(qbit):
    client, api = qbit
    api.put("C1" * 20, files=[], infohash_v1=HASH)
    api.put("C2" * 20, files=[], infohash_v2=HASH + "0" * 24)

    with raises_code("client.qbittorrent.hash_ambiguous", RuntimeError):
        client.materialize_magnet(MAGNET, SAVE, HASH)

    assert "add" not in api.calls


def test_pending_magnet_that_is_active_is_not_touched(qbit):
    client, api = qbit
    api.put(HASH, files=[], tags="tow,tow-pending", state="metaDL")

    with raises_code("client.qbittorrent.pending_magnet_active", RuntimeError):
        client.materialize_magnet(MAGNET, SAVE, HASH)

    assert not any(isinstance(call, tuple) and call[0] == "delete" for call in api.calls)
    assert "add" not in api.calls


def test_stale_pending_magnet_without_delete_operation_is_not_recreated(qbit):
    client, api = qbit
    api.put(HASH, files=[], tags="tow,tow-pending", state="stoppedDL")
    api.torrents_delete = None

    with raises_code("client.qbittorrent.cannot_recreate_magnet", RuntimeError):
        client.materialize_magnet(MAGNET, SAVE, HASH)

    assert "add" not in api.calls


def test_stale_pending_magnet_whose_removal_is_not_confirmed_is_not_readded(qbit):
    client, api = qbit
    api.put(HASH, files=[], tags="tow,tow-pending", state="stoppedDL")
    api.delete_works = False

    with raises_code("client.qbittorrent.stale_magnet_not_removed", RuntimeError):
        client.materialize_magnet(MAGNET, SAVE, HASH)

    assert ("delete", False) in api.calls  # never deletes payload files
    assert "add" not in api.calls


def test_magnet_metadata_waits_for_files_and_retries_export(qbit):
    client, api = qbit
    _registers(api)
    api.hidden_file_reads = 3
    api.export_failures = 1

    assert client.materialize_magnet(MAGNET, SAVE, HASH) == TORRENT
    assert api.calls.count("export") == 2
    assert api.added["stop_condition"] == "MetadataReceived"


def test_magnet_metadata_for_another_torrent_is_rejected_and_pending_add_stopped(qbit):
    client, api = qbit
    _registers(api)
    api.export_content = OTHER_TORRENT

    with raises_code("client.qbittorrent.magnet_data_mismatch", RuntimeError):
        client.materialize_magnet(MAGNET, SAVE, HASH)

    assert api.calls.count("stop") == 2  # the metadata-only stop, then the failure cleanup


def test_magnet_metadata_timeout_stops_the_created_torrent(qbit, monkeypatch):
    client, api = qbit
    _registers(api, files=[])
    ticks = itertools.count(0, 10)
    monkeypatch.setattr(qbittorrent.time, "monotonic", lambda: next(ticks))

    with raises_code("client.qbittorrent.magnet_timeout", RuntimeError):
        client.materialize_magnet(MAGNET, SAVE, HASH)

    assert api.calls[-1] == "stop"
    assert "export" not in api.calls


def test_unowned_magnet_add_never_stops_a_possibly_foreign_torrent(qbit):
    client, api = qbit
    _registers(api, files=[], tags="manual")

    with raises_code("client.managed.no_owner_mark", RuntimeError):
        client.materialize_magnet(MAGNET, SAVE, HASH)

    assert "stop" not in api.calls


# --------------------------------------------------------------------------- CLI


def test_secrets_ok_is_false_without_a_client_host_or_with_blocked_secrets():
    from tow.paths import secrets_path

    assert cli.secrets_ok() is False  # template config, no secrets at all

    secrets_path().write_text(json.dumps({"qbittorrent": {"host": "http://qbit"}}), encoding="utf-8")
    assert cli.secrets_ok() is False  # legacy plaintext must be migrated first


def test_human_output_of_a_scalar_is_the_text_itself():
    assert cli._human("plain text") == "plain text"
    assert cli._human(3) == "3"


def test_secrets_status_generate_key_and_refusal_to_overwrite(capsys, tmp_path):
    assert cli.main(["secrets", "status", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["storage"] == "uninitialized"

    key_file = tmp_path / "keys" / "master.key"
    assert cli.main(["secrets", "generate-key", "--key-file", str(key_file), "--json"]) == 0
    printed = capsys.readouterr().out
    assert json.loads(printed) == {
        "ok": True,
        "key_file_created": True,
        "message": t("cli.keys.generated", path=key_file),
    }
    original = key_file.read_bytes()
    assert original.decode().strip() not in printed

    assert cli.main(["secrets", "generate-key", "--key-file", str(key_file)]) == 3
    out = capsys.readouterr().out
    assert "ok: false" in out
    assert t("cli.keys.error.exists") in out
    assert key_file.read_bytes() == original


def test_secrets_migrate_encrypts_legacy_secrets_without_printing_them(monkeypatch, capsys):
    from tow.paths import secrets_path
    from tow.store import load_secrets

    monkeypatch.setenv("TOW_MASTER_KEY", Fernet.generate_key().decode("ascii"))
    monkeypatch.delenv("TOW_MASTER_KEY_FILE", raising=False)
    assert cli.main(["secrets", "migrate", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["migrated"] is False

    secrets_path().write_text(json.dumps({"telegram": {"token": "tok-secret-123"}}), encoding="utf-8")
    assert cli.main(["secrets", "migrate", "--json"]) == 0
    out = capsys.readouterr().out
    result = json.loads(out)
    assert result["ok"] is True
    assert result["migrated"] is True
    assert result["storage"] == "encrypted"
    assert "tok-secret-123" not in out
    assert not secrets_path().exists()
    assert load_secrets()["telegram"]["token"] == "tok-secret-123"


def test_import_monitorrent_prints_readable_result(monkeypatch, capsys, tmp_path):
    seen = []
    monkeypatch.setattr(
        "tow.import_monitorrent.import_monitorrent",
        lambda db, **kwargs: seen.append((db, kwargs.get("apply", False))) or {"ok": True, "imported": 2},
    )

    assert cli.main(["import-monitorrent", "--db", str(tmp_path / "mt.db")]) == 0
    out = capsys.readouterr().out
    assert seen == [(tmp_path / "mt.db", False)]  # never applies unless asked
    assert "imported: 2" in out
    assert "{" not in out


def _passphrases(monkeypatch, *answers: str) -> None:
    replies = iter(answers)
    monkeypatch.setattr("getpass.getpass", lambda _prompt: next(replies))


def test_export_refuses_mismatched_passphrases(monkeypatch, capsys, tmp_path):
    _passphrases(monkeypatch, "correct horse battery", "correct horse battery!")
    output = tmp_path / "x.towx"

    assert cli.main(["export", "--output", str(output), "--json"]) == 3
    assert json.loads(capsys.readouterr().out) == {"ok": False, "error": "export passphrases do not match"}
    assert not output.exists()


def test_export_unexpected_failure_does_not_leak_details(monkeypatch, capsys, tmp_path):
    _passphrases(monkeypatch, "correct horse battery", "correct horse battery")

    def boom(*_args, **_kwargs):
        raise OSError(r"C:\private\runtime\secret-path")

    monkeypatch.setattr("tow.bundle.export_bundle", boom)

    assert cli.main(["export", "--output", str(tmp_path / "x.towx"), "--json"]) == 3
    out = capsys.readouterr().out
    assert json.loads(out) == {"ok": False, "error": "export failed safely"}
    assert "private" not in out


def test_export_then_import_preview_round_trip(monkeypatch, capsys, tmp_path):
    output = tmp_path / "bundle.towx"
    _passphrases(monkeypatch, "correct horse battery", "correct horse battery")
    assert cli.main(["export", "--output", str(output), "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["ok"] is True
    assert output.is_file()

    _passphrases(monkeypatch, "correct horse battery")
    assert cli.main(["import", "--input", str(output), "--json"]) == 0
    preview = json.loads(capsys.readouterr().out)
    assert preview["ok"] is True
    assert preview["preview"] is True

    _passphrases(monkeypatch, "wrong passphrase!!")
    assert cli.main(["import", "--input", str(output), "--json"]) == 3
    refused = json.loads(capsys.readouterr().out)
    assert refused["ok"] is False
    assert refused["error"]


def test_import_rollback_of_missing_checkpoint_is_a_guarded_error(capsys, tmp_path):
    assert cli.main(["import-rollback", "--checkpoint", str(tmp_path / "nope"), "--json"]) == 3
    result = json.loads(capsys.readouterr().out)
    assert result["ok"] is False
    assert "Traceback" not in result["error"]


@pytest.mark.parametrize(
    ("report_ok", "secrets_fine", "code"),
    [(True, True, 0), (False, True, 2), (True, False, 3), (False, False, 3)],
)
def test_doctor_exit_codes(monkeypatch, capsys, report_ok, secrets_fine, code):
    monkeypatch.setattr("tow.doctor.doctor_report", lambda probe: {"ok": report_ok, "probe": probe})
    monkeypatch.setattr("tow.doctor.doctor_text", lambda report: "line one\nline two")
    monkeypatch.setattr(cli, "secrets_ok", lambda: secrets_fine)

    assert cli.main(["doctor"]) == code
    assert capsys.readouterr().out.strip() == "line one\nline two"


def test_doctor_json_and_notify(monkeypatch, capsys):
    sent = []
    monkeypatch.setattr("tow.doctor.doctor_report", lambda probe: {"ok": True, "checks": ["a"]})
    monkeypatch.setattr("tow.doctor.doctor_text", lambda report: "line one\nline two")
    monkeypatch.setattr("tow.notify.send", lambda secrets, text: sent.append(text) or True)
    monkeypatch.setattr(cli, "secrets_ok", lambda: True)

    assert cli.main(["doctor", "--json", "--notify"]) == 0
    assert json.loads(capsys.readouterr().out) == {"ok": True, "checks": ["a"]}
    assert sent == ["tow doctor: line one | line two"]


@pytest.fixture
def uvicorn_runs(monkeypatch):
    import uvicorn

    runs = []

    class Server:  # `tow serve` runs uvicorn.Server itself (it may watch its parent, `tow run`)
        def __init__(self, config):
            self.config = config

        def run(self):
            runs.append(self.config)

    monkeypatch.setattr(uvicorn, "Config", lambda app, **kwargs: (app, kwargs))
    monkeypatch.setattr(uvicorn, "Server", Server)
    monkeypatch.setattr(uvicorn, "run", lambda *_a, **_k: pytest.fail("a real server"))
    return runs


def test_serve_uses_config_bind_and_port(uvicorn_runs):
    assert cli.main(["serve"]) == 0

    app, kwargs = uvicorn_runs[0]
    assert app == "tow.web:app"
    assert kwargs["host"] == "127.0.0.1"
    assert kwargs["port"] == 8787
    assert kwargs["reload"] is False
    assert kwargs["log_config"] is None


def test_serve_refuses_lan_bind_without_permission(uvicorn_runs, capsys):
    assert cli.main(["serve", "--host", "0.0.0.0"]) == 3
    assert t("cli.serve_blocked", error="") in capsys.readouterr().out
    assert uvicorn_runs == []


def test_serve_with_log_file_rotates_into_it(uvicorn_runs, tmp_path):
    log_file = tmp_path / "logs" / "serve.log"

    assert cli.main(["serve", "--port", "9999", "--log-file", str(log_file)]) == 0

    kwargs = uvicorn_runs[0][1]
    assert kwargs["port"] == 9999
    handler = kwargs["log_config"]["handlers"]["file"]
    assert handler["filename"] == str(log_file)
    assert handler["class"] == "logging.handlers.RotatingFileHandler"
    assert log_file.parent.is_dir()
    assert set(kwargs["log_config"]["loggers"]) == {"uvicorn", "uvicorn.error", "uvicorn.access"}


def test_backup_reports_snapshot_or_its_error(monkeypatch, capsys):
    from tow.snapshots import SnapshotError

    monkeypatch.setattr("tow.snapshots.create_snapshot", lambda: {"ok": True, "name": "snap-1"})
    assert cli.main(["backup", "--json"]) == 0
    assert json.loads(capsys.readouterr().out)["name"] == "snap-1"

    def refuse():
        raise SnapshotError("backup_dir is not configured")

    monkeypatch.setattr("tow.snapshots.create_snapshot", refuse)
    assert cli.main(["backup", "--json"]) == 3
    assert json.loads(capsys.readouterr().out) == {"ok": False, "error": "backup_dir is not configured"}


def test_restore_snapshot_error_is_exit_3(monkeypatch, capsys, tmp_path):
    from tow.snapshots import SnapshotError

    def refuse(path, *, apply):
        raise SnapshotError("snapshot is damaged")

    monkeypatch.setattr("tow.snapshots.restore_snapshot", refuse)

    assert cli.main(["restore-snapshot", "--path", str(tmp_path), "--json"]) == 3
    assert json.loads(capsys.readouterr().out)["error"] == "snapshot is damaged"


def test_restore_snapshot_needs_no_scheduler_for_a_changed_interval(monkeypatch, capsys, tmp_path):
    # 1.21: `tow run` reads the restored interval from the config; there is no TOW-check task.
    monkeypatch.setattr(
        "tow.snapshots.restore_snapshot",
        lambda path, *, apply: {"ok": True, "applied": apply, "interval_changed": {"from": 600, "to": 900}},
    )

    assert cli.main(["restore-snapshot", "--path", str(tmp_path), "--apply", "--json"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["applied"] is True
    assert "check_task" not in result


@pytest.mark.parametrize(
    ("service_ok", "checks_ok", "code"),
    [(True, True, 0), (False, True, 2), (True, False, 2)],
)
def test_watchdog_exit_code_follows_service_and_checks(monkeypatch, capsys, service_ok, checks_ok, code):
    monkeypatch.setattr("tow.watchdog.run_watchdog", lambda: {"service_ok": service_ok, "checks_ok": checks_ok})

    assert cli.main(["watchdog", "--json"]) == code
    assert json.loads(capsys.readouterr().out)["service_ok"] is service_ok


def test_interrupt_is_exit_130(monkeypatch):
    def interrupted():
        raise KeyboardInterrupt

    monkeypatch.setattr("tow.watchdog.run_watchdog", interrupted)

    assert cli.main(["watchdog"]) == 130


def test_unregistered_command_is_exit_1(monkeypatch):
    monkeypatch.delitem(cli._COMMANDS, "version")

    assert cli.main(["version"]) == 1


def test_module_entry_point_exits_with_the_command_code(monkeypatch, capsys):
    monkeypatch.setattr(sys, "argv", ["tow", "version", "--json"])

    with warnings.catch_warnings():
        warnings.simplefilter("ignore", RuntimeWarning)  # tow.cli is already imported
        with pytest.raises(SystemExit) as exited:
            runpy.run_module("tow.cli", run_name="__main__")

    assert exited.value.code == 0
    assert "version" in json.loads(capsys.readouterr().out)


# --------------------------------------------------------------------------- restore points


def _text(name: str, **params) -> str:
    """A restore-point message in the test's language (its config), as the code writes it."""
    return t(f"backup.restore_point.{name}", **params)


def _says(name: str, **params) -> str:
    return re.escape(_text(name, **params))


def _seed(monkeypatch, tmp_path: Path) -> None:
    home = tmp_path / "data"
    config_path = tmp_path / "config.yaml"
    monkeypatch.setenv("TOW_HOME", str(home))
    monkeypatch.setenv("TOW_CONFIG", str(config_path))
    monkeypatch.setenv("TOW_MASTER_KEY", Fernet.generate_key().decode("ascii"))
    monkeypatch.delenv("TOW_MASTER_KEY_FILE", raising=False)
    config_path.write_text(
        yaml.safe_dump(
            {
                "bind": "127.0.0.1",
                "port": 8787,
                "allow_lan": False,
                "lan_auth": True,
                "interval_sec": 111,
                "trackers": {},
                "client": {"kind": "qbittorrent"},
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    save_state({"topics": [{"id": "before"}], "mirrors": {}})
    (home / "download_history.json").write_text(json.dumps({"schema_version": 1, "topics": {}}), encoding="utf-8")
    save_secrets({"telegram": {"token": "test-secret"}})


def _fake_point(stamp: str, nonce: str = "abcdef12", content: bytes = b"bundle") -> Path:
    root = restore_points_dir()
    root.mkdir(parents=True, exist_ok=True)
    path = root / f"{stamp}-{nonce}.towx"
    path.write_bytes(content)
    return path


def _stub_export(monkeypatch) -> None:
    monkeypatch.setenv("TOW_MASTER_KEY", Fernet.generate_key().decode("ascii"))
    monkeypatch.setattr(
        restore_points,
        "export_bundle",
        lambda path, _passphrase, **_kwargs: Path(path).write_bytes(b"bundle") and {"ok": True},
    )
    # Synthetic archives stand in for this installation's verified copies.
    monkeypatch.setattr(restore_points, "verify_bundle", lambda *_args: None)


def test_list_is_newest_first_and_skips_damaged_entries():
    assert list_restore_points() == []  # no directory yet

    _fake_point("20260101T000000Z", "00000001")
    _fake_point("20260301T000000Z", "00000003")
    _fake_point("20260201T000000Z", "00000002", content=b"12345")
    _fake_point("20260401T000000Z", "00000004", content=b"")  # empty: unusable
    _fake_point("20261399T000000Z", "00000005")  # impossible timestamp
    (restore_points_dir() / "notes.txt").write_text("x", encoding="utf-8")
    (restore_points_dir() / "20260501T000000Z-00000006.bak").write_bytes(b"x")

    points = list_restore_points()

    assert [point["id"] for point in points] == [
        "20260301T000000Z-00000003",
        "20260201T000000Z-00000002",
        "20260101T000000Z-00000001",
    ]
    assert points[1] == {
        "id": "20260201T000000Z-00000002",
        "created_at": "2026-02-01T00:00:00+00:00",
        "bytes": 5,
    }


def test_list_of_an_unreadable_directory_is_an_error(monkeypatch):
    _fake_point("20260101T000000Z")

    def unreadable(self):
        raise PermissionError("denied")

    monkeypatch.setattr(Path, "iterdir", unreadable)

    with pytest.raises(RestorePointError, match=_says("cannot_read")):
        list_restore_points()


def test_create_rotates_to_the_limit_but_keeps_protected_points(monkeypatch):
    _stub_export(monkeypatch)
    old = [_fake_point(f"202001{day:02d}T000000Z", f"{day:08x}").stem for day in range(1, 13)]
    protected = old[0]  # the oldest one

    created = create_restore_point(protected={protected})

    ids = [point["id"] for point in list_restore_points()]
    assert len(ids) == restore_points.RESTORE_POINT_LIMIT
    assert ids[0] == created["id"]
    assert protected in ids
    assert ids[1:-1] == list(reversed(old[-8:]))


@pytest.mark.skipif(os.name != "nt", reason="read-only files refuse deletion only on Windows")
def test_create_that_cannot_rotate_keeps_its_new_point_with_warning():
    old = [_fake_point(f"202001{day:02d}T000000Z", f"{day:08x}") for day in range(1, 11)]
    old[0].chmod(stat.S_IREAD)
    try:
        with pytest.MonkeyPatch.context() as monkeypatch:
            _stub_export(monkeypatch)
            created = create_restore_point()
        assert created["cleanup_warning"] == _text("cleanup_warning")
        assert {point["id"] for point in list_restore_points()} == {created["id"], *(path.stem for path in old)}
    finally:
        old[0].chmod(stat.S_IREAD | stat.S_IWRITE)


def test_create_without_master_key_fails_and_leaves_nothing(monkeypatch, tmp_path):
    monkeypatch.delenv("TOW_MASTER_KEY", raising=False)
    monkeypatch.delenv("TOW_MASTER_KEY_FILE", raising=False)

    with pytest.raises(RestorePointError, match=_says("cannot_create", reason=_text("master_key"))):
        create_restore_point()
    with pytest.raises(RestorePointError, match=f"^{_says('master_key')}$"):
        export_portable_bundle(tmp_path / "portable.towx")

    assert list(restore_points_dir().iterdir()) == []
    assert not (tmp_path / "portable.towx").exists()


def test_create_when_the_directory_name_is_taken_by_a_file(monkeypatch):
    _stub_export(monkeypatch)
    restore_points_dir().write_text("not a directory", encoding="utf-8")

    with pytest.raises(RestorePointError, match=_says("cannot_create_dir")) as caught:
        create_restore_point()
    assert caught.value.kind == restore_points.CREATE_FAILED
    assert list_restore_points() == []


def test_create_whose_bundle_is_not_written_fails_read_back(monkeypatch):
    monkeypatch.setenv("TOW_MASTER_KEY", Fernet.generate_key().decode("ascii"))
    monkeypatch.setattr(restore_points, "export_bundle", lambda *_args, **_kwargs: {"ok": True})

    with pytest.raises(RestorePointError, match=_says("read_back_failed")):
        create_restore_point()


@pytest.mark.skipif(os.name != "nt", reason="directory junctions are a Windows feature")
def test_junctions_are_never_trusted_as_restore_points(monkeypatch, tmp_path):
    import _winapi

    _stub_export(monkeypatch)
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    root = restore_points_dir()
    root.mkdir()
    point_id = "20260101T000000Z-abcdef12"
    _winapi.CreateJunction(str(elsewhere), str(root / f"{point_id}.towx"))

    assert list_restore_points() == []
    with pytest.raises(RestorePointError, match=_says("unsafe")):
        restore_from_point(point_id)

    (root / f"{point_id}.towx").rmdir()
    root.rmdir()
    _winapi.CreateJunction(str(elsewhere), str(root))
    assert list_restore_points() == []
    with pytest.raises(RestorePointError, match=_says("unsafe_dir")):
        create_restore_point()
    assert list(elsewhere.iterdir()) == []


@pytest.mark.parametrize("content", [None, b""])
def test_restore_of_a_missing_or_empty_point_is_refused(content):
    point_id = "20260101T000000Z-abcdef12"
    if content is not None:
        _fake_point("20260101T000000Z", content=content)

    with pytest.raises(RestorePointError, match=_says("missing")) as caught:
        restore_from_point(point_id)
    assert caught.value.kind == restore_points.UNKNOWN_POINT


def test_portable_export_never_overwrites_an_existing_file(monkeypatch, tmp_path):
    _seed(monkeypatch, tmp_path)
    output = tmp_path / "portable.towx"
    output.write_bytes(b"keep me")

    with pytest.raises(RestorePointError, match=_says("cannot_create_portable")):
        export_portable_bundle(output)

    assert output.read_bytes() == b"keep me"


def test_portable_check_rejects_garbage_and_failed_previews(monkeypatch, tmp_path):
    _seed(monkeypatch, tmp_path)
    garbage = tmp_path / "garbage.towx"
    garbage.write_bytes(b"not a bundle")

    with pytest.raises(RestorePointError, match=_says("portable_invalid")) as caught:
        check_portable_bundle(garbage)
    assert caught.value.kind == restore_points.INVALID_FILE

    monkeypatch.setattr(restore_points, "import_bundle", lambda *_args, **_kwargs: {"ok": True, "preview": False})
    with pytest.raises(RestorePointError, match=_says("portable_check_failed")):
        check_portable_bundle(garbage)


def _changed_after_point(monkeypatch, tmp_path) -> str:
    _seed(monkeypatch, tmp_path)
    saved = create_restore_point()
    save_state({"topics": [{"id": "after"}], "mirrors": {}})
    return str(saved["id"])


def test_restore_whose_preview_is_not_ok_changes_nothing(monkeypatch, tmp_path):
    point_id = _changed_after_point(monkeypatch, tmp_path)
    monkeypatch.setattr(restore_points, "import_bundle", lambda *_args, **_kwargs: {"ok": False})

    with pytest.raises(RestorePointError, match=_says("validation_failed")) as caught:
        restore_from_point(point_id)
    assert caught.value.kind == restore_points.INVALID_FILE

    assert len(list_restore_points()) == 1  # no safety point either
    assert load_state()["topics"] == [{"id": "after"}]


def test_restore_that_loses_lan_access_is_rolled_back(monkeypatch, tmp_path):
    point_id = _changed_after_point(monkeypatch, tmp_path)
    real_import = restore_points.import_bundle

    def import_then_drift(path, passphrase, **kwargs):
        result = real_import(path, passphrase, **kwargs)
        if kwargs.get("apply"):
            drifted = load_config()
            drifted["port"] = 9999
            save_config(drifted)
        return result

    monkeypatch.setattr(restore_points, "import_bundle", import_then_drift)

    with pytest.raises(RestorePointError, match=_says("lan_access_failed")) as caught:
        restore_from_point(point_id)
    assert caught.value.kind == restore_points.RESTORE_FAILED

    assert load_config()["port"] == 8787
    assert load_state()["topics"] == [{"id": "after"}]
    assert len(list_restore_points()) == 2  # the safety point taken before applying


def test_restore_whose_rollback_also_fails_says_so(monkeypatch, tmp_path):
    point_id = _changed_after_point(monkeypatch, tmp_path)
    real_import = restore_points.import_bundle

    def import_then_drift(path, passphrase, **kwargs):
        result = real_import(path, passphrase, **kwargs)
        if kwargs.get("apply"):
            drifted = load_config()
            drifted["allow_lan"] = True
            save_config(drifted)
        return result

    def rollback_fails(_checkpoint, *, apply):
        raise ExportImportError("cannot roll back import checkpoint safely")

    monkeypatch.setattr(restore_points, "import_bundle", import_then_drift)
    monkeypatch.setattr(restore_points, "rollback_import", rollback_fails)

    with pytest.raises(RestorePointError, match=_says("rollback_failed")) as caught:
        restore_from_point(point_id)
    assert caught.value.kind == restore_points.ROLLBACK_FAILED


def test_restore_apply_error_keeps_current_data_without_rollback(monkeypatch, tmp_path):
    point_id = _changed_after_point(monkeypatch, tmp_path)
    real_import = restore_points.import_bundle
    rollbacks = []

    def apply_fails(path, passphrase, **kwargs):
        if kwargs.get("apply"):
            raise OSError("disk full")
        return real_import(path, passphrase, **kwargs)

    monkeypatch.setattr(restore_points, "import_bundle", apply_fails)
    monkeypatch.setattr(restore_points, "rollback_import", lambda checkpoint, *, apply: rollbacks.append(checkpoint))

    with pytest.raises(RestorePointError, match=_says("data_kept")) as caught:
        restore_from_point(point_id)
    assert caught.value.kind == restore_points.RESTORE_FAILED

    assert rollbacks == []
    assert load_state()["topics"] == [{"id": "after"}]
