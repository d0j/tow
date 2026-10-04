"""A completed copy/restore is distinct from best-effort cleanup of older copies."""

from __future__ import annotations

import hashlib
import stat
from pathlib import Path
from types import SimpleNamespace

import pytest
import test_snapshots as fixtures

from tow import snapshots
from tow.config import load_config, save_config
from tow.i18n import t
from tow.log import history_events, read_events
from tow.paths import data_dir

backup = fixtures.backup


def _block_deletion(monkeypatch, folder: Path, mode: str):
    real_unlink, real_rmdir = Path.unlink, Path.rmdir
    enabled = {"value": True}

    def unlink(path, *args, **kwargs):
        if enabled["value"] and path == folder / "state.json" and mode != "rmdir":
            if mode == "noop":
                return None
            raise PermissionError("synthetic held file")
        return real_unlink(path, *args, **kwargs)

    def rmdir(path, *args, **kwargs):
        if enabled["value"] and path == folder and mode == "rmdir":
            raise PermissionError("synthetic held directory")
        return real_rmdir(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", unlink)
    monkeypatch.setattr(Path, "rmdir", rmdir)
    return enabled


@pytest.mark.parametrize("mode", ["held", "noop", "rmdir"])
def test_night_cleanup_failure_preserves_verified_new_copy_and_reports_only_actual_removals(backup, monkeypatch, mode):
    fixtures._clock(monkeypatch, ["20261001-000000", "20261001-000001", "20261001-000002"])
    old = Path(snapshots.create_snapshot()["snapshot"])
    proof = (old / "MANIFEST.json").read_bytes()
    fault = _block_deletion(monkeypatch, old, mode)
    result = snapshots.create_snapshot(keep=1)
    assert result["ok"] is True
    assert snapshots.verify_snapshot(Path(result["snapshot"]))["signed"] is True
    assert result["pruned"] == []
    assert result["cleanup_warning"]
    assert snapshots.status()["last_cleanup_pending"] is True
    assert snapshots.status()["last_error"] == ""
    assert (old / "MANIFEST.json").read_bytes() == proof
    events = read_events()
    assert next(row for row in events if row["kind"] == "backup_created")["pruned"] == 0
    assert any(row["kind"] == "backup_cleanup_pending" for row in history_events(group="errors"))
    fault["value"] = False
    retry = snapshots.create_snapshot(keep=1)
    assert old.name in retry["pruned"]
    assert not old.exists()
    assert not retry.get("cleanup_warning")
    assert snapshots.status()["last_cleanup_pending"] is False


def _safety_copies() -> list[Path]:
    folders = []
    content = b"synthetic saved state"
    for index in range(snapshots.SAFETY_KEEP + 1):
        folder = data_dir() / f"before-restore-20261001-00000{index}"
        folder.mkdir()
        (folder / "state.json").write_bytes(content)
        snapshots._write_journal(
            folder,
            {
                "format": snapshots._JOURNAL_FORMAT,
                "entries": [{"name": "state.json", "existed": True, "sha256": hashlib.sha256(content).hexdigest()}],
            },
            "committed",
        )
        folders.append(folder)
    return folders


@pytest.mark.parametrize("mode", ["held", "noop", "rmdir"])
def test_safety_cleanup_failure_retains_ownership_proof_for_retry(monkeypatch, mode):
    folders = _safety_copies()
    old = folders[0]
    proof = (old / snapshots._JOURNAL).read_bytes()
    fault = _block_deletion(monkeypatch, old, mode)
    assert snapshots._tidy_safety_copies() == []
    assert (old / snapshots._JOURNAL).read_bytes() == proof
    assert any(row["kind"] == "backup_cleanup_pending" for row in history_events(group="errors"))
    fault["value"] = False
    assert snapshots._tidy_safety_copies() == [old.name]
    assert not old.exists()
    assert all(folder.exists() for folder in folders[1:])


@pytest.mark.parametrize("lang", ["ru", "en"])
@pytest.mark.parametrize("operation", ["copy", "restore"])
def test_actual_http_cleanup_warning_does_not_claim_the_copy_or_restore_failed(backup, monkeypatch, lang, operation):
    from bs4 import BeautifulSoup
    from fastapi.testclient import TestClient
    from helpers import flash_of

    from tow.store import load_state, save_state
    from tow.web import app

    cfg = load_config()
    cfg["language"] = lang
    save_config(cfg)
    fixtures._clock(monkeypatch, ["20261001-000000", "20261001-000001"])
    point = Path(snapshots.create_snapshot()["snapshot"])
    cfg["backup_keep"] = 1
    save_config(cfg)
    if operation == "copy":
        old = point
        route = "/settings/backup/now"
        key = "backup.snapshot.cleanup_warning"
    else:
        old = _safety_copies()[0]
        route = f"/settings/backup/night/{point.name}/restore"
        key = "backup.snapshot.restore_cleanup_warning"
        save_state({"topics": [{"id": "after"}]})
    _block_deletion(monkeypatch, old, "held")
    client = TestClient(app, base_url="http://127.0.0.1", headers={"Origin": "http://127.0.0.1"})
    response = client.post(route, follow_redirects=False)
    assert response.status_code == 303
    assert t(key, lang) in flash_of(response.headers["location"])
    page = client.get(response.headers["location"])
    soup = BeautifulSoup(page.text, "html.parser")
    assert t(key, lang) in soup.select_one(".flash.warn").get_text()
    assert load_state()["topics"] == [{"id": "before"}]
    if operation == "copy":
        assert t(key, lang) in soup.select_one("#backup-night [role=status]").get_text()
        assert (
            t("settings.backups.pill_cleanup", lang) in soup.select_one("#acc-transfer > summary .pill.warn").get_text()
        )


@pytest.mark.parametrize("extra", ["keep.txt", "unexpected-folder/keep.txt"])
def test_signed_night_copy_with_unowned_files_is_not_pruned(backup, monkeypatch, extra):
    fixtures._clock(monkeypatch, ["20261001-000000", "20261001-000001"])
    old = Path(snapshots.create_snapshot()["snapshot"])
    foreign = old / extra
    foreign.parent.mkdir(exist_ok=True)
    foreign.write_bytes(b"not owned by TOW")
    result = snapshots.create_snapshot(keep=1)
    assert result["pruned"] == []
    assert foreign.read_bytes() == b"not owned by TOW"


@pytest.mark.parametrize("mode", ["held", "noop"])
def test_retained_safety_undo_failure_is_reported_without_pruning_the_kept_copy(monkeypatch, mode):
    folders = _safety_copies()
    newest = folders[-1]
    undo = newest / snapshots._UNDO_IN_SAFETY
    undo.write_bytes(b"synthetic encrypted undo")
    journal = snapshots._read_journal(newest)
    journal["entries"].append(
        {"name": undo.name, "existed": True, "sha256": hashlib.sha256(undo.read_bytes()).hexdigest()}
    )
    snapshots._write_journal(newest, journal, "committed")
    real_unlink = Path.unlink

    def blocked(path, *args, **kwargs):
        if path == undo:
            if mode == "noop":
                return None
            raise PermissionError("synthetic held undo")
        return real_unlink(path, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", blocked)
    removed, pending = snapshots._cleanup_safety_copies()
    assert removed == [folders[0].name]
    assert pending is True
    assert undo.read_bytes() == b"synthetic encrypted undo"


@pytest.mark.parametrize("marker", ["MANIFEST.json", "RESTORE.json"])
@pytest.mark.parametrize(
    "mode",
    [
        "ok",
        "recursive_partial",
        "recursive_noop",
        "directory_noop",
        "unknown_absence",
        "foreign_marker",
        "reparse",
        "wrong_parent",
    ],
)
def test_removal_boundary_confirms_absence_and_never_replaces_foreign_proof(tmp_path, monkeypatch, marker, mode):
    folder = tmp_path / "copy"
    folder.mkdir()
    proof = folder / marker
    proof.write_bytes(b"original ownership proof")
    (folder / "state.json").write_bytes(b"saved state")
    points = folder / "restore-points"
    points.mkdir()
    (points / "fixture.towx").write_bytes(b"saved point")
    expected = {marker, "state.json", "restore-points/fixture.towx"}
    real_rmdir, real_lstat = Path.rmdir, Path.lstat
    removed = {"value": False}

    def rmdir(path, *args, **kwargs):
        if path == folder and mode == "directory_noop":
            return None
        if path == folder and mode == "foreign_marker":
            proof.write_bytes(b"concurrent writer proof")
            raise PermissionError("synthetic held directory")
        result = real_rmdir(path, *args, **kwargs)
        if path == folder:
            removed["value"] = True
        return result

    def lstat(path, *args, **kwargs):
        if path == folder and mode == "reparse":
            return SimpleNamespace(st_mode=stat.S_IFDIR, st_file_attributes=stat.FILE_ATTRIBUTE_REPARSE_POINT)
        if path == folder and mode == "unknown_absence" and removed["value"]:
            raise PermissionError("synthetic unknown folder state")
        return real_lstat(path, *args, **kwargs)

    monkeypatch.setattr(Path, "rmdir", rmdir)
    monkeypatch.setattr(Path, "lstat", lstat)
    if mode.startswith("recursive_"):

        def blocked_rmtree(path, *args, **kwargs):
            assert path == points
            if mode == "recursive_partial":
                (points / "fixture.towx").unlink()
                raise PermissionError("synthetic partial tree removal")

        monkeypatch.setattr(snapshots.shutil, "rmtree", blocked_rmtree)
    parent = tmp_path / "other" if mode == "wrong_parent" else tmp_path
    assert snapshots._remove_copy(folder, parent, marker, expected) is (mode == "ok")
    if mode in {"ok", "unknown_absence"}:
        assert not folder.exists()
    else:
        assert proof.read_bytes() == (
            b"concurrent writer proof" if mode == "foreign_marker" else b"original ownership proof"
        )


@pytest.mark.parametrize("marker", ["../outside", "/absolute", "missing-marker"])
def test_removal_boundary_rejects_an_unexpected_marker_before_any_change(tmp_path, marker):
    folder = tmp_path / "copy"
    folder.mkdir()
    member = folder / "keep.txt"
    member.write_bytes(b"keep")
    assert snapshots._remove_copy(folder, tmp_path, marker, {"MANIFEST.json", "keep.txt"}) is False
    assert member.read_bytes() == b"keep"


def test_cleanup_listing_error_does_not_abort_the_new_verified_night_copy(backup, monkeypatch):
    fixtures._clock(monkeypatch, ["20261001-000000", "20261001-000001"])
    old = Path(snapshots.create_snapshot()["snapshot"])
    real_scan = snapshots._snapshots
    calls = []

    def scan(root):
        calls.append(root)
        if len(calls) > 1:
            raise PermissionError("synthetic unavailable listing")
        return real_scan(root)

    monkeypatch.setattr(snapshots, "_snapshots", scan)
    result = snapshots.create_snapshot(keep=1)
    assert result["ok"] is True
    assert result["pruned"] == []
    assert result["cleanup_warning"]
    assert snapshots.verify_snapshot(Path(result["snapshot"]))["signed"] is True
    assert old.is_dir()


@pytest.mark.parametrize("kind", ["night", "safety"])
@pytest.mark.parametrize("lang", ["ru", "en"])
def test_cleanup_history_detail_identifies_the_kind_of_copies_without_private_paths(kind, lang):
    from tow.log import format_event

    cfg = load_config()
    cfg["language"] = lang
    save_config(cfg)
    row = format_event({"kind": "backup_cleanup_pending", "copy_kind": kind, "how": "auto"})
    assert row["label"] == t("log.kind.backup_cleanup_pending", lang).capitalize()
    assert t(f"log.cleanup.{kind}", lang) in row["detail"]


def test_cli_backup_reports_verified_copy_and_cleanup_warning_without_a_failure_exit(backup, monkeypatch, capsys):
    import json

    from tow import cli

    fixtures._clock(monkeypatch, ["20261001-000000", "20261001-000001"])
    old = Path(snapshots.create_snapshot()["snapshot"])
    cfg = load_config()
    cfg["backup_keep"] = 1
    save_config(cfg)
    _block_deletion(monkeypatch, old, "held")
    assert cli.main(["backup", "--json"]) == 0
    result = json.loads(capsys.readouterr().out)
    assert result["ok"] is True
    assert result["cleanup_warning"]
    assert result["pruned"] == []


@pytest.mark.parametrize("lang", ["ru", "en"])
def test_watchdog_reports_cleanup_and_recovery_once_without_calling_a_verified_copy_failed(backup, monkeypatch, lang):
    from tow.store import save_state
    from tow.watchdog import run_watchdog

    fixtures._clock(monkeypatch, ["20261001-000000", "20261001-000001", "20261001-000002"])
    cfg = load_config()
    cfg.update(language=lang, heartbeat_url="https://hc-ping.com/test-cleanup")
    save_config(cfg)
    old = Path(snapshots.create_snapshot()["snapshot"])
    fault = _block_deletion(monkeypatch, old, "held")
    snapshots.create_snapshot(keep=1)
    now = snapshots.status()["last_ok_at"]
    save_state({"topics": [], "health": {"auto_at_ts": now}})
    sent, pings = [], []

    def check():
        result = run_watchdog(
            is_healthy=lambda _port: True,
            deploy_running=lambda: False,
            send=lambda text: sent.append(text) or True,
            now=lambda: now,
            sleep=lambda _seconds: None,
            pulse=lambda _url, ok: pings.append(ok) or True,
            flush=lambda: None,
        )
        assert result["backup_ok"] is True
        assert result["service_ok"] is True
        assert result["checks_ok"] is True
        return result

    assert check()["backup_cleanup_pending"] is True
    assert sent == [t("watchdog.alert.backup_cleanup_pending", lang)]
    check()
    assert len(sent) == 1
    fault["value"] = False
    snapshots.create_snapshot(keep=1)
    assert check()["backup_cleanup_pending"] is False
    assert sent == [t("watchdog.alert.backup_cleanup_pending", lang), t("watchdog.alert.backup_cleanup_ok", lang)]
    check()
    assert len(sent) == 2
    assert pings == [True] * 4
