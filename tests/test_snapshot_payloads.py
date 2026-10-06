"""A signed, hash-correct copy must still be readable before restore or retention."""

from __future__ import annotations

import hashlib
import json
import stat
from pathlib import Path
from types import SimpleNamespace

import pytest

from tow import snapshots, store
from tow.config import load_config, save_config
from tow.i18n import t
from tow.paths import config_path, data_dir, download_history_path, state_path


@pytest.fixture
def point(tmp_path, monkeypatch):
    monkeypatch.setenv("TOW_HOME", str(tmp_path / "data"))
    cfg = load_config()
    cfg.update(backup_dir=str(tmp_path / "night"), backup_keep=1)
    save_config(cfg)
    store.save_state({"topics": [{"id": "before"}], "mirrors": {}})
    store.save_download_history({"schema_version": 1, "topics": {"fixture": {"items": {}}}})
    store.save_secrets({"telegram": {"token": "synthetic-before"}})
    store.save_secret_undo({"telegram": {"token": "synthetic-undo"}})
    return Path(snapshots.create_snapshot()["snapshot"])


def _replace(point, name, content, *, signed=True):
    (point / name).write_bytes(content)
    path = point / "MANIFEST.json"
    manifest = json.loads(path.read_bytes())
    manifest["files"][name] = {"size": len(content), "sha256": hashlib.sha256(content).hexdigest()}
    if signed:
        manifest["signature"] = snapshots._signature(manifest, snapshots._signing_key())
    else:
        manifest["format"] = snapshots.UNSIGNED_FORMAT
        manifest.pop("signature", None)
    path.write_text(json.dumps(manifest), encoding="utf-8")


def _read_live():
    paths = [
        config_path(),
        state_path(),
        download_history_path(),
        store.encrypted_secrets_path(),
        store.secret_undo_path(),
    ]
    return {path: path.read_bytes() for path in paths}


def _inspect(point, surface):
    if surface == "verify":
        return snapshots.verify_snapshot(point)
    return snapshots.restore_snapshot(point, apply=surface == "apply")


BAD_STORES = [
    pytest.param("state.json", b"{broken", id="state-syntax"),
    pytest.param("state.json", b"\xff", id="state-unicode"),
    pytest.param("state.json", b"null", id="state-null"),
    pytest.param("state.json", b"[]", id="state-array"),
    pytest.param("state.json", b'{"topics":{}}', id="state-topics"),
    pytest.param("state.json", b'{"topics":[false]}', id="state-topic"),
    pytest.param("state.json", b'{"mirrors":[]}', id="state-mirrors"),
    pytest.param("state.json", b'{"schema_version":true}', id="state-bool-version"),
    pytest.param("state.json", b'{"schema_version":-1}', id="state-negative-version"),
    pytest.param("state.json", b'{"schema_version":"synthetic-private"}', id="state-text-version"),
    pytest.param("state.json", b'{"schema_version":999}', id="state-future-version"),
    pytest.param("state.json", b'{"extra":NaN}', id="state-nan"),
    pytest.param("state.json", b'{"extra":1e9999}', id="state-overflow"),
    pytest.param("state.json", b'{"extra":' + b"[" * 130 + b"0" + b"]" * 130 + b"}", id="state-depth"),
    pytest.param("state.json", b"[" * 20000 + b"0" + b"]" * 20000, id="state-parser-recursion"),
    pytest.param("download_history.json", b"{broken", id="history-syntax"),
    pytest.param("download_history.json", b"\xff", id="history-unicode"),
    pytest.param("download_history.json", b"[]", id="history-array"),
    pytest.param("download_history.json", b'{"topics":[]}', id="history-topics"),
    pytest.param("download_history.json", b'{"topics":{"fixture":false}}', id="history-topic"),
    pytest.param("download_history.json", b'{"topics":{"fixture":{"items":[]}}}', id="history-items"),
    pytest.param("download_history.json", b'{"topics":{"fixture":{"items":{"item":false}}}}', id="history-item"),
    pytest.param("download_history.json", b'{"extra":Infinity}', id="history-infinite"),
    pytest.param("download_history.json", b'{"extra":' + b"[" * 130 + b"0" + b"]" * 130 + b"}", id="history-depth"),
]


@pytest.mark.parametrize(("name", "content"), BAD_STORES)
@pytest.mark.parametrize("surface", ["verify", "preview", "apply"])
def test_hash_correct_but_unreadable_store_is_refused_before_any_live_write(point, name, content, surface):
    _replace(point, name, content)
    store.save_state({"topics": [{"id": "current"}], "mirrors": {}})
    before = _read_live()
    with pytest.raises(snapshots.SnapshotError) as failed:
        _inspect(point, surface)
    assert str(failed.value) == t("backup.snapshot.file_unusable", "ru", name=name)
    assert _read_live() == before
    assert (point / name).read_bytes() == content
    assert not (data_dir() / snapshots._MARKER).exists()
    assert not list(data_dir().glob("before-restore-*"))
    assert not list(data_dir().glob("*.corrupt-*"))
    assert "synthetic-private" not in str(failed.value)


@pytest.mark.parametrize(("name", "content"), BAD_STORES)
def test_unreadable_live_source_never_prunes_the_previous_usable_copy(point, name, content):
    source = state_path() if name == "state.json" else download_history_path()
    source.write_bytes(content)
    before = _read_live()
    good_manifest = (point / "MANIFEST.json").read_bytes()
    previous_status = snapshots.status()
    with pytest.raises(snapshots.SnapshotError):
        snapshots.create_snapshot()
    assert _read_live() == before
    assert (point / "MANIFEST.json").read_bytes() == good_manifest
    assert snapshots.verify_snapshot(point)["signed"]
    status = snapshots.status()
    assert status["last_ok_at"] == previous_status["last_ok_at"]
    assert status["last_snapshot"] == point.name
    assert status["last_error"]


def _bad_encrypted(name, case):
    format_value = "tow-secrets/v1" if name == "secrets.enc" else "tow-secrets-undo/v1"
    if case == "syntax":
        return b"{broken"
    if case == "unicode":
        return b"\xff"
    if case == "null":
        return b"null"
    raw = b'{"synthetic": "secret"}' if name == "secrets.enc" else b'{"secrets":{"synthetic":"secret"}}'
    if case.startswith("payload-"):
        raw = {
            "payload-syntax": b"{broken",
            "payload-unicode": b"\xff",
            "payload-array": b"[]",
            "payload-infinite": b'{"synthetic":NaN}',
            "payload-depth": b'{"synthetic":' + b"[" * 130 + b"0" + b"]" * 130 + b"}",
            "payload-undo": b'{"secrets":false}',
        }[case]
    envelope = {"format": format_value, "cipher": "fernet", "token": store.master_fernet().encrypt(raw).decode("ascii")}
    if case == "format":
        envelope["format"] = "tow-secrets-unknown/v1"
    elif case == "swapped-format":
        envelope["format"] = "tow-secrets-undo/v1" if name == "secrets.enc" else "tow-secrets/v1"
    elif case == "cipher":
        envelope["cipher"] = "unsupported"
    elif case == "missing-token":
        del envelope["token"]
    elif case == "empty-token":
        envelope["token"] = ""
    elif case == "token":
        envelope["token"] = "synthetic-private"
    return json.dumps(envelope).encode("utf-8")


SECRET_CASES = [
    "syntax",
    "unicode",
    "null",
    "format",
    "swapped-format",
    "cipher",
    "missing-token",
    "empty-token",
    "token",
    "payload-syntax",
    "payload-unicode",
    "payload-array",
    "payload-infinite",
    "payload-depth",
]


@pytest.mark.parametrize("case", SECRET_CASES)
@pytest.mark.parametrize("name", ["secrets.enc", "secrets-undo.enc"])
@pytest.mark.parametrize("surface", ["verify", "preview", "apply"])
def test_encrypted_content_is_fully_checked_not_merely_decrypted(point, name, case, surface):
    content = _bad_encrypted(name, case)
    _replace(point, name, content)
    before = _read_live()
    with pytest.raises(snapshots.SnapshotError) as failed:
        _inspect(point, surface)
    assert str(failed.value) == t("backup.snapshot.secrets_unusable", "ru", name=name)
    assert _read_live() == before
    assert (point / name).read_bytes() == content
    assert not (data_dir() / snapshots._MARKER).exists()
    assert not list(data_dir().glob("before-restore-*"))
    assert "synthetic-private" not in str(failed.value)


@pytest.mark.parametrize("surface", ["verify", "preview", "apply", "create"])
def test_decryptable_undo_must_contain_a_settings_mapping(point, surface):
    content = _bad_encrypted("secrets-undo.enc", "payload-undo")
    if surface == "create":
        store.secret_undo_path().write_bytes(content)
    else:
        _replace(point, "secrets-undo.enc", content)
    before = _read_live()
    with pytest.raises(snapshots.SnapshotError):
        snapshots.create_snapshot() if surface == "create" else _inspect(point, surface)
    assert _read_live() == before
    assert point.is_dir()


@pytest.mark.parametrize("case", SECRET_CASES)
@pytest.mark.parametrize("name", ["secrets.enc", "secrets-undo.enc"])
def test_bad_encrypted_source_keeps_the_previous_verified_copy(point, name, case):
    source = store.encrypted_secrets_path() if name == "secrets.enc" else store.secret_undo_path()
    source.write_bytes(_bad_encrypted(name, case))
    before = _read_live()
    with pytest.raises(snapshots.SnapshotError):
        snapshots.create_snapshot()
    assert _read_live() == before
    assert snapshots.verify_snapshot(point)["signed"]


@pytest.mark.parametrize("name", ["state.json", "download_history.json"])
@pytest.mark.parametrize("signed", [False, True])
def test_valid_legacy_containers_remain_supported_without_rewriting_bytes(point, name, signed):
    content = b" { } \n"
    _replace(point, name, content, signed=signed)
    before = _read_live()
    assert snapshots.verify_snapshot(point)["signed"] is signed
    assert snapshots.restore_snapshot(point)["signed"] is signed
    assert _read_live() == before
    assert (point / name).read_bytes() == content
    if signed:
        assert snapshots.restore_snapshot(point, apply=True)["applied"]
        target = state_path() if name == "state.json" else download_history_path()
        assert target.read_bytes() == content


@pytest.mark.parametrize("name", ["state.json", "download_history.json", "secrets.enc", "secrets-undo.enc"])
def test_unsigned_legacy_preview_also_refuses_unreadable_contents(point, name):
    _replace(point, name, b"{broken", signed=False)
    before = _read_live()
    for surface in ("verify", "preview"):
        with pytest.raises(snapshots.SnapshotError):
            _inspect(point, surface)
    assert _read_live() == before


def test_normal_copy_restores_encrypted_undo_readable_by_the_live_store(point):
    store.save_secret_undo({"synthetic": "current"})
    assert snapshots.restore_snapshot(point, apply=True)["applied"]
    assert store.load_secret_undo("settings-v1") == {"telegram": {"token": "synthetic-undo"}}


def test_bad_signature_is_rejected_before_any_payload_parser(point, monkeypatch):
    path = point / "MANIFEST.json"
    manifest = json.loads(path.read_bytes())
    manifest["signature"] = "invalid"
    path.write_text(json.dumps(manifest), encoding="utf-8")

    def unexpected(_content):
        pytest.fail("untrusted contents reached the payload parser")

    monkeypatch.setattr(snapshots, "_check_payloads", unexpected)
    with pytest.raises(snapshots.SnapshotError) as failed:
        snapshots.verify_snapshot(point)
    assert str(failed.value) == t("backup.snapshot.bad_signature", "ru")


@pytest.mark.parametrize("name", ["state.json", "download_history.json"])
def test_bad_hash_is_rejected_before_semantic_validation(point, name, monkeypatch):
    (point / name).write_bytes(b"{broken")

    def unexpected(_content):
        pytest.fail("unchecked bytes reached the payload parser")

    monkeypatch.setattr(snapshots, "_check_payloads", unexpected)
    with pytest.raises(snapshots.SnapshotError) as failed:
        snapshots.verify_snapshot(point)
    assert str(failed.value) == t("backup.snapshot.file_damaged", "ru", name=name)


@pytest.mark.parametrize("surface", ["verify", "preview"])
def test_opaque_snapshot_members_are_hashed_in_bounded_blocks(point, surface, monkeypatch):
    content = b"x" * (3 * snapshots._READ_CHUNK_BYTES + 37)
    _replace(point, "tow.jsonl", content)
    member = point / "tow.jsonl"
    real_open = Path.open
    reads = []

    class ChunkReader:
        def __init__(self, handle):
            self.handle = handle

        def __enter__(self):
            return self

        def __exit__(self, *args):
            self.handle.close()

        def fileno(self):
            return self.handle.fileno()

        def read(self, size=-1):
            assert 0 < size <= snapshots._READ_CHUNK_BYTES
            reads.append(size)
            return self.handle.read(size)

    def watched_open(path, *args, **kwargs):
        handle = real_open(path, *args, **kwargs)
        return ChunkReader(handle) if path == member and args == ("rb",) else handle

    before = _read_live()
    monkeypatch.setattr(Path, "open", watched_open)
    assert _inspect(point, surface)["signed"]
    assert len(reads) == 5  # three blocks, the remainder, then EOF; no retained/second opaque read
    assert _read_live() == before


def test_verification_releases_members_and_validates_one_store_at_a_time(point, monkeypatch):
    calls = []
    original = snapshots._check_payloads

    def watched(contents):
        calls.append(tuple(contents))
        return original(contents)

    monkeypatch.setattr(snapshots, "_check_payloads", watched)
    manifest, contents, signed = snapshots._read_verified(point, retain=False)
    assert signed
    assert manifest["files"]
    assert contents == {}
    assert calls == [("state.json",), ("download_history.json",), ("secrets.enc",), ("secrets-undo.enc",)]


@pytest.mark.parametrize("surface", ["verify", "preview", "apply"])
@pytest.mark.parametrize(
    ("member", "mode", "attributes"),
    [
        ("state.json", stat.S_IFIFO, 0),
        ("state.json", stat.S_IFLNK, 0),
        ("state.json", stat.S_IFREG, stat.FILE_ATTRIBUTE_REPARSE_POINT),
        ("MANIFEST.json", stat.S_IFLNK, 0),
        ("", stat.S_IFLNK, 0),
    ],
)
def test_snapshot_special_files_and_links_are_refused_before_open_or_write(
    point, surface, member, mode, attributes, monkeypatch
):
    unsafe = point / member if member else point
    real_lstat, real_open = Path.lstat, Path.open
    opened = []

    def watched_lstat(path, *args, **kwargs):
        if path == unsafe:
            return SimpleNamespace(st_mode=mode, st_file_attributes=attributes)
        return real_lstat(path, *args, **kwargs)

    def watched_open(path, *args, **kwargs):
        if path == unsafe:
            opened.append(path)
        return real_open(path, *args, **kwargs)

    before = _read_live()
    monkeypatch.setattr(Path, "lstat", watched_lstat)
    monkeypatch.setattr(Path, "open", watched_open)
    with pytest.raises(snapshots.SnapshotError):
        _inspect(point, surface)
    assert not opened
    assert _read_live() == before


def test_preview_checks_again_before_parsing_a_changed_semantic_store(point, monkeypatch):
    name = "download_history.json"
    original = snapshots._verified_member
    calls = 0

    def watched(source, member, meta, *, collect):
        nonlocal calls
        if member == name:
            calls += 1
            if calls == 2:
                source.write_bytes(b"{broken")
        return original(source, member, meta, collect=collect)

    monkeypatch.setattr(snapshots, "_verified_member", watched)
    before = _read_live()
    with pytest.raises(snapshots.SnapshotError) as failed:
        snapshots.restore_snapshot(point)
    assert str(failed.value) == t("backup.snapshot.file_damaged", "ru", name=name)
    assert calls == 2
    assert _read_live() == before


def test_applied_restore_writes_the_original_verified_bytes_not_a_changed_source(point, monkeypatch):
    original = snapshots._restore_plan
    state = (point / "state.json").read_bytes()

    def watched(contents, *args):
        (point / "state.json").write_bytes(b"changed after verification")
        assert contents["state.json"] == state
        return original(contents, *args)

    monkeypatch.setattr(snapshots, "_restore_plan", watched)
    assert snapshots.restore_snapshot(point, apply=True)["applied"]
    assert state_path().read_bytes() == state


def test_unsigned_apply_is_refused_before_allocating_or_parsing_any_member(point, monkeypatch):
    _replace(point, "state.json", (point / "state.json").read_bytes(), signed=False)

    def unexpected(*args, **kwargs):
        pytest.fail("an unsigned apply read a member")

    before = _read_live()
    monkeypatch.setattr(snapshots, "_verified_member", unexpected)
    with pytest.raises(snapshots.SnapshotError) as failed:
        snapshots.restore_snapshot(point, apply=True)
    assert str(failed.value) == t("backup.snapshot.unsigned", "ru")
    assert _read_live() == before


@pytest.mark.parametrize("command", ["verify", "apply", "create"])
def test_cli_reports_a_readable_failure_without_changing_live_data(point, command, capsys):
    from tow.cli import main

    if command == "create":
        state_path().write_bytes(b"{broken")
        argv = ["backup", "--json"]
    else:
        _replace(point, "state.json", b"{broken")
        argv = ["restore-snapshot", "--path", str(point), "--json"]
        if command == "apply":
            argv.append("--apply")
    before = _read_live()
    assert main(argv) == 3
    result = json.loads(capsys.readouterr().out)
    assert result["ok"] is False
    assert t("backup.snapshot.file_unusable", "ru", name="state.json") in result["error"]
    assert _read_live() == before
    assert point.is_dir()


@pytest.mark.parametrize("action", ["create", "restore"])
def test_web_reports_the_reason_not_success_and_preserves_previous_data(point, action):
    from fastapi.testclient import TestClient
    from helpers import flash_of

    from tow.web import app

    if action == "create":
        state_path().write_bytes(b"{broken")
        url = "/settings/backup/now"
    else:
        _replace(point, "state.json", b"{broken")
        url = f"/settings/backup/night/{point.name}/restore"
    before = _read_live()
    response = TestClient(app, headers={"Origin": "http://127.0.0.1"}).post(url, follow_redirects=False)
    assert response.status_code == 303
    assert t("backup.snapshot.file_unusable", "ru", name="state.json") in flash_of(response.headers["location"])
    assert _read_live() == before
    assert point.is_dir()


def test_next_healthy_copy_clears_failure_and_can_prune_normally(point):
    content = state_path().read_bytes()
    state_path().write_bytes(b"{broken")
    with pytest.raises(snapshots.SnapshotError):
        snapshots.create_snapshot()
    assert snapshots.status()["last_error"]
    state_path().write_bytes(content)
    newest = Path(snapshots.create_snapshot()["snapshot"])
    assert newest != point
    assert not point.exists()
    assert snapshots.verify_snapshot(newest)["signed"]
    assert snapshots.status()["last_error"] == ""
    assert snapshots.status()["last_snapshot"] == newest.name
