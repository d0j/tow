"""A4: daily data snapshot to another folder, and a restore drill."""

from __future__ import annotations

import hashlib
import json
import re
from pathlib import Path

import pytest
import yaml
from cryptography.fernet import Fernet
from helpers import flash_of

from tow.config import load_config, save_config
from tow.i18n import t
from tow.paths import data_dir
from tow.snapshots import (
    SnapshotError,
    create_snapshot,
    list_snapshots,
    restore_snapshot,
    snapshot_path,
    verify_snapshot,
)
from tow.store import load_secrets, load_state, save_secrets, save_state


def _says(key: str, **params) -> str:
    """The owner's (Russian, in tests) message ``key`` as an exact pattern."""
    return re.escape(t(key, "ru", **params))


@pytest.fixture
def backup(tmp_path_factory, monkeypatch):
    monkeypatch.setenv("TOW_MASTER_KEY", Fernet.generate_key().decode("ascii"))
    root = tmp_path_factory.mktemp("other-drive") / "TOW-backup"  # outside TOW_HOME
    cfg = load_config()
    cfg["backup_dir"] = str(root)
    cfg["backup_keep"] = 3
    save_config(cfg)
    save_state({"topics": [{"id": "before"}], "mirrors": {}})
    save_secrets({"telegram": {"token": "t-before"}})
    (data_dir() / "lan-auth.token").write_text("plaintext-lan-token", encoding="utf-8")
    (data_dir() / "master.key").write_text("never-copied", encoding="utf-8")
    (data_dir() / "browser-auth").mkdir()
    (data_dir() / "browser-auth" / "Cookies").write_text("cookie-db", encoding="utf-8")
    return root


def test_snapshot_selection_refuses_a_link_to_another_directory(backup, tmp_path):
    target = tmp_path / "elsewhere"
    target.mkdir()
    (target / "MANIFEST.json").write_text("{}", encoding="utf-8")
    link = backup / "tow-linked"
    try:
        link.symlink_to(target, target_is_directory=True)
    except OSError:
        pytest.skip("creating directory symlinks requires permission on this system")

    with pytest.raises(SnapshotError, match=_says("backup.snapshot.unknown")):
        snapshot_path(link.name)
    assert link.name not in {row["name"] for row in list_snapshots()}


def test_snapshot_selection_refuses_a_junction(backup, monkeypatch):
    candidate = backup / "tow-junction"
    candidate.mkdir(parents=True)
    (candidate / "MANIFEST.json").write_text("{}", encoding="utf-8")
    real_is_junction = Path.is_junction
    monkeypatch.setattr(Path, "is_junction", lambda path: path == candidate or real_is_junction(path))

    with pytest.raises(SnapshotError, match=_says("backup.snapshot.unknown")):
        snapshot_path(candidate.name)
    assert candidate.name not in {row["name"] for row in list_snapshots()}


def test_restore_drill_brings_everything_back(backup):
    made = create_snapshot()
    snapshot = Path(made["snapshot"])
    save_state({"topics": [{"id": "after"}], "mirrors": {}})
    save_secrets({"telegram": {"token": "t-after"}})
    cfg = load_config()
    cfg["interval_sec"] = 7200
    cfg["port"] = 8799  # this machine's access setting: kept by a restore
    save_config(cfg)

    preview = restore_snapshot(snapshot)
    assert preview["applied"] is False
    assert load_state()["topics"] == [{"id": "after"}]

    result = restore_snapshot(snapshot, apply=True)

    assert result["applied"] is True
    assert load_state()["topics"] == [{"id": "before"}]
    assert load_secrets()["telegram"]["token"] == "t-before"
    assert load_config()["interval_sec"] != 7200
    assert load_config()["port"] == 8799
    assert result["interval_changed"]["from"] == 7200
    safety = Path(result["safety_copy"])
    assert json.loads((safety / "state.json").read_text(encoding="utf-8"))["topics"] == [{"id": "after"}]


def test_plaintext_credentials_and_the_key_are_never_copied(backup):
    snapshot = Path(create_snapshot()["snapshot"])

    copied = {p.relative_to(snapshot).as_posix() for p in snapshot.rglob("*") if p.is_file()}
    assert "lan-auth.token" not in copied
    assert "master.key" not in copied
    assert not any(name.startswith("browser-auth") for name in copied)
    assert {"state.json", "secrets.enc", "config.yaml", "MANIFEST.json"} <= copied
    assert b"t-before" not in (snapshot / "secrets.enc").read_bytes()  # still encrypted


def test_night_copy_only_includes_regular_restore_point_files(backup):
    from tow.restore_points import create_restore_point, restore_points_dir

    made = create_restore_point()
    root = restore_points_dir()
    foreign = root / "unrelated.towx"
    foreign.write_bytes(b"not a restore point")
    folder = root / "20260101T000000Z-deadbeef.towx"
    folder.mkdir()
    snapshot = Path(create_snapshot()["snapshot"])
    members = verify_snapshot(snapshot)["files"]
    assert f"restore-points/{made['id']}.towx" in members
    assert "restore-points/unrelated.towx" not in members
    assert "restore-points/20260101T000000Z-deadbeef.towx" not in members
    assert foreign.read_bytes() == b"not a restore point"
    assert folder.is_dir()


def test_a_damaged_snapshot_is_refused_before_anything_changes(backup):
    snapshot = Path(create_snapshot()["snapshot"])
    (snapshot / "state.json").write_text('{"topics": []}', encoding="utf-8")
    save_state({"topics": [{"id": "current"}], "mirrors": {}})

    with pytest.raises(SnapshotError, match=re.escape(t("backup.snapshot.file_damaged", name="state.json"))):
        restore_snapshot(snapshot, apply=True)
    assert load_state()["topics"] == [{"id": "current"}]


def test_another_master_key_is_refused_before_anything_changes(backup, monkeypatch):
    snapshot = Path(create_snapshot()["snapshot"])
    monkeypatch.setenv("TOW_MASTER_KEY", Fernet.generate_key().decode("ascii"))

    with pytest.raises(SnapshotError, match=_says("backup.snapshot.bad_signature")):  # the signature catches it first
        restore_snapshot(snapshot, apply=True)


def test_old_snapshots_are_pruned_to_backup_keep(backup, monkeypatch):
    stamps = iter(f"20261001-0000{n:02d}" for n in range(10))

    class FixedNow:
        @staticmethod
        def now(_tz=None):
            class Stamp:
                def astimezone(self):
                    return self

                def strftime(self, _fmt):
                    return next(stamps)

                def isoformat(self):
                    return "2026-10-01T00:00:00+00:00"

            return Stamp()

    monkeypatch.setattr("tow.snapshots.datetime", FixedNow)
    for _ in range(5):
        create_snapshot()

    kept = sorted(p.name for p in backup.iterdir())
    assert kept == ["tow-20261001-000002", "tow-20261001-000003", "tow-20261001-000004"]


def _clock(monkeypatch, stamps):
    stamps = iter(stamps)

    class FixedNow:
        @staticmethod
        def now(_tz=None):
            class Stamp:
                def astimezone(self):
                    return self

                def strftime(self, _fmt):
                    return next(stamps)

                def isoformat(self):
                    return "2026-10-01T00:00:00+00:00"

            return Stamp()

    monkeypatch.setattr("tow.snapshots.datetime", FixedNow)


def test_a_copy_that_does_not_read_back_prunes_nothing(backup, monkeypatch):
    _clock(monkeypatch, [f"20261001-0000{n:02d}" for n in range(10)])
    for _ in range(3):
        create_snapshot()

    def unreadable(_path):
        raise SnapshotError("file damaged in the copy: state.json")

    monkeypatch.setattr("tow.snapshots.verify_snapshot", unreadable)
    with pytest.raises(SnapshotError) as failed:
        create_snapshot()

    assert str(failed.value).startswith(
        t("backup.snapshot.unverified", "ru", name="tow-20261001-000003", reason="")[:20]
    )
    kept = sorted(p.name for p in backup.iterdir())
    assert kept[:3] == ["tow-20261001-000000", "tow-20261001-000001", "tow-20261001-000002"]  # all good ones stay
    from tow.snapshots import status

    assert status()["last_error"] == str(failed.value)


def test_the_newest_copy_is_never_pruned_even_when_the_clock_went_back(backup, monkeypatch):
    cfg = load_config()
    cfg["backup_keep"] = 1
    save_config(cfg)
    _clock(monkeypatch, ["20261025-023000", "20261025-021500"])  # summer time ended in between

    create_snapshot()
    newest = Path(create_snapshot()["snapshot"])

    assert newest.is_dir()
    assert [p.name for p in backup.iterdir()] == [newest.name]


def test_missing_stores_are_reported_not_silently_skipped(backup):
    from tow.paths import download_history_path

    assert not download_history_path().exists()

    made = create_snapshot()

    assert "download_history.json" in made["missing"]
    assert "secrets-undo.enc" not in made["missing"]  # normally absent: not listed
    manifest = verify_snapshot(Path(made["snapshot"]))
    assert manifest["missing"] == made["missing"]  # signed with the rest


@pytest.mark.parametrize("name", ["state.json", "download_history.json", "secrets.enc"])
def test_a_store_present_in_an_earlier_copy_must_not_disappear_silently(backup, name):
    from tow.paths import download_history_path
    from tow.snapshots import status

    if name == "download_history.json":
        download_history_path().write_text("{}", encoding="utf-8")
    first = Path(create_snapshot()["snapshot"])
    first_ok_at = status()["last_ok_at"]
    source = {
        "state.json": data_dir() / "state.json",
        "download_history.json": download_history_path(),
        "secrets.enc": data_dir() / "secrets.enc",
    }[name]
    source.unlink()

    with pytest.raises(SnapshotError, match=_says("backup.snapshot.source_missing", name=name)):
        create_snapshot()

    assert first.is_dir()
    assert status()["last_ok_at"] == first_ok_at
    assert name in status()["last_error"]
    assert not list(backup.glob(".*.partial"))


def test_backup_now_says_what_was_not_in_the_copy(backup):

    from fastapi.testclient import TestClient

    from tow.web import app

    response = TestClient(app, headers={"Origin": "http://127.0.0.1"}).post(
        "/settings/backup/now", follow_redirects=False
    )

    flash = flash_of(response.headers["location"])
    assert t("web.backup.copy_missing", "ru", names="download_history.json") in flash


def test_backup_dir_defaults_next_to_config_and_must_be_outside_the_data(tmp_path):
    from tow.locations import NIGHT, resolve
    from tow.paths import config_path

    assert resolve("", NIGHT) == config_path().resolve().parent / "backup" / "night"  # no setting needed
    cfg = load_config()
    cfg["backup_dir"] = str(data_dir() / "inside")
    save_config(cfg)
    with pytest.raises(SnapshotError, match=re.escape(t("locations.night_outside_data"))):
        create_snapshot()


def _plant(snapshot: Path, name: str, content: bytes = b"planted", *, sign: bool = True) -> None:
    """Add a member to a copy; ``sign`` re-signs the MANIFEST as only the key holder could."""
    from tow.snapshots import _signature, _signing_key

    manifest = json.loads((snapshot / "MANIFEST.json").read_text(encoding="utf-8"))
    manifest["files"][name] = {"sha256": hashlib.sha256(content).hexdigest(), "size": len(content)}
    if sign:
        manifest["signature"] = _signature(manifest, _signing_key())
    (snapshot / "MANIFEST.json").write_text(json.dumps(manifest), encoding="utf-8")


@pytest.mark.parametrize("action", ["verify", "preview", "apply"])
@pytest.mark.parametrize("source", [b"extra: &loop [*loop]\n", b"false", b"[]"])
def test_signed_and_hashed_unsafe_yaml_is_not_reported_as_restorable(backup, source, action):
    from tow.paths import config_path

    snapshot = Path(create_snapshot()["snapshot"])
    (snapshot / "config.yaml").write_bytes(source)
    _plant(snapshot, "config.yaml", source)
    before_config = config_path().read_bytes()
    before_data = {p.relative_to(data_dir()).as_posix(): p.read_bytes() for p in data_dir().rglob("*") if p.is_file()}

    def invoke():
        if action == "verify":
            verify_snapshot(snapshot)
        else:
            restore_snapshot(snapshot, apply=action == "apply")

    with pytest.raises(SnapshotError):
        invoke()
    assert config_path().read_bytes() == before_config
    assert {
        p.relative_to(data_dir()).as_posix(): p.read_bytes() for p in data_dir().rglob("*") if p.is_file()
    } == before_data


def test_signed_oversized_config_is_refused_before_allocating_member_bytes(backup, monkeypatch):
    from tow import snapshots, yaml_guard

    snapshot = Path(create_snapshot()["snapshot"])
    monkeypatch.setattr(snapshots, "MAX_INPUT_BYTES", 8)
    reads = []
    real_open = Path.open

    def watched_open(path, *args, **kwargs):
        if path == snapshot / "config.yaml":
            reads.append(path)
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", watched_open)
    with pytest.raises(SnapshotError) as caught:
        verify_snapshot(snapshot)
    assert str(yaml_guard.YamlLimitError("yaml_limits.size")) == str(caught.value)
    assert not reads


def test_manifest_rejects_foreign_files(backup):
    snapshot = Path(create_snapshot()["snapshot"])
    _plant(snapshot, "../evil.txt", b"")

    with pytest.raises(SnapshotError, match=re.escape(t("backup.snapshot.unexpected_file", name=""))):
        verify_snapshot(snapshot)


@pytest.mark.parametrize(
    "name",
    [
        "restore-points/\\Windows\\Temp\\x",
        "restore-points//Users/user/AppData/Roaming/Microsoft/Windows/Start Menu/Programs/Startup/x.bat",
        "restore-points/../../evil.towx",
        "restore-points/20261001T000000Z-deadbeef.towx/../../x.towx",
        "restore-points/not-a-point-id.towx",
        "restore-points/20261001T000000Z-deadbeef.towx.bat",
        "restore-points/sub/20261001T000000Z-deadbeef.towx",
    ],
)
def test_a_signed_copy_still_cannot_write_outside_the_restore_points_folder(backup, name):
    snapshot = Path(create_snapshot()["snapshot"])
    _plant(snapshot, name)
    save_state({"topics": [{"id": "current"}], "mirrors": {}})

    with pytest.raises(SnapshotError) as refused:
        restore_snapshot(snapshot, apply=True)
    assert str(refused.value) in {
        t("backup.snapshot.unsafe_path", "ru", name=name),
        t("backup.snapshot.unexpected_file", "ru", name=name),
    }
    assert load_state()["topics"] == [{"id": "current"}]


def test_a_real_restore_point_in_a_copy_comes_back(backup):
    from tow.restore_points import restore_points_dir

    point = "restore-points/20261001T000000Z-deadbeef.towx"
    snapshot = Path(create_snapshot()["snapshot"])
    (snapshot / "restore-points").mkdir(exist_ok=True)
    (snapshot / point).write_bytes(b"point bytes")
    _plant(snapshot, point, b"point bytes")

    restore_snapshot(snapshot, apply=True)

    assert (restore_points_dir() / "20261001T000000Z-deadbeef.towx").read_bytes() == b"point bytes"


def test_an_edited_manifest_is_refused(backup):
    snapshot = Path(create_snapshot()["snapshot"])
    (snapshot / "state.json").write_text('{"topics": [{"id": "planted"}]}', encoding="utf-8")
    _plant(snapshot, "state.json", (snapshot / "state.json").read_bytes(), sign=False)  # hash fixed, not signed
    save_state({"topics": [{"id": "current"}], "mirrors": {}})

    with pytest.raises(SnapshotError, match=_says("backup.snapshot.bad_signature")):
        restore_snapshot(snapshot, apply=True)
    assert load_state()["topics"] == [{"id": "current"}]


@pytest.mark.parametrize("format_value", [[], {}, None, 2])
def test_malformed_manifest_format_is_a_snapshot_error(backup, format_value):
    snapshot = Path(create_snapshot()["snapshot"])
    (snapshot / "MANIFEST.json").write_text(json.dumps({"format": format_value, "files": {}}), encoding="utf-8")
    with pytest.raises(SnapshotError, match=_says("backup.snapshot.not_tow")):
        verify_snapshot(snapshot)


def test_foreign_or_damaged_copy_is_never_pruned(backup, monkeypatch):
    _clock(monkeypatch, ["20261001-000000", "20261001-000001"])
    original = Path(create_snapshot()["snapshot"])
    foreign = backup / "tow-20250901-000000"
    foreign.mkdir()
    (foreign / "MANIFEST.json").write_text("{}", encoding="utf-8")
    marker = foreign / "keep.txt"
    marker.write_text("not a TOW copy", encoding="utf-8")
    manifest_path = original / "MANIFEST.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["signature"] = "не подпись"
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")

    newest = Path(create_snapshot(keep=1)["snapshot"])

    assert newest.is_dir()
    assert original.is_dir()
    assert marker.read_text(encoding="utf-8") == "not a TOW copy"


def test_unsigned_legacy_copy_is_kept_by_automatic_pruning(backup, monkeypatch):
    _clock(monkeypatch, ["20261001-000000", "20261001-000001"])
    legacy = Path(create_snapshot()["snapshot"])
    manifest_path = legacy / "MANIFEST.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["format"] = "tow-snapshot-v1"
    manifest.pop("signature")
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    create_snapshot(keep=1)
    assert verify_snapshot(legacy)["signed"] is False


def test_a_malformed_copy_returns_a_clear_web_error_without_touching_data(backup):
    from fastapi.testclient import TestClient

    from tow.web import app

    snapshot = Path(create_snapshot()["snapshot"])
    (snapshot / "MANIFEST.json").write_text('{"format":[],"files":{}}', encoding="utf-8")
    response = TestClient(app, headers={"Origin": "http://127.0.0.1"}).post(
        f"/settings/backup/night/{snapshot.name}/restore", follow_redirects=False
    )
    assert response.status_code == 303
    assert t("backup.snapshot.not_tow", "ru") in flash_of(response.headers["location"])
    assert load_state()["topics"] == [{"id": "before"}]


def test_two_copies_in_the_same_second_and_an_old_partial_do_not_conflict(backup, monkeypatch):
    _clock(monkeypatch, ["20261001-000000"] * 3)
    first = Path(create_snapshot()["snapshot"])
    second = Path(create_snapshot()["snapshot"])
    partial = backup / ".tow-20261001-000000.partial"
    partial.mkdir()
    (partial / "keep.txt").write_text("interrupted copy", encoding="utf-8")
    third = Path(create_snapshot()["snapshot"])

    assert len({first, second, third}) == 3
    for folder in (first, second, third):
        assert verify_snapshot(snapshot_path(folder.name))["signed"] is True
    assert (partial / "keep.txt").read_text(encoding="utf-8") == "interrupted copy"


def test_a_failed_partial_reservation_never_removes_another_writers_files(backup, monkeypatch):
    _clock(monkeypatch, ["20261001-000000"])
    partial = backup / ".tow-20261001-000000.partial"
    real_mkdir = Path.mkdir

    def competing_writer(path, *args, **kwargs):
        real_mkdir(path, *args, **kwargs)
        if path == partial:
            (path / "keep.txt").write_text("another install's copy", encoding="utf-8")
            raise FileExistsError("reserved by another writer")

    monkeypatch.setattr(Path, "mkdir", competing_writer)
    with pytest.raises(SnapshotError):
        create_snapshot()
    assert (partial / "keep.txt").read_text(encoding="utf-8") == "another install's copy"


@pytest.mark.parametrize("size", [-1, True, "100", None, 1])
def test_a_signed_manifest_with_an_invalid_file_size_is_refused(backup, size):
    from tow.snapshots import _signature, _signing_key

    snapshot = Path(create_snapshot()["snapshot"])
    manifest_path = snapshot / "MANIFEST.json"
    manifest = json.loads(manifest_path.read_text(encoding="utf-8"))
    manifest["files"]["state.json"]["size"] = size
    manifest["signature"] = _signature(manifest, _signing_key())
    manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    with pytest.raises(SnapshotError, match=_says("backup.snapshot.file_damaged", name="state.json")):
        verify_snapshot(snapshot)


def test_snapshot_member_reads_are_bounded_by_the_verified_size(backup, monkeypatch):
    import io

    snapshot = Path(create_snapshot()["snapshot"])
    member = snapshot / "state.json"
    original = member.read_bytes()
    real_open = Path.open

    class GrowingFile:
        def __init__(self, handle):
            self.handle = handle
            self.content = io.BytesIO(original + b"changed after stat")

        def __enter__(self):
            return self

        def __exit__(self, *args):
            self.handle.close()

        def fileno(self):
            return self.handle.fileno()

        def read(self, size=-1):
            assert 0 < size <= len(original) + 1
            return self.content.read(size)

    def open_file(path, *args, **kwargs):
        if path == member and args == ("rb",):
            return GrowingFile(real_open(path, *args, **kwargs))
        return real_open(path, *args, **kwargs)

    monkeypatch.setattr(Path, "open", open_file)
    with pytest.raises(SnapshotError, match=_says("backup.snapshot.file_damaged", name="state.json")):
        verify_snapshot(snapshot)


def test_a_copy_signed_with_another_master_key_is_refused(backup, monkeypatch):
    snapshot = Path(create_snapshot()["snapshot"])
    monkeypatch.setenv("TOW_MASTER_KEY", Fernet.generate_key().decode("ascii"))

    with pytest.raises(SnapshotError, match=_says("backup.snapshot.bad_signature")):
        verify_snapshot(snapshot)


def test_an_unsigned_older_copy_can_be_checked_but_is_not_restored(backup):
    snapshot = Path(create_snapshot()["snapshot"])
    manifest = json.loads((snapshot / "MANIFEST.json").read_text(encoding="utf-8"))
    manifest["format"] = "tow-snapshot-v1"
    manifest.pop("signature")
    (snapshot / "MANIFEST.json").write_text(json.dumps(manifest), encoding="utf-8")
    save_state({"topics": [{"id": "current"}], "mirrors": {}})

    assert verify_snapshot(snapshot)["signed"] is False  # the files themselves are intact
    preview = restore_snapshot(snapshot)
    assert preview["signed"] is False
    with pytest.raises(SnapshotError, match=_says("backup.snapshot.unsigned")):
        restore_snapshot(snapshot, apply=True)
    assert load_state()["topics"] == [{"id": "current"}]


def test_no_master_key_no_copy(backup, monkeypatch):
    monkeypatch.delenv("TOW_MASTER_KEY")
    monkeypatch.delenv("TOW_MASTER_KEY_FILE", raising=False)

    with pytest.raises(SnapshotError, match=_says("backup.snapshot.no_key")):
        create_snapshot()
    assert not backup.exists() or not any(backup.iterdir())


# --- all or nothing ------------------------------------------------------------------------------


def _changed_after(snapshot_made: dict) -> Path:
    save_state({"topics": [{"id": "after"}], "mirrors": {}})
    save_secrets({"telegram": {"token": "t-after"}})
    return Path(snapshot_made["snapshot"])


def _fail_on(monkeypatch, live: Path, error: BaseException, *, times: int = 1) -> dict:
    """Writing the live ``live`` file fails ``times`` times (the before-restore copy is written).

    Returns the switch: ``fault["on"] = False`` lets every later write through.
    """
    import tow.snapshots

    real = tow.snapshots.atomic_write_bytes
    fault = {"on": True, "left": times}

    def write(path, content):
        if fault["on"] and fault["left"] and Path(path) == live:
            fault["left"] -= 1
            raise error
        real(path, content)

    monkeypatch.setattr(tow.snapshots, "atomic_write_bytes", write)
    return fault


def test_an_error_half_way_puts_every_file_back(backup, monkeypatch):
    from tow.store import encrypted_secrets_path

    snapshot = _changed_after(create_snapshot())
    config_before = load_config()
    _fail_on(monkeypatch, encrypted_secrets_path(), PermissionError(13, "Access is denied"))

    with pytest.raises(SnapshotError, match=_says("backup.snapshot.restore_failed", reason="Access is denied")):
        restore_snapshot(snapshot, apply=True)

    assert load_state()["topics"] == [{"id": "after"}]  # state.json had been written: put back
    assert load_secrets()["telegram"]["token"] == "t-after"
    assert load_config() == config_before
    assert not (data_dir() / ".tow-night-restore.json").exists()
    journal = json.loads(next(data_dir().glob("before-restore-*/RESTORE.json")).read_text(encoding="utf-8"))
    assert journal["status"] == "rolled_back"


def test_a_crash_half_way_is_undone_before_the_next_write(backup, monkeypatch):
    from tow.store import encrypted_secrets_path, persistence_lock

    snapshot = _changed_after(create_snapshot())

    class Crash(BaseException):  # the process dies: no except clause runs
        pass

    fault = _fail_on(monkeypatch, encrypted_secrets_path(), Crash())
    with pytest.raises(Crash):
        restore_snapshot(snapshot, apply=True)
    fault["on"] = False  # the next process writes normally
    assert (data_dir() / ".tow-night-restore.json").exists()

    with persistence_lock():  # any TOW process taking the lock recovers first
        pass

    assert load_state()["topics"] == [{"id": "after"}]
    assert load_secrets()["telegram"]["token"] == "t-after"
    assert not (data_dir() / ".tow-night-restore.json").exists()


def test_store_imports_the_recovery_of_a_process_that_never_loaded_it():
    import tow.snapshots
    import tow.store

    assert ("tow.snapshots", "recover_interrupted_restore", tow.snapshots._MARKER) in tow.store.RECOVERY_STEPS


def test_a_failed_put_back_is_said_and_retried(backup, monkeypatch):
    from tow.store import encrypted_secrets_path, persistence_lock

    snapshot = _changed_after(create_snapshot())
    _fail_on(monkeypatch, encrypted_secrets_path(), OSError(5, "I/O error"), times=2)  # the put-back fails too

    with pytest.raises(SnapshotError) as failed:
        restore_snapshot(snapshot, apply=True)
    safety = next(data_dir().glob("before-restore-*"))
    assert str(failed.value) == t("backup.snapshot.rollback_failed", "ru", path=str(safety))
    assert (data_dir() / ".tow-night-restore.json").exists()

    with persistence_lock():  # the next writer tries again, and now it works
        pass
    assert load_state()["topics"] == [{"id": "after"}]
    assert load_secrets()["telegram"]["token"] == "t-after"
    assert not (data_dir() / ".tow-night-restore.json").exists()


def _crashed_restore(backup, monkeypatch) -> Path:
    """A restore the process died in half-way; returns its before-restore folder."""
    from tow.store import encrypted_secrets_path

    snapshot = _changed_after(create_snapshot())

    class Crash(BaseException):
        pass

    fault = _fail_on(monkeypatch, encrypted_secrets_path(), Crash())
    with pytest.raises(Crash):
        restore_snapshot(snapshot, apply=True)
    fault["on"] = False
    return next(data_dir().glob("before-restore-*"))


def _journal_held(monkeypatch, *, times: int | None) -> dict:
    """Reading RESTORE.json fails like a file an antivirus holds: ``times`` times (None: until
    ``held["on"] = False``). Returns the switch, with the number of reads in ``held["calls"]``."""
    import tow.snapshots

    real = tow.snapshots._read_journal
    held = {"on": True, "calls": 0}

    def read(safety):
        held["calls"] += 1
        if held["on"] and (times is None or held["calls"] <= times):
            raise PermissionError(13, "The process cannot access the file")
        return real(safety)

    monkeypatch.setattr(tow.snapshots, "_read_journal", read)
    monkeypatch.setattr(tow.snapshots.time, "sleep", lambda _s: None)
    return held


def test_a_journal_held_for_a_moment_is_read_again_and_the_restore_undone(backup, monkeypatch):
    from tow.store import persistence_lock

    _crashed_restore(backup, monkeypatch)
    held = _journal_held(monkeypatch, times=2)

    with persistence_lock():
        pass

    assert held["calls"] >= 3
    assert load_state()["topics"] == [{"id": "after"}]
    assert not (data_dir() / ".tow-night-restore.json").exists()


def test_a_journal_that_stays_held_refuses_every_write_and_keeps_the_marker(backup, monkeypatch):
    from tow.store import persistence_lock, save_state

    _crashed_restore(backup, monkeypatch)
    held = _journal_held(monkeypatch, times=None)
    marker = data_dir() / ".tow-night-restore.json"

    with pytest.raises(SnapshotError) as refused:
        save_state({"topics": [{"id": "written over a half restore"}], "mirrors": {}})
    assert str(refused.value) == t(
        "backup.snapshot.recovery_blocked", "ru", reason="The process cannot access the file", marker=str(marker)
    )
    assert marker.exists()  # never given up on

    held["on"] = False  # the antivirus lets go: the next writer undoes the restore
    with persistence_lock():
        pass
    assert load_state()["topics"] == [{"id": "after"}]
    assert not marker.exists()


def test_a_damaged_journal_is_not_dropped_either(backup, monkeypatch):
    from tow.store import persistence_lock

    safety = _crashed_restore(backup, monkeypatch)
    (safety / "RESTORE.json").write_text("{not json", encoding="utf-8")

    with pytest.raises(SnapshotError) as refused, persistence_lock():
        pass
    assert str(refused.value).startswith(t("backup.snapshot.recovery_blocked", "ru").split("(")[0])
    assert (data_dir() / ".tow-night-restore.json").exists()


def test_old_before_restore_copies_are_pruned_and_keep_no_settings_undo(backup, monkeypatch):
    from tow.snapshots import SAFETY_KEEP
    from tow.store import save_secret_undo

    snapshot = Path(create_snapshot()["snapshot"])
    stamps = [f"20261001-0000{n:02d}" for n in range(SAFETY_KEEP + 3)]
    unfinished = data_dir() / "before-restore-20260901-000000"  # a restore that is not finished
    unfinished.mkdir()
    (unfinished / "RESTORE.json").write_text(
        json.dumps({"format": "tow-night-restore/v1", "status": "prepared", "entries": []}), encoding="utf-8"
    )
    (unfinished / "secrets-undo.enc").write_text("kept while it may be needed", encoding="utf-8")
    _clock(monkeypatch, stamps)
    for _ in stamps:
        save_secret_undo({"telegram": {"token": "undo"}})
        restore_snapshot(snapshot, apply=True)

    kept = sorted(p.name for p in data_dir().glob("before-restore-*"))
    assert kept == [unfinished.name, *(f"before-restore-{stamp}" for stamp in stamps[-SAFETY_KEEP:])]
    assert (unfinished / "secrets-undo.enc").exists()
    assert not any((data_dir() / name / "secrets-undo.enc").exists() for name in kept[1:])
    assert all((data_dir() / name / "state.json").exists() for name in kept[1:])  # the rest stays


def test_a_broken_live_config_does_not_stop_the_restore_and_stays_local(backup):
    from tow.paths import config_path

    snapshot = Path(create_snapshot()["snapshot"])
    cfg = load_config()
    cfg["allow_lan"] = True
    cfg["bind"] = "0.0.0.0"
    save_config(cfg)
    config_path().write_text("trackers: [unclosed\n", encoding="utf-8")

    restore_snapshot(snapshot, apply=True)

    restored = load_config()
    assert restored["allow_lan"] is False
    assert restored["bind"] == "127.0.0.1"
    assert load_state()["topics"] == [{"id": "before"}]


def test_a_store_the_copy_did_not_have_is_removed_and_kept_aside(backup):
    from tow.store import save_secret_undo, secret_undo_path

    snapshot = Path(create_snapshot()["snapshot"])
    assert "secrets-undo.enc" not in verify_snapshot(snapshot)["files"]
    save_secret_undo({"telegram": {"token": "undo"}})

    result = restore_snapshot(snapshot, apply=True)

    assert not secret_undo_path().exists()
    # Kept aside while the restore could still be rolled back; once it is done, that undo can
    # never be applied again, so its secrets are not kept.
    assert not (Path(result["safety_copy"]) / "secrets-undo.enc").exists()
    assert (Path(result["safety_copy"]) / "secrets.enc").is_file()


# --- the current password stays ------------------------------------------------------------------


def test_a_restore_keeps_the_password_the_owner_knows_now(backup):
    from tow.auth import lan_password_matches, lan_password_record

    save_secrets({"telegram": {"token": "t-before"}, "lan_auth": lan_password_record("last-nights-pass")})
    snapshot = Path(create_snapshot()["snapshot"])
    save_secrets({"telegram": {"token": "t-after"}, "lan_auth": lan_password_record("todays-password")})

    restore_snapshot(snapshot, apply=True)

    secrets = load_secrets()
    assert secrets["telegram"]["token"] == "t-before"  # the copy's secrets ...
    assert lan_password_matches("todays-password", secrets["lan_auth"])  # ... with today's password
    assert not lan_password_matches("last-nights-pass", secrets["lan_auth"])


def test_no_password_now_means_none_after_the_restore(backup):
    from tow.auth import lan_password_record

    save_secrets({"telegram": {"token": "t-before"}, "lan_auth": lan_password_record("last-nights-pass")})
    snapshot = Path(create_snapshot()["snapshot"])
    save_secrets({"telegram": {"token": "t-after"}})

    restore_snapshot(snapshot, apply=True)

    assert "lan_auth" not in load_secrets()
    assert load_secrets()["telegram"]["token"] == "t-before"


def test_a_copy_without_secrets_keeps_only_the_current_password(backup):
    from tow.auth import lan_password_matches, lan_password_record
    from tow.store import encrypted_secrets_path

    encrypted_secrets_path().unlink()
    snapshot = Path(create_snapshot()["snapshot"])
    assert "secrets.enc" not in verify_snapshot(snapshot)["files"]
    save_secrets({"telegram": {"token": "t-after"}, "lan_auth": lan_password_record("todays-password")})

    result = restore_snapshot(snapshot, apply=True)

    secrets = load_secrets()
    assert set(secrets) == {"lan_auth"}
    assert lan_password_matches("todays-password", secrets["lan_auth"])
    assert (Path(result["safety_copy"]) / "secrets.enc").is_file()  # the tokens are kept aside


def test_cli_backup_and_restore_round_trip(backup, capsys):
    from tow import cli

    assert cli.main(["backup", "--json"]) == 0
    snapshot = json.loads(capsys.readouterr().out)["snapshot"]
    save_state({"topics": [{"id": "after"}], "mirrors": {}})

    assert cli.main(["restore-snapshot", "--path", snapshot, "--apply", "--json"]) == 0
    assert load_state()["topics"] == [{"id": "before"}]
    assert yaml.safe_load(Path(snapshot, "config.yaml").read_text(encoding="utf-8"))["backup_keep"] == 3


def test_a_relative_backup_dir_lives_inside_the_install_and_moves_with_it(tmp_path_factory, monkeypatch):
    # Portability: backup/night next to config.yaml; copying the install keeps the copies.
    from tow.paths import config_path

    monkeypatch.setenv("TOW_MASTER_KEY", Fernet.generate_key().decode("ascii"))
    install = tmp_path_factory.mktemp("install")
    (install / "config.yaml").write_text(
        config_path().read_text(encoding="utf-8") + "\nbackup_dir: backup/night\n", encoding="utf-8"
    )
    monkeypatch.setenv("TOW_CONFIG", str(install / "config.yaml"))
    save_state({"topics": [], "mirrors": {}})

    made = Path(create_snapshot()["snapshot"])

    assert made.parent == (install / "backup" / "night").resolve()
