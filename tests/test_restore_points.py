from __future__ import annotations

import asyncio
import io
import json
import re
from pathlib import Path

import pytest
import yaml
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from helpers import flash_kind, shown

from tow import restore_points
from tow.bundle import ExportImportError
from tow.config import load_config, save_config
from tow.i18n import t
from tow.restore_points import (
    CREATE_FAILED,
    INVALID_FILE,
    ROLLBACK_FAILED,
    RestorePointError,
    create_restore_point,
    list_restore_points,
    restore_from_point,
)
from tow.store import load_secrets, load_state, save_secret_undo, save_secrets, save_state
from tow.web import app


def _seed(monkeypatch, tmp_path: Path, language: str = "ru") -> Path:
    home = tmp_path / "data"
    config_path = tmp_path / "config.yaml"
    monkeypatch.setenv("TOW_HOME", str(home))
    monkeypatch.setenv("TOW_CONFIG", str(config_path))
    monkeypatch.setenv("TOW_MASTER_KEY", Fernet.generate_key().decode("ascii"))
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
                "language": language,  # the suite asserts the Russian texts (tests/conftest.py)
            },
            sort_keys=False,
        ),
        encoding="utf-8",
    )
    save_state({"topics": [{"id": "before"}], "mirrors": {}})
    (home / "download_history.json").write_text(
        json.dumps({"schema_version": 1, "topics": {}}),
        encoding="utf-8",
    )
    save_secrets({"telegram": {"token": "test-secret"}})
    return config_path


def test_create_and_restore_preserves_current_lan_access(monkeypatch, tmp_path):
    _seed(monkeypatch, tmp_path)
    saved = create_restore_point()

    changed = load_config()
    changed.update(
        {
            "bind": "0.0.0.0",
            "port": 8787,
            "allow_lan": True,
            "interval_sec": 222,
        }
    )
    save_config(changed)
    save_state({"topics": [{"id": "after"}], "mirrors": {}})
    current_lan_password = {"salt": "current", "hash": "current"}
    save_secrets(
        {
            "telegram": {"token": "current-token"},
            "lan_auth": current_lan_password,
        }
    )

    result = restore_from_point(saved["id"])

    restored = load_config()
    assert result["access_preserved"] is True
    assert restored["interval_sec"] == 111
    assert restored["bind"] == "0.0.0.0"
    assert restored["port"] == 8787
    assert restored["allow_lan"] is True
    assert "lan_auth" not in restored  # the point's obsolete flag is read and ignored
    assert load_state()["topics"] == [{"id": "before"}]
    assert load_secrets()["telegram"]["token"] == "test-secret"
    assert load_secrets()["lan_auth"] == current_lan_password
    assert len(list_restore_points()) == 2


def test_a_directory_named_like_a_restore_point_is_not_listed_or_pruned(monkeypatch, tmp_path):
    _seed(monkeypatch, tmp_path)
    folder = restore_points.restore_points_dir() / "20260101T000000Z-deadbeef.towx"
    folder.mkdir(parents=True)
    (folder / "keep.txt").write_text("not a restore point", encoding="utf-8")
    assert list_restore_points() == []
    made = create_restore_point()
    assert [point["id"] for point in list_restore_points()] == [made["id"]]
    assert (folder / "keep.txt").read_text(encoding="utf-8") == "not a restore point"


@pytest.mark.parametrize("point_id", ["../state", "..\\state", "C:evil", "not-an-id"])
def test_restore_rejects_untrusted_identifier(monkeypatch, tmp_path, point_id):
    _seed(monkeypatch, tmp_path)

    with pytest.raises(RestorePointError, match=re.escape(t("backup.restore_point.unknown"))):
        restore_from_point(point_id)


def test_tampered_restore_point_does_not_replace_state(monkeypatch, tmp_path):
    _seed(monkeypatch, tmp_path)
    saved = create_restore_point()
    path = tmp_path / "data" / "restore-points" / f"{saved['id']}.towx"
    path.write_bytes(path.read_bytes()[:-12] + b"tampered-data")
    save_state({"topics": [{"id": "current"}], "mirrors": {}})

    with pytest.raises(RestorePointError, match=re.escape(t("backup.restore_point.validation_failed"))):
        restore_from_point(saved["id"])

    assert load_state()["topics"] == [{"id": "current"}]


def test_browser_file_export_check_and_restore_round_trip(monkeypatch, tmp_path):
    _seed(monkeypatch, tmp_path)
    client = TestClient(app, headers={"Origin": "http://127.0.0.1"})

    exported = client.post("/settings/portable/export")

    assert exported.status_code == 200
    assert exported.content.startswith(b"{")
    assert ".towx" in exported.headers["content-disposition"]
    assert exported.headers["cache-control"] == "no-store"
    assert list((tmp_path / "data").glob("tow-browser-export-*")) == []

    save_state({"topics": [{"id": "current"}], "mirrors": {}})
    checked = client.post(
        "/settings/portable/import",
        data={"operation": "check"},
        files={"backup_file": ("backup.towx", exported.content, "application/octet-stream")},
        follow_redirects=False,
    )

    assert checked.status_code == 303
    assert "можно восстановить" in shown(checked.headers["location"])
    assert load_state()["topics"] == [{"id": "current"}]
    assert list((tmp_path / "data").glob("tow-browser-import-*")) == []

    restored = client.post(
        "/settings/portable/import",
        data={"operation": "restore"},
        files={"backup_file": ("backup.towx", exported.content, "application/octet-stream")},
        follow_redirects=False,
    )

    assert restored.status_code == 303
    assert "восстановлено из файла" in shown(restored.headers["location"])
    assert load_state()["topics"] == [{"id": "before"}]
    assert list((tmp_path / "data").glob("tow-browser-import-*")) == []


@pytest.mark.parametrize("origin", [None, "null", "https://evil.example"])
def test_browser_export_rejects_missing_or_foreign_origin(monkeypatch, tmp_path, origin):
    _seed(monkeypatch, tmp_path)
    client = TestClient(app)
    headers = {"Origin": origin} if origin is not None else {}

    response = client.post("/settings/portable/export", headers=headers)

    assert response.status_code == 403
    assert list((tmp_path / "data").glob("tow-browser-export-*")) == []


def test_browser_export_is_post_only(monkeypatch, tmp_path):
    _seed(monkeypatch, tmp_path)

    response = TestClient(app).get("/settings/portable/export")

    assert response.status_code == 405


@pytest.mark.parametrize("error", [RestorePointError("broken bundle"), OSError("disk full")])
def test_browser_export_failure_cleans_temporary_directory(monkeypatch, tmp_path, error):
    _seed(monkeypatch, tmp_path)
    monkeypatch.setattr("tow.web.services.export_portable_bundle", lambda _path: (_ for _ in ()).throw(error))
    client = TestClient(app, headers={"Origin": "http://127.0.0.1"})

    response = client.post("/settings/portable/export", follow_redirects=False)

    assert response.status_code == 303
    assert "не удалось сохранить файл" in shown(response.headers["location"])
    assert list((tmp_path / "data").glob("tow-browser-export-*")) == []


def test_browser_rejects_tampered_file_without_state_change(monkeypatch, tmp_path):
    _seed(monkeypatch, tmp_path)
    client = TestClient(app, headers={"Origin": "http://127.0.0.1"})
    exported = client.post("/settings/portable/export").content
    save_state({"topics": [{"id": "current"}], "mirrors": {}})

    response = client.post(
        "/settings/portable/import",
        data={"operation": "restore"},
        files={
            "backup_file": (
                "backup.towx",
                exported[:-12] + b"tampered-data",
                "application/octet-stream",
            )
        },
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert "данные не изменены" in shown(response.headers["location"])
    assert load_state()["topics"] == [{"id": "current"}]
    assert list_restore_points() == []


def test_browser_rejects_oversized_request_before_multipart_parse(monkeypatch, tmp_path):
    _seed(monkeypatch, tmp_path)
    client = TestClient(app, headers={"Origin": "http://127.0.0.1"})

    response = client.post(
        "/settings/portable/import",
        content=b"x",
        headers={"Content-Length": str(70 * 1024 * 1024)},
    )

    assert response.status_code == 413


@pytest.mark.parametrize("language", ["en", "ru"])
@pytest.mark.parametrize("operation", ["check", "restore"])
@pytest.mark.parametrize("failure", [PermissionError, FileNotFoundError, OSError])
def test_browser_import_reports_preparation_failure_without_changing_data(
    monkeypatch, tmp_path, language, operation, failure
):
    _seed(monkeypatch, tmp_path, language)
    before = {path: path.read_bytes() for path in tmp_path.rglob("*") if path.is_file()}

    def cannot_prepare(*_args, **_kwargs):
        raise failure("synthetic storage failure")

    def must_not_import(_path):
        pytest.fail("an unprepared upload must never enter the import engine")

    monkeypatch.setattr("tow.web.routes_backup.tempfile.mkdtemp", cannot_prepare)
    monkeypatch.setattr("tow.web.services.check_portable_bundle", must_not_import)
    monkeypatch.setattr("tow.web.services.restore_portable_bundle", must_not_import)
    client = TestClient(app, headers={"Origin": "http://127.0.0.1"}, raise_server_exceptions=False)

    response = client.post(
        "/settings/portable/import",
        data={"operation": operation},
        files={"backup_file": ("backup.towx", b"not-empty", "application/octet-stream")},
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert t("web.settings.file_stage_failed", language) in shown(response.headers["location"])
    assert all(path.read_bytes() == content for path, content in before.items())
    assert list((tmp_path / "data").glob("tow-browser-import-*")) == []


def test_browser_import_cleans_staged_file_when_upload_close_fails(monkeypatch, tmp_path):
    from starlette.datastructures import UploadFile

    from tow.web.routes_backup import settings_portable_import

    _seed(monkeypatch, tmp_path)
    upload = UploadFile(file=io.BytesIO(b"not-empty"), filename="backup.towx")

    async def cannot_close():
        raise OSError("synthetic close failure")

    monkeypatch.setattr(upload, "close", cannot_close)
    monkeypatch.setattr("tow.web.services.check_portable_bundle", lambda _path: None)

    with pytest.raises(OSError, match="synthetic close failure"):
        asyncio.run(settings_portable_import(upload, "check"))

    assert list((tmp_path / "data").glob("tow-browser-import-*")) == []
    assert load_state()["topics"] == [{"id": "before"}]
    upload.file.close()


@pytest.mark.parametrize("operation", ["check", "restore"])
@pytest.mark.parametrize(
    "filename", ["backup.towx", "../../outside.towx", r"..\..\outside.towx", r"Z:\outside.towx", "/outside.towx"]
)
def test_browser_import_never_uses_the_uploaded_filename_as_a_path(monkeypatch, tmp_path, operation, filename):
    _seed(monkeypatch, tmp_path)
    seen = []

    def inspect_staging(path):
        seen.append(path)
        assert path.name == "uploaded.towx"
        assert path.parent.name.startswith("tow-browser-import-")
        assert path.parent.parent.resolve() == (tmp_path / "data").resolve()
        return {"safety_point": "synthetic-point"}

    monkeypatch.setattr("tow.web.services.check_portable_bundle", inspect_staging)
    monkeypatch.setattr("tow.web.services.restore_portable_bundle", inspect_staging)
    client = TestClient(app, headers={"Origin": "http://127.0.0.1"})

    response = client.post(
        "/settings/portable/import",
        data={"operation": operation},
        files={"backup_file": (filename, b"not-empty", "application/octet-stream")},
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert len(seen) == 1
    assert list((tmp_path / "data").glob("tow-browser-import-*")) == []
    assert load_state()["topics"] == [{"id": "before"}]


def test_browser_reports_critical_rollback_failure_truthfully(monkeypatch, tmp_path):
    _seed(monkeypatch, tmp_path)
    client = TestClient(app, headers={"Origin": "http://127.0.0.1"})

    def fail_restore(_path):
        raise RestorePointError("any text, any language", kind=ROLLBACK_FAILED)

    monkeypatch.setattr("tow.web.services.restore_portable_bundle", fail_restore)
    response = client.post(
        "/settings/portable/import",
        data={"operation": "restore"},
        files={"backup_file": ("backup.towx", b"not-empty", "application/octet-stream")},
        follow_redirects=False,
    )

    assert response.status_code == 303
    message = shown(response.headers["location"])
    assert "критическая ошибка отката" in message
    assert "данные могли измениться" in message


@pytest.mark.parametrize("language", ["en", "ru"])
@pytest.mark.parametrize("source", ["file", "point"])
def test_browser_reports_restored_data_but_missing_audit_event(monkeypatch, tmp_path, language, source):
    _seed(monkeypatch, tmp_path, language)
    client = TestClient(app, headers={"Origin": "http://127.0.0.1"})
    if source == "point":
        point = create_restore_point()
    else:
        exported = client.post("/settings/portable/export").content
    save_state({"topics": [{"id": "current"}], "mirrors": {}})

    def unavailable_log():
        raise OSError("synthetic audit storage unavailable")

    monkeypatch.setattr("tow.log.log_path", unavailable_log)
    if source == "point":
        response = client.post(f"/settings/restore-points/{point['id']}/restore", follow_redirects=False)
        success = t("web.settings.restored", language)
    else:
        response = client.post(
            "/settings/portable/import",
            data={"operation": "restore"},
            files={"backup_file": ("backup.towx", exported, "application/octet-stream")},
            follow_redirects=False,
        )
        success = t("web.settings.restored_file", language)

    assert response.status_code == 303
    message = shown(response.headers["location"])
    assert success in message
    assert t("web.settings.audit_missing", language) in message
    assert flash_kind(response.headers["location"]) == "warn"
    assert t("web.settings.file_unreadable", language) not in message
    assert load_state()["topics"] == [{"id": "before"}]


def test_restore_point_and_export_work_after_settings_and_site_edits(monkeypatch, tmp_path):
    # Regression: a substring heuristic took TOW's own undo metadata (secret_scope,
    # secret_undo_ref, secrets_undo_ref) for plaintext secrets and refused every backup.
    _seed(monkeypatch, tmp_path)
    reference = save_secret_undo(load_secrets())
    save_state(
        {
            "topics": [{"id": "before"}],
            "mirrors": {},
            "undo": {
                "kind": "settings",
                "secrets_undo_ref": reference,
                "secret_undo_ref": reference,
                "secret_scope": ["telegram"],
                "interval_sec": 111,
            },
        }
    )

    assert create_restore_point()["id"]
    exported = TestClient(app, headers={"Origin": "http://127.0.0.1"}).post("/settings/portable/export")
    assert exported.status_code == 200
    assert exported.content.startswith(b"{")


def test_plaintext_secret_in_state_is_refused_with_its_path(monkeypatch, tmp_path):
    _seed(monkeypatch, tmp_path)
    save_state({"topics": [{"id": "t1", "auth": {"password": "hunter2"}}], "mirrors": {}})

    with pytest.raises(RestorePointError, match=r"state\.json:topics\[0\]\.auth\.password") as caught:
        create_restore_point()

    assert "hunter2" not in str(caught.value)
    assert list_restore_points() == []


def test_restore_point_failure_flash_names_the_reason(monkeypatch, tmp_path):
    _seed(monkeypatch, tmp_path)
    save_state({"topics": [], "mirrors": {}, "telegram_token": "x"})

    client = TestClient(app, headers={"Origin": "http://127.0.0.1"})
    response = client.post("/settings/restore-points", follow_redirects=False)

    assert response.status_code == 303
    message = shown(response.headers["location"])
    assert "не удалось сохранить точку" in message
    assert "state.json:telegram_token" in message


def test_restoring_another_interval_needs_only_the_config(monkeypatch, tmp_path):
    # N9 (1.21): `tow run` reads interval_sec from the restored config; no scheduler to follow.
    _seed(monkeypatch, tmp_path)
    saved = create_restore_point()  # interval_sec 111
    changed = load_config()
    changed["interval_sec"] = 222
    save_config(changed)

    client = TestClient(app, headers={"Origin": "http://127.0.0.1"})
    response = client.post(f"/settings/restore-points/{saved['id']}/restore", follow_redirects=False)

    assert load_config()["interval_sec"] == 111
    assert "расписание" not in shown(response.headers["location"])


# --- failures are told apart by kind, in every language -------------------------------------
# Regression: the web flash matched English fragments that only ru.json's untranslated entries
# contained, so in English (the default) "restore and its undo both failed" read as a plain
# "restore failed".

LANGUAGES = ["en", "ru"]


def _upload(client: TestClient, content: bytes, operation: str = "restore") -> str:
    response = client.post(
        "/settings/portable/import",
        data={"operation": operation},
        files={"backup_file": ("backup.towx", content, "application/octet-stream")},
        follow_redirects=False,
    )
    assert response.status_code == 303
    return shown(response.headers["location"])


@pytest.mark.parametrize("language", LANGUAGES)
@pytest.mark.parametrize("operation", ["check", "restore"])
def test_browser_copy_needs_its_original_key_and_preserves_a_different_install(
    monkeypatch, tmp_path, language, operation
):
    import os

    _seed(monkeypatch, tmp_path, language)
    original_key = os.environ["TOW_MASTER_KEY"]
    client = TestClient(app, headers={"Origin": "http://127.0.0.1"})
    exported = client.post("/settings/portable/export").content
    destination = tmp_path / "different-install"
    destination.mkdir()
    _seed(monkeypatch, destination, language)
    members = [
        destination / "config.yaml",
        *(destination / "data" / name for name in ("state.json", "download_history.json", "secrets.enc")),
    ]
    before = {path: path.read_bytes() for path in members}

    assert t("web.settings.file_invalid", language) in _upload(client, exported, operation)
    assert {path: path.read_bytes() for path in members} == before
    assert not (destination / "data" / "restore-points").exists()

    # The same archive is valid with its source key: it is not a corrupted export.
    saved_file = tmp_path / "source-copy.towx"
    saved_file.write_bytes(exported)
    monkeypatch.setenv("TOW_MASTER_KEY", original_key)
    assert restore_points.check_portable_bundle(saved_file)["preview"] is True
    assert {path: path.read_bytes() for path in members} == before


@pytest.mark.parametrize("language", LANGUAGES)
def test_master_key_requirement_is_visible_in_the_file_copy_card(monkeypatch, tmp_path, language):
    from bs4 import BeautifulSoup

    _seed(monkeypatch, tmp_path, language)
    response = TestClient(app).get("/settings")
    assert response.status_code == 200
    card = BeautifulSoup(response.text, "html.parser").find(id="backup-file")
    assert card is not None
    assert "keys/master.key" in card.get_text(" ", strip=True)


def _drift_then_fail_rollback(monkeypatch) -> None:
    """The restore applies, loses the network settings, and its undo fails too."""
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


@pytest.mark.parametrize("language", LANGUAGES)
def test_file_restore_whose_undo_fails_is_critical_in_every_language(monkeypatch, tmp_path, language):
    _seed(monkeypatch, tmp_path, language)
    client = TestClient(app, headers={"Origin": "http://127.0.0.1"})
    exported = client.post("/settings/portable/export").content
    _drift_then_fail_rollback(monkeypatch)

    message = _upload(client, exported)

    assert t("web.settings.rollback_critical", language) in message
    assert t("web.settings.restore_failed", language) not in message


@pytest.mark.parametrize("language", LANGUAGES)
def test_point_restore_whose_undo_fails_is_critical_in_every_language(monkeypatch, tmp_path, language):
    _seed(monkeypatch, tmp_path, language)
    saved = create_restore_point()
    _drift_then_fail_rollback(monkeypatch)

    response = TestClient(app, headers={"Origin": "http://127.0.0.1"}).post(
        f"/settings/restore-points/{saved['id']}/restore", follow_redirects=False
    )

    assert t("web.settings.rollback_critical", language) in shown(response.headers["location"])


@pytest.mark.parametrize("language", LANGUAGES)
def test_import_engine_rollback_failure_is_critical_too(monkeypatch, tmp_path, language):
    _seed(monkeypatch, tmp_path, language)
    saved = create_restore_point()
    real_import = restore_points.import_bundle

    def apply_and_its_rollback_fail(path, passphrase, **kwargs):
        if kwargs.get("apply"):
            raise ExportImportError("import failed and destination rollback also failed")
        return real_import(path, passphrase, **kwargs)

    monkeypatch.setattr(restore_points, "import_bundle", apply_and_its_rollback_fail)

    with pytest.raises(RestorePointError) as caught:
        restore_from_point(saved["id"])

    assert caught.value.kind == ROLLBACK_FAILED


@pytest.mark.parametrize("language", LANGUAGES)
@pytest.mark.parametrize("content", [b"not a bundle", b""])
def test_bad_file_says_invalid_in_every_language(monkeypatch, tmp_path, language, content):
    _seed(monkeypatch, tmp_path, language)
    client = TestClient(app, headers={"Origin": "http://127.0.0.1"})

    for operation in ("check", "restore"):
        assert t("web.settings.file_invalid", language) in _upload(client, content, operation)
    assert load_state()["topics"] == [{"id": "before"}]


@pytest.mark.parametrize("language", LANGUAGES)
def test_safety_point_failure_says_so_in_every_language(monkeypatch, tmp_path, language):
    _seed(monkeypatch, tmp_path, language)
    client = TestClient(app, headers={"Origin": "http://127.0.0.1"})
    exported = client.post("/settings/portable/export").content
    monkeypatch.setattr(restore_points, "_point_view", lambda _path: None)  # the new point never reads back

    with pytest.raises(RestorePointError) as caught:
        create_restore_point()
    assert caught.value.kind == CREATE_FAILED

    message = _upload(client, exported)

    assert t("web.settings.safety_point_failed", language) in message
    assert load_state()["topics"] == [{"id": "before"}]


def test_tampered_point_is_an_invalid_file(monkeypatch, tmp_path):
    _seed(monkeypatch, tmp_path, "en")
    saved = create_restore_point()
    path = tmp_path / "data" / "restore-points" / f"{saved['id']}.towx"
    path.write_bytes(path.read_bytes()[:-12] + b"tampered-data")

    with pytest.raises(RestorePointError) as caught:
        restore_from_point(saved["id"])

    assert caught.value.kind == INVALID_FILE
    assert str(caught.value) == t("backup.restore_point.validation_failed", "en")
