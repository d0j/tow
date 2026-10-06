from __future__ import annotations

from copy import deepcopy
from types import SimpleNamespace

import pytest
from test_check_contract import FakeClient, _wire_fake_check

from tow import bundle, check
from tow.progress import reconcile_topic
from tow.selection import normalize_policy
from tow.store import load_download_history, load_state, save_state
from tow.torrent import TorrentFile

HASH = "a" * 40
CANONICAL = "Season|1/ShowA.S01E01.mkv"
NATIVE = "ShowRoot/Season_1/ShowA.S01E01.mkv"


class Client(FakeClient):
    def __init__(self, folder, **kwargs):
        super().__init__(**kwargs)
        self.folder = folder

    def inspect_torrent(self, infohash):
        if not self.present:
            return None
        return {
            "hash": infohash,
            "save_path": str(self.folder),
            "content_path": str(self.folder / "ShowRoot"),
            "tags": ["tow"],
            "files": [{"index": 0, "name": NATIVE, "size": 1, "priority": 1, "progress": 1.0}],
        }


def prepare(monkeypatch, tmp_path, *, add_error=None):
    file = tmp_path / NATIVE
    file.parent.mkdir(parents=True)
    file.write_bytes(b"x")
    policy = normalize_policy("files", "Season|1/*.mkv")
    save_state(
        {
            "topics": [
                {
                    "id": "test",
                    "title": "Show A",
                    "url": "https://tracker.example/1",
                    "save_path": str(tmp_path),
                    "selection": policy,
                }
            ]
        }
    )
    client = Client(tmp_path, add_error=add_error)
    tracker = _wire_fake_check(monkeypatch, client)
    monkeypatch.setattr(
        check,
        "parse_torrent_metadata",
        lambda _blob: SimpleNamespace(
            infohash=HASH, client_hash=HASH, name="ShowRoot", files=(TorrentFile(0, CANONICAL, 1),)
        ),
    )
    monkeypatch.setattr(check, "reconcile_topic", reconcile_topic)
    return client, tracker


def test_confirmed_add_persists_original_alias_and_progress_uses_native_disk_path(monkeypatch, tmp_path):
    client, tracker = prepare(monkeypatch, tmp_path)
    first = check.run_check(apply=True, notify=False, how="test")
    assert first["results"][0]["ok"] is True
    topic = load_state()["topics"][0]
    assert topic["file_aliases"] == {"hash": topic["hash"], "files": [{"path": CANONICAL, "size": 1}]}
    assert topic["hash"].casefold() == HASH
    history = load_download_history()["topics"]["test"]
    assert history["expected"]["total"] == 1
    assert {item["relative_path"] for item in history["items"].values()} == {NATIVE}
    assert {item["status"] for item in history["items"].values()} == {"completed"}
    second = check.run_check(apply=True, notify=False, progress_only=True, how="test")
    assert second["results"][0]["ok"] is True
    assert client.add_calls == 1
    assert tracker.fetch_calls == 1


@pytest.mark.parametrize("apply", [False, True])
def test_preview_or_failed_add_never_publishes_verified_aliases(monkeypatch, tmp_path, apply):
    client, _tracker = prepare(monkeypatch, tmp_path, add_error="synthetic refusal" if apply else None)
    before = deepcopy(load_state())
    result = check.run_check(apply=apply, notify=False, how="test")
    assert "file_aliases" not in load_state()["topics"][0]
    if not apply:
        assert load_state() == before
        assert client.add_calls == 0
    else:
        assert result["results"][0]["ok"] is False


def test_aliases_survive_portable_export_preview_and_restore_on_synthetic_stores(monkeypatch, tmp_path):
    from tow.config import load_config, save_config

    cfg = load_config()
    save_config(cfg)
    topic = {
        "id": "test",
        "hash": HASH,
        "selection": {"mode": "files", "value": "*.mkv"},
        "file_aliases": {"hash": HASH, "files": [{"path": CANONICAL, "size": 1}]},
    }
    save_state({"topics": [topic]})
    archive = tmp_path / "transfer.towx"
    monkeypatch.setattr(bundle, "KDF_ITERATIONS", 100_000)
    bundle.export_bundle(archive, "synthetic-passphrase")
    before = deepcopy(load_state())
    bundle.import_bundle(archive, "synthetic-passphrase")
    assert load_state() == before
    save_state({"topics": []})
    bundle.import_bundle(archive, "synthetic-passphrase", apply=True)
    restored = load_state()["topics"][0]
    assert restored["file_aliases"] == topic["file_aliases"]
