"""Matching backup controls, scoped confirmed deletion and non-destructive checks."""

from __future__ import annotations

import json
import shutil
from pathlib import Path

import pytest
from bs4 import BeautifulSoup
from fastapi.testclient import TestClient
from helpers import flash_of

from tow import restore_points, snapshots
from tow.config import load_config, save_config
from tow.log import read_events
from tow.paths import config_path, data_dir
from tow.store import save_secrets, save_state
from tow.web import app


@pytest.fixture
def copies(tmp_path, monkeypatch):
    monkeypatch.setenv("TOW_HOME", str(tmp_path / "data"))
    cfg = load_config()
    cfg.update(backup_dir=str(tmp_path / "night"), backup_keep=30)
    save_config(cfg)
    save_state({"topics": [{"id": "synthetic-watch"}]})
    save_secrets({"telegram": {"token": "synthetic-token"}})
    point = restore_points.create_restore_point()
    night = Path(snapshots.create_snapshot()["snapshot"])
    return point["id"], night.name


def _client(**kwargs):
    return TestClient(app, headers={"Origin": "http://127.0.0.1"}, **kwargs)


def _url(copies, category):
    point, night = copies
    return f"/settings/backup/night/{night}" if category == "night" else f"/settings/restore-points/{point}"


def _path(copies, category):
    point, night = copies
    return snapshots.snapshot_path(night) if category == "night" else restore_points.point_path(point)


def _live_bytes():
    return {path: path.read_bytes() for path in (config_path(), data_dir() / "state.json", data_dir() / "secrets.enc")}


def _confirm(client, url):
    response = client.get(url + "/delete")
    assert response.status_code == 200
    page = BeautifulSoup(response.text, "html.parser")
    assert page.select_one(".backup-delete-confirm form")["action"] == url + "/delete"
    assert page.select_one('.backup-delete-confirm a[href="/settings?open=transfer#acc-transfer"]')
    return page.select_one('input[name="revision"]')["value"]


@pytest.mark.parametrize("category", ["point", "night"])
@pytest.mark.parametrize("language", ["ru", "en"])
def test_matching_actions_are_compact_and_closed(copies, category, language):
    cfg = load_config()
    cfg["language"] = language
    save_config(cfg)
    page = BeautifulSoup(_client().get("/settings?open=transfer").text, "html.parser")
    card = page.select_one("#backup-night" if category == "night" else "#backup-manual")
    assert not card.select_one(".backup-inventory").has_attr("open")
    row = card.select_one(".restore-point-row")
    url = _url(copies, category)
    assert row.select_one(f'form[action="{url}/check"]')
    assert row.select_one(f'form[action="{url}/restore"]')
    assert row.select_one(f'a[href="{url}/delete"]')
    assert card.select_one(".backup-inventory > summary").get_text(strip=True)
    assert page.select_one("#backup-days")["value"] == "7"
    assert page.select_one('select[name="days"]') is None


@pytest.mark.parametrize("category", ["point", "night"])
def test_confirmation_and_cancel_never_delete(copies, category):
    client = _client()
    before = _live_bytes()
    path = _path(copies, category)
    revision = _confirm(client, _url(copies, category))
    assert len(revision) == 64
    assert client.get("/settings?open=transfer#acc-transfer").status_code == 200
    assert path.exists()
    assert _live_bytes() == before


@pytest.mark.parametrize("category", ["point", "night"])
def test_delete_removes_only_selected_copy_and_rejects_repeated_post(copies, category):
    client = _client()
    url = _url(copies, category)
    path = _path(copies, category)
    other = (
        Path(snapshots.create_snapshot()["snapshot"])
        if category == "night"
        else restore_points.point_path(restore_points.create_restore_point()["id"])
    )
    other_before = (
        {p: p.read_bytes() for p in other.rglob("*") if p.is_file()} if other.is_dir() else {other: other.read_bytes()}
    )
    before = _live_bytes()
    revision = _confirm(client, url)
    response = client.post(url + "/delete", data={"revision": revision}, follow_redirects=False)
    assert response.status_code == 303
    assert not path.exists()
    assert _live_bytes() == before
    assert all(p.read_bytes() == content for p, content in other_before.items())
    events = [record for record in read_events(limit=100) if record["kind"] == "settings_backup_deleted"]
    assert len(events) == 1
    again = client.post(url + "/delete", data={"revision": revision}, follow_redirects=False)
    assert again.status_code == 303
    assert len([r for r in read_events(limit=100) if r["kind"] == "settings_backup_deleted"]) == 1


@pytest.mark.parametrize("category", ["point", "night"])
@pytest.mark.parametrize("revision", ["", "wrong", "0" * 64])
def test_delete_requires_matching_confirmation(copies, category, revision):
    before = _live_bytes()
    response = _client().post(_url(copies, category) + "/delete", data={"revision": revision}, follow_redirects=False)
    assert response.status_code == 303
    assert _path(copies, category).exists()
    assert _live_bytes() == before


@pytest.mark.parametrize("category", ["point", "night"])
def test_changed_copy_requires_new_confirmation(copies, category):
    client = _client()
    url = _url(copies, category)
    revision = _confirm(client, url)
    path = _path(copies, category)
    member = path / "state.json" if category == "night" else path
    member.write_bytes(member.read_bytes() + b"changed-after-confirmation")
    before = member.read_bytes()
    response = client.post(url + "/delete", data={"revision": revision}, follow_redirects=False)
    assert "после открытия" in flash_of(response.headers["location"])
    assert member.read_bytes() == before


@pytest.mark.parametrize("category", ["point", "night"])
def test_changed_folder_with_same_named_copy_is_not_deleted(copies, category, tmp_path):
    client = _client()
    url = _url(copies, category)
    original = _path(copies, category)
    revision = _confirm(client, url)
    folder = tmp_path / "another-folder"
    folder.mkdir()
    replacement = folder / original.name
    if category == "night":
        shutil.copytree(original, replacement)
    else:
        shutil.copyfile(original, replacement)
    cfg = load_config()
    cfg["backup_dir" if category == "night" else "restore_points_dir"] = str(folder)
    save_config(cfg)
    response = client.post(url + "/delete", data={"revision": revision}, follow_redirects=False)
    assert "после открытия" in flash_of(response.headers["location"])
    assert original.exists()
    assert replacement.exists()


@pytest.mark.parametrize("category", ["point", "night"])
def test_check_does_not_restore_or_create_safety_copy(copies, category):
    before = _live_bytes()
    points = restore_points.list_restore_points()
    nights = snapshots.list_snapshots(limit=None)
    response = _client().post(_url(copies, category) + "/check", follow_redirects=False)
    assert response.status_code == 303
    assert "прошла проверку" in flash_of(response.headers["location"])
    assert _live_bytes() == before
    assert restore_points.list_restore_points() == points
    assert snapshots.list_snapshots(limit=None) == nights


@pytest.mark.parametrize("category", ["point", "night"])
def test_damaged_copy_check_reports_failure_without_live_changes(copies, category):
    path = _path(copies, category)
    member = path / "state.json" if category == "night" else path
    member.write_bytes(b"damaged-synthetic-copy")
    before = _live_bytes()
    response = _client().post(_url(copies, category) + "/check", follow_redirects=False)
    assert "Не удалось проверить" in flash_of(response.headers["location"])
    assert _live_bytes() == before


@pytest.mark.parametrize("content", [b"", b"damaged archive"])
def test_explicit_manual_delete_can_remove_damaged_regular_archive(copies, content):
    path = _path(copies, "point")
    path.write_bytes(content)
    client = _client()
    url = _url(copies, "point")
    assert copies[0] in {row["id"] for row in restore_points.list_restore_points()}
    revision = _confirm(client, url)
    response = client.post(url + "/delete", data={"revision": revision}, follow_redirects=False)
    assert response.status_code == 303
    assert not path.exists()


@pytest.mark.parametrize("category", ["point", "night"])
@pytest.mark.parametrize("failure", ["held", "noop"])
def test_unconfirmed_deletion_does_not_report_success(copies, category, failure, monkeypatch):
    client = _client()
    url = _url(copies, category)
    revision = _confirm(client, url)
    path = _path(copies, category)
    blocked = path / "config.yaml" if category == "night" else path
    real = Path.unlink

    def cannot_remove(target, *args, **kwargs):
        if target == blocked:
            if failure == "held":
                raise PermissionError("synthetic held copy")
            return None
        return real(target, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", cannot_remove)
    response = client.post(url + "/delete", data={"revision": revision}, follow_redirects=False)
    assert "не подтверждено" in flash_of(response.headers["location"])
    assert path.exists()
    assert not [r for r in read_events(limit=100) if r["kind"] == "settings_backup_deleted"]
    if category == "night":
        assert snapshots.cleanup_status()["pending"] is True


@pytest.mark.parametrize("category", ["point", "night"])
def test_delete_preserves_previous_cleanup_warning(copies, category):
    path = _path(copies, category)
    if category == "night":
        snapshots._record(
            last_cleanup_pending=True,
            location=str(path.parent.resolve()),
            cleanup_inventory=snapshots._cleanup_inventory(path.parent),
        )
    else:
        restore_points._record_cleanup(True, path.parent)
    client = _client()
    url = _url(copies, category)
    revision = _confirm(client, url)
    client.post(url + "/delete", data={"revision": revision}, follow_redirects=False)
    observation = snapshots.cleanup_status() if category == "night" else restore_points.cleanup_status()
    assert observation["pending"] is True
    assert not observation["read_error"]


@pytest.mark.parametrize("foreign", ["file", "folder", "unsigned", "bad-signature"])
def test_night_delete_refuses_foreign_or_unsigned_tree(copies, foreign):
    path = _path(copies, "night")
    if foreign in {"file", "folder"}:
        extra = path / "foreign.txt" if foreign == "file" else path / "foreign" / "keep.txt"
        extra.parent.mkdir(exist_ok=True)
        extra.write_text("foreign content", encoding="utf-8")
    else:
        manifest_path = path / "MANIFEST.json"
        manifest = json.loads(manifest_path.read_bytes())
        if foreign == "unsigned":
            manifest["format"] = snapshots.UNSIGNED_FORMAT
            manifest.pop("signature")
        else:
            manifest["signature"] = "0" * 64
        manifest_path.write_text(json.dumps(manifest), encoding="utf-8")
    response = _client().get(_url(copies, "night") + "/delete", follow_redirects=False)
    assert response.status_code == 303
    assert path.exists()


@pytest.mark.parametrize("category", ["point", "night"])
@pytest.mark.parametrize("operation", ["check", "delete"])
@pytest.mark.parametrize("origin", [None, "null", "https://foreign.invalid"])
def test_backup_actions_require_matching_origin(copies, category, operation, origin):
    path = _path(copies, category)
    response = TestClient(app).post(
        _url(copies, category) + "/" + operation,
        headers={} if origin is None else {"Origin": origin},
        follow_redirects=False,
    )
    assert response.status_code == 403
    assert path.exists()


def test_all_night_copies_are_shown_not_only_seven(copies):
    for _ in range(9):
        snapshots.create_snapshot()
    page = BeautifulSoup(_client().get("/settings").text, "html.parser")
    assert len(page.select("#backup-night .restore-point-row")) == 10


def test_empty_manual_archive_is_deletable_but_not_restorable_and_does_not_break_retention(copies):
    path = _path(copies, "point")
    path.write_bytes(b"")
    made = restore_points.create_restore_point()
    assert "cleanup_warning" not in made
    assert path.exists()
    page = BeautifulSoup(_client().get("/settings").text, "html.parser")
    row = page.select_one(f'a[href="{_url(copies, "point")}/delete"]').find_parent(class_="restore-point-row")
    assert row.select_one('button[type="submit"]').has_attr("disabled")
    assert all(button.has_attr("disabled") for button in row.select("button"))
    assert "Пустой архив" in row.get_text()
