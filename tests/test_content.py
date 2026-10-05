from __future__ import annotations

import copy
import json

import pytest
from fastapi.testclient import TestClient
from helpers import multi_file_torrent

from tow import content
from tow.errors import TowError
from tow.paths import tmp_dir
from tow.selection import normalize_policy, policy_from_topic, resolve_selection, stored_policy
from tow.store import StoreCorruptionError, load_state, save_state
from tow.torrent import TorrentFile, parse_torrent_metadata
from tow.web import app, services

URL = "https://tracker.example/topic/1"
HASH = "A" * 40
FILES = (TorrentFile(0, "Season 1/[Grp], S01E01; final*.mkv", 100), TorrentFile(1, "Season 1/S01E02.mkv", 200))


def blob():
    return multi_file_torrent(
        b"Show",
        [
            {b"path": [b"Season 1", b"[Grp], S01E01; final*.mkv"], b"length": 100},
            {b"path": [b"Season 1", b"S01E02.mkv"], b"length": 200},
        ],
    )


def exact():
    return normalize_policy("exact", files=[{"path": FILES[0].path, "size": 100}], source_hash=HASH)


def test_literal_selection_is_not_a_pattern_and_survives_indices_changing():
    policy = exact()
    assert resolve_selection(FILES, policy).selected_indices == (0,)
    moved = (TorrentFile(9, FILES[0].path, 100), TorrentFile(0, "new.mkv", 40))
    assert resolve_selection(moved, policy).selected_indices == (9,)


@pytest.mark.parametrize("files", [(), (TorrentFile(0, FILES[0].path, 101),), (TorrentFile(0, "renamed.mkv", 100),)])
def test_changed_selected_files_refuse_instead_of_selecting_everything(files):
    with pytest.raises(TowError) as error:
        resolve_selection(files, exact())
    assert error.value.code == "selection.exact_changed"


@pytest.mark.parametrize(
    "files",
    [
        None,
        [],
        "*",
        [{"path": "../a", "size": 1}],
        [{"path": "/a", "size": 1}],
        [{"path": "a", "size": True}],
        [{"path": "a", "size": -1}],
        [{"path": "a", "size": 1, "index": 0}],
        [{"path": "a", "size": 1}, {"path": "A", "size": 1}],
    ],
)
def test_bad_manual_policy_is_refused(files):
    with pytest.raises(TowError):
        normalize_policy("exact", files=files, source_hash=HASH)


def test_manual_policy_is_structured_and_durable():
    policy = exact()
    topic = {"id": "t", "selection": stored_policy(policy), "tracking_mode": "watch"}
    save_state({"topics": [topic]})
    assert policy_from_topic(load_state()["topics"][0]) == policy


def test_invalid_manual_policy_cannot_be_saved():
    save_state({"topics": []})
    with pytest.raises(StoreCorruptionError):
        save_state({"topics": [{"selection": {"mode": "exact", "files": [], "source_hash": HASH}}]})
    assert load_state()["topics"] == []


def test_preparation_is_encrypted_bound_and_does_not_create_topics():
    save_state({"topics": []})
    snapshot = content.prepare(blob(), URL, "main")
    assert content.read(snapshot["token"], URL, "main") == blob()
    encrypted = (tmp_dir() / "content" / (snapshot["token"] + ".bin")).read_bytes()
    assert URL.encode() not in encrypted
    assert b"Season 1" not in encrypted
    assert load_state()["topics"] == []
    for url, client in [(URL + "0", "main"), (URL, "other")]:
        with pytest.raises(TowError):
            content.read(snapshot["token"], url, client)


@pytest.mark.parametrize("indices", [[], [True], [0, 0], [-1], [999], "0"])
def test_prepared_selection_rejects_wrong_ids(indices):
    snapshot = content.prepare(blob(), URL, "main")
    with pytest.raises(TowError):
        content.selection(snapshot["token"], URL, "main", indices, "watch")


def test_prepared_selection_uses_server_paths_and_sizes():
    snapshot = content.prepare(blob(), URL, "main")
    policy = content.selection(snapshot["token"], URL, "main", [0], "watch")
    assert policy["files"] == [{"path": FILES[0].path, "size": 100}]
    assert policy["source_hash"] == snapshot["hash"]


def test_expired_or_modified_preparation_is_refused(monkeypatch):
    snapshot = content.prepare(blob(), URL, "main")
    monkeypatch.setattr(content, "TTL", -1)
    with pytest.raises(TowError):
        content.read(snapshot["token"], URL, "main")
    path = tmp_dir() / "content" / (snapshot["token"] + ".bin")
    path.write_bytes(b"invalid")
    with pytest.raises(TowError):
        content.read(snapshot["token"], URL, "main")


def test_full_cache_refuses_without_destroying_current_record(monkeypatch):
    first = content.prepare(blob(), URL, "main")
    monkeypatch.setattr(content, "MAX_CACHE_BYTES", 1)
    with pytest.raises(TowError):
        content.prepare(blob(), URL, "main")
    assert content.read(first["token"], URL, "main") == blob()


def test_manual_selection_import_is_semantically_checked():
    from tow.bundle import ExportImportError, _validate_state_schema

    data = {"schema_version": 2, "topics": [{"selection": stored_policy(exact())}], "mirrors": {}}
    _validate_state_schema(data)
    wrong = copy.deepcopy(data)
    wrong["topics"][0]["selection"]["files"][0]["size"] = True
    with pytest.raises(ExportImportError):
        _validate_state_schema(wrong)


def test_web_metadata_preparation_is_explicit_and_upload_is_bounded(monkeypatch):
    calls = []
    monkeypatch.setattr(services, "prepare_content", lambda *args: calls.append(args) or {"files": [], "token": "t"})
    client = TestClient(app, headers={"Origin": "http://127.0.0.1"})
    assert client.get("/").status_code == 200
    assert calls == []
    response = client.post(
        "/content/prepare", data={"url": URL, "client_id": "main"}, files={"torrent": ("show.torrent", blob())}
    )
    assert response.status_code == 200
    assert calls[0] == (URL, "main", blob(), False)
    assert response.headers["cache-control"] == "no-store"


def test_rule_preview_uses_same_engine_without_state_changes():
    save_state({"topics": []})
    snapshot = content.prepare(blob(), URL, "main")
    client = TestClient(app, headers={"Origin": "http://127.0.0.1"})
    response = client.post(
        "/content/resolve",
        data={"url": URL, "client_id": "main", "token": snapshot["token"], "mode": "episodes", "value": "S01E02"},
    )
    assert response.status_code == 200
    assert response.json()["indices"] == [1]
    assert load_state()["topics"] == []


def test_edit_without_new_preparation_preserves_manual_policy():
    from tow.web.routes_topics import _selection_form

    original = stored_policy(exact())
    result = _selection_form("exact", "", "once", "", "", URL, "main", original)
    assert stored_policy(result) == original
    assert result["tracking_mode"] == "once"


def test_manual_form_does_not_accept_paths_from_browser():
    from tow.web.routes_topics import _selection_form

    snapshot = content.prepare(blob(), URL, "main")
    with pytest.raises(TowError):
        _selection_form("exact", "", "watch", snapshot["token"], json.dumps([{"path": FILES[0].path}]), URL, "main")


def test_snapshot_restore_is_bound_and_never_fetches_tracker(monkeypatch):
    snapshot = content.prepare(blob(), URL, "main")
    monkeypatch.setattr(services, "prepare_content", lambda *_args: pytest.fail("must only read existing preparation"))
    client = TestClient(app, headers={"Origin": "http://127.0.0.1"})
    response = client.post("/content/snapshot", data={"url": URL, "client_id": "main", "token": snapshot["token"]})
    assert response.status_code == 200
    assert response.json()["files"] == snapshot["files"]
    assert response.headers["cache-control"] == "no-store"
    response = client.post("/content/snapshot", data={"url": URL, "client_id": "other", "token": snapshot["token"]})
    assert response.status_code == 400


def test_read_missing_preparation_creates_no_directory(monkeypatch, tmp_path):
    home = tmp_path / "absent-home"
    monkeypatch.setenv("TOW_HOME", str(home))
    with pytest.raises(TowError):
        content.read("a" * 32, URL, "main")
    assert not home.exists()


def test_preparation_write_failure_cannot_create_watch(monkeypatch):
    save_state({"topics": []})
    monkeypatch.setattr(content, "atomic_create_bytes", lambda *_args: (_ for _ in ()).throw(OSError("test fault")))
    with pytest.raises(OSError, match="test fault"):
        content.prepare(blob(), URL, "main")
    assert load_state()["topics"] == []


@pytest.mark.parametrize("apply", [True, False])
def test_manual_prepared_add_uses_existing_verified_client_pipeline(monkeypatch, apply):
    from test_check_contract import FakeClient, _wire_fake_check

    from tow import check

    class Client(FakeClient):
        def inspect_torrent(self, infohash):
            if not self.present:
                return None
            return {
                "hash": infohash,
                "save_path": r"M:\TV",
                "tags": ["tow"],
                "files": [
                    {
                        "index": row.index,
                        "name": row.path,
                        "size": row.size,
                        "priority": int(row.index in self.selected_indices),
                    }
                    for row in FILES
                ],
            }

    client = Client()
    tracker = _wire_fake_check(monkeypatch, client)
    monkeypatch.setattr(check, "parse_torrent_metadata", parse_torrent_metadata)
    snapshot = content.prepare(blob(), URL, "main")
    topic = {
        "id": "prepared",
        "title": "Show",
        "url": URL,
        "save_path": r"M:\TV",
        "client_id": "main",
        "selection": stored_policy(content.selection(snapshot["token"], URL, "main", [0], "watch")),
        "content_token": snapshot["token"],
        "hash": None,
    }
    save_state({"topics": [topic]})
    result = check.run_check(apply=apply, notify=False, how="test")
    row = result["results"][0]
    assert row["ok"] is True, row
    assert tracker.fetch_calls == 0
    assert client.add_calls == int(apply)
    saved = load_state()["topics"][0]
    if apply:
        assert client.selected_indices == (0,)
        assert saved["hash"] == snapshot["hash"]
        assert saved["selection_verified"] is True
        assert "content_token" not in saved
    else:
        assert saved["hash"] is None
        assert saved["content_token"] == snapshot["token"]


def test_manual_dirty_revision_change_refuses_before_client_mutation(monkeypatch):
    from test_check_contract import FakeClient, _wire_fake_check

    from tow import check

    client = FakeClient()
    tracker = _wire_fake_check(monkeypatch, client)
    monkeypatch.setattr(check, "parse_torrent_metadata", parse_torrent_metadata)
    monkeypatch.setattr(tracker, "fetch_torrent", lambda *_args, **_kwargs: blob())
    save_state(
        {
            "topics": [
                {
                    "id": "changed",
                    "title": "Show",
                    "url": URL,
                    "save_path": r"M:\TV",
                    "client_id": "main",
                    "hash": HASH,
                    "selection": stored_policy(exact()),
                    "selection_dirty": True,
                }
            ]
        }
    )
    result = check.run_check(apply=True, notify=False, how="test")
    row = result["results"][0]
    assert row["ok"] is False
    assert row["error_record"]["code"] == "selection.preview_changed"
    assert client.add_calls == 0
    assert client.configure_calls == 0
    assert load_state()["topics"][0]["hash"] == HASH


@pytest.mark.parametrize("mode", ["all", "exact"])
def test_expired_preparation_refetch_cannot_add_changed_revision(monkeypatch, mode):
    from test_check_contract import FakeClient, _wire_fake_check

    from tow import check

    client = FakeClient()
    tracker = _wire_fake_check(monkeypatch, client)
    monkeypatch.setattr(check, "parse_torrent_metadata", parse_torrent_metadata)
    monkeypatch.setattr(tracker, "fetch_torrent", lambda *_args, **_kwargs: blob())
    policy = exact() if mode == "exact" else normalize_policy("all")
    save_state(
        {
            "topics": [
                {
                    "id": "expired",
                    "title": "Show",
                    "url": URL,
                    "save_path": r"M:\TV",
                    "client_id": "main",
                    "hash": None,
                    "selection": stored_policy(policy),
                    "content_token": "a" * 32,
                    "content_hash": HASH,
                }
            ]
        }
    )
    result = check.run_check(apply=True, notify=False, how="test")
    row = result["results"][0]
    assert row["ok"] is False
    assert row["error_record"]["code"] == "selection.preview_changed"
    assert client.add_calls == 0
    assert client.configure_calls == 0


def test_expired_preparation_cannot_materialize_magnet_before_identity_check(monkeypatch):
    from test_check_contract import FakeClient, _wire_fake_check

    from tow import check

    client = FakeClient()
    tracker = _wire_fake_check(monkeypatch, client)
    monkeypatch.setattr(
        tracker, "fetch_torrent", lambda *_args, **_kwargs: (_ for _ in ()).throw(TowError("tracker.no_download_link"))
    )
    monkeypatch.setattr(
        check, "_fetch_by_magnet", lambda *_args: pytest.fail("cannot mutate a client while recovering preview")
    )
    save_state(
        {
            "topics": [
                {
                    "id": "expired",
                    "title": "Show",
                    "url": URL,
                    "save_path": r"M:\TV",
                    "client_id": "main",
                    "hash": None,
                    "selection": stored_policy(exact()),
                    "content_token": "a" * 32,
                    "content_hash": HASH,
                }
            ]
        }
    )
    row = check.run_check(apply=True, notify=False, how="test")["results"][0]
    assert row["ok"] is False
    assert client.add_calls == 0


def test_limited_metadata_requires_explicit_confirmation_and_normal_tracker_login(monkeypatch):
    from types import SimpleNamespace

    from tow import trackers

    calls = []
    tracker = SimpleNamespace(
        name="fixture",
        spec={"download_limit": True},
        fetch_torrent=lambda *args, **kwargs: calls.append(kwargs) or blob(),
    )
    monkeypatch.setattr(
        services, "load_config", lambda: {"clients": [{"id": "main", "kind": "qbittorrent", "default": True}]}
    )
    monkeypatch.setattr(trackers, "load_trackers", lambda *_args: {"fixture": tracker})
    monkeypatch.setattr(trackers, "match_tracker", lambda *_args: tracker)
    with pytest.raises(TowError) as error:
        services.prepare_content(URL, "main", None, False)
    assert error.value.code == "content.limited"
    assert calls == []
    services.prepare_content(URL, "main", None, True)
    assert calls == [{"persist": True}]
    services.prepare_content(URL, "main", blob(), False)
    assert len(calls) == 1, "local metadata never consumes a tracker download"


def test_cache_recovers_only_its_interrupted_atomic_write(monkeypatch):
    snapshot = content.prepare(blob(), URL, "main")
    folder = tmp_dir() / "content"
    interrupted = folder / ("." + "b" * 32 + ".bin.abcdef12.tmp")
    interrupted.write_bytes(b"interrupted encrypted record")
    content.prepare(blob(), URL, "main")
    assert not interrupted.exists()
    assert content.read(snapshot["token"], URL, "main") == blob()
    foreign = folder / "keep.txt"
    foreign.write_bytes(b"foreign file")
    with pytest.raises(TowError):
        content.prepare(blob(), URL, "main")
    assert foreign.read_bytes() == b"foreign file"


def test_cache_record_count_is_bounded(monkeypatch):
    snapshot = content.prepare(blob(), URL, "main")
    monkeypatch.setattr(content, "MAX_CACHE_RECORDS", 1)
    with pytest.raises(TowError) as error:
        content.prepare(blob(), URL, "main")
    assert error.value.code == "content.full"
    assert content.read(snapshot["token"], URL, "main") == blob()


def test_existing_browser_identities_do_not_round_large_sizes():
    from tow.web.templating import content_existing

    topic = {"selection": {"mode": "exact", "files": [{"path": "large.iso", "size": 2**63 - 1}]}}
    assert content_existing(topic) == [{"path": "large.iso", "size": "9223372036854775807"}]


def test_metadata_upload_limit_precedes_parsing(monkeypatch):
    monkeypatch.setattr(
        services, "prepare_content", lambda *_args: pytest.fail("oversized request reached preparation")
    )
    client = TestClient(app, headers={"Origin": "http://127.0.0.1"})
    response = client.post("/content/prepare", content=b"x", headers={"Content-Length": str(34 * 1024 * 1024)})
    assert response.status_code == 413


def test_metadata_storage_fault_has_safe_error(monkeypatch):
    monkeypatch.setattr(services, "prepare_content", lambda *_args: (_ for _ in ()).throw(OSError("private path")))
    client = TestClient(app, headers={"Origin": "http://127.0.0.1"})
    response = client.post("/content/prepare", data={"url": URL, "client_id": "main"})
    assert response.status_code == 400
    assert response.json()["code"] == "content.unavailable"
    assert "private path" not in response.text


@pytest.mark.parametrize("endpoint", ["snapshot", "prepare", "resolve"])
@pytest.mark.parametrize("kind", [OSError, ValueError, RuntimeError])
def test_every_content_boundary_hides_unexpected_exception_text(monkeypatch, endpoint, kind):
    def fail(*_args):
        raise kind("private path and tracker credential")

    monkeypatch.setattr(services, "read_content", fail)
    monkeypatch.setattr(services, "prepare_content", fail)
    client = TestClient(app, headers={"Origin": "http://127.0.0.1"})
    response = client.post(
        f"/content/{endpoint}",
        data={"url": URL, "client_id": "main", "token": "a" * 32, "mode": "all"},
    )
    assert response.status_code == 400
    assert response.json()["code"] == "content.unavailable"
    assert "private path" not in response.text
    assert "credential" not in response.text
    assert response.headers["cache-control"] == "no-store"


@pytest.mark.parametrize("endpoint", ["snapshot", "prepare", "resolve"])
def test_content_errors_render_catalog_instead_of_exception_string(monkeypatch, endpoint):
    class DiagnosticError(TowError):
        def __str__(self):
            return "private diagnostic traceback"

    def fail(*_args):
        raise DiagnosticError("content.changed")

    monkeypatch.setattr(services, "read_content", fail)
    monkeypatch.setattr(services, "prepare_content", fail)
    client = TestClient(app, headers={"Origin": "http://127.0.0.1"})
    response = client.post(
        f"/content/{endpoint}",
        data={"url": URL, "client_id": "main", "token": "a" * 32, "mode": "all"},
    )
    assert response.status_code == 400
    assert response.json()["code"] == "content.changed"
    assert response.json()["error"] == TowError("content.changed").text()
    assert "private diagnostic" not in response.text


@pytest.mark.parametrize(
    "token",
    [
        "",
        "../" + "a" * 32,
        "a" * 32 + "/..",
        "a" * 32 + "\\..",
        "/" + "a" * 31,
        "A" * 32,
        "a" * 31,
        "a" * 33,
        "a" * 31 + "\n",
        "a" * 31 + "\x00",
        "%2e%2e%2f",
    ],
)
def test_untrusted_cache_tokens_are_refused_before_any_path_access(monkeypatch, token):
    monkeypatch.setattr(content, "_folder", lambda **_kwargs: pytest.fail("invalid token accessed cache"))
    with pytest.raises(TowError) as error:
        content.read(token, URL, "main")
    assert error.value.code == "content.expired"
