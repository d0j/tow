"""Backup paths must not depend on a drive's ambient current directory."""

from pathlib import PureWindowsPath

import pytest
from fastapi.testclient import TestClient
from helpers import flash_of

from tow import locations
from tow.config import load_config, save_config
from tow.i18n import t
from tow.locations import MANUAL, NIGHT, problem
from tow.paths import config_path

VALUES = ["D:copies", "D:", "C:copies", "z:copies/sub", r"a:copies\sub", "E:.", "F:копии", "C::copies"]


def test_foreign_drive_relative_path_is_not_anchored_to_install():
    path = PureWindowsPath("C:/install") / "D:copies"
    assert str(path) == "D:copies"
    assert not path.is_absolute()


@pytest.mark.parametrize("value", VALUES)
@pytest.mark.parametrize("location", [NIGHT, MANUAL])
def test_drive_relative_backup_path_is_refused_before_resolution(value, location, monkeypatch):
    monkeypatch.setattr(locations, "resolve", lambda *_a: pytest.fail("drive-relative resolution"))
    assert problem(value, location) == t("locations.drive_relative")
    assert problem(f'  "{value}"  ', location) == t("locations.drive_relative")
    with pytest.raises(locations.LocationError, match="D:copies"):
        locations.resolve_checked(value, location)


@pytest.mark.parametrize("value", VALUES)
@pytest.mark.parametrize("key", ["backup_dir", "restore_points_dir"])
def test_configured_drive_relative_path_is_refused_when_used(value, key, monkeypatch):
    from tow.restore_points import RestorePointError, restore_points_dir
    from tow.snapshots import SnapshotError, backup_root

    cfg = load_config()
    cfg[key] = value
    save_config(cfg)
    monkeypatch.setattr(locations, "resolve", lambda *_a: pytest.fail("configured drive-relative resolution"))
    error = SnapshotError if key == "backup_dir" else RestorePointError
    with pytest.raises(error, match="D:copies"):
        backup_root() if key == "backup_dir" else restore_points_dir()


@pytest.mark.parametrize("value", VALUES)
@pytest.mark.parametrize("kind", ["night", "manual"])
@pytest.mark.parametrize("action", ["check", "save"])
def test_settings_refuse_drive_relative_path_without_probe_or_config_change(value, kind, action, monkeypatch):
    from tow.web import app

    monkeypatch.setattr("tow.web.services.folder_write_problem", lambda *_a: pytest.fail("drive-relative write probe"))
    before = config_path().read_bytes()
    response = TestClient(app, headers={"Origin": "http://127.0.0.1"}).post(
        "/settings/backup/location",
        data={"kind": kind, "path": value, "action": action},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert t("locations.drive_relative") in flash_of(response.headers["location"])
    assert config_path().read_bytes() == before


@pytest.mark.parametrize("value", ["copies", "copies/daily", "copies-daily", "copies daily"])
def test_ordinary_relative_folders_still_use_the_install(value):
    assert problem(value, MANUAL) is None
    assert locations.resolve_checked(value, MANUAL) == locations.install_dir() / value


@pytest.mark.parametrize("value", ["C:/copies", r"D:\copies", "z:/copies/sub", r"\\nas\share\copies"])
def test_fully_qualified_windows_paths_do_not_trigger_drive_relative_refusal(value, monkeypatch):
    # No filesystem/network lookup: this assertion tests only the lexical guard and dispatch.
    monkeypatch.setattr(locations, "_of_another_system", lambda _v: True)
    assert problem(value, MANUAL) == t("locations.other_system")
