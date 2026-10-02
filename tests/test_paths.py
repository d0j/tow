"""One install folder: root discovery and every path derived from it (docs/PORTABLE.md)."""

from __future__ import annotations

import os
import tempfile
import time
from pathlib import Path

import pytest

from tow import paths

_ENV = ("TOW_ROOT", "TOW_HOME", "TOPIC_WATCH_HOME", "TOW_CONFIG")


@pytest.fixture
def bare(monkeypatch):
    """No explicit locations: the root comes from where the code is."""
    for name in _ENV:
        monkeypatch.delenv(name, raising=False)


def _checkout(folder: Path) -> Path:
    (folder / "src" / "tow").mkdir(parents=True)
    (folder / "pyproject.toml").write_text("[project]\nname = 'tow'\n", encoding="utf-8")
    return folder


def test_runtime_install_is_the_parent_of_app(bare, monkeypatch, tmp_path):
    app = _checkout(tmp_path / "TOW" / "app")
    (tmp_path / "TOW" / "config.yaml").write_text("{}\n", encoding="utf-8")
    monkeypatch.setattr(paths, "repo_root", lambda: app)

    install = tmp_path / "TOW"
    assert paths.root() == install
    assert paths.config_path() == install / "config.yaml"
    assert paths.data_dir() == install / "data"
    assert paths.keys_dir() == install / "keys"
    assert paths.key_file() == install / "keys" / "master.key"
    assert paths.backup_root() == install / "backup"
    assert paths.runtime_dir() == install / "runtime"
    assert paths.tmp_dir() == install / "data" / "tmp"
    assert paths.logs_dir() == install / "data" / "logs"
    assert paths.run_dir() == install / "data" / "run"


def test_a_data_folder_alone_also_marks_the_runtime_layout(bare, monkeypatch, tmp_path):
    app = _checkout(tmp_path / "TOW" / "app")
    (tmp_path / "TOW" / "data").mkdir()
    monkeypatch.setattr(paths, "repo_root", lambda: app)
    assert paths.root() == tmp_path / "TOW"


def test_a_checkout_is_its_own_root(bare, monkeypatch, tmp_path):
    # Not named app, or an app folder with nothing next to it: development.
    for code in (_checkout(tmp_path / "tow"), _checkout(tmp_path / "solo" / "app")):
        monkeypatch.setattr(paths, "repo_root", lambda code=code: code)
        assert paths.root() == code
        assert paths.keys_dir() == code / "keys"


def test_tow_root_wins_and_home_and_config_stay_explicit(monkeypatch, tmp_path):
    monkeypatch.setenv("TOW_ROOT", str(tmp_path / "install"))
    monkeypatch.setenv("TOW_HOME", str(tmp_path / "elsewhere" / "data"))
    monkeypatch.setenv("TOW_CONFIG", str(tmp_path / "elsewhere" / "config.yaml"))

    assert paths.root() == tmp_path / "install"
    assert paths.data_dir() == tmp_path / "elsewhere" / "data"
    assert paths.config_path() == tmp_path / "elsewhere" / "config.yaml"
    assert paths.key_file() == tmp_path / "install" / "keys" / "master.key"
    assert paths.tmp_dir() == tmp_path / "elsewhere" / "data" / "tmp"


def test_an_old_launcher_that_set_tow_root_to_the_app_folder_still_finds_the_install(monkeypatch, tmp_path):
    app = _checkout(tmp_path / "TOW" / "app")
    (tmp_path / "TOW" / "config.yaml").write_text("{}\n", encoding="utf-8")
    monkeypatch.setattr(paths, "repo_root", lambda: app)
    monkeypatch.setenv("TOW_ROOT", str(app))
    assert paths.root() == tmp_path / "TOW"


def test_an_installed_package_takes_the_root_from_the_explicit_locations(bare, monkeypatch, tmp_path):
    monkeypatch.setattr(paths, "repo_root", lambda: tmp_path / "venv" / "Lib")
    monkeypatch.setenv("TOW_CONFIG", str(tmp_path / "install" / "config.yaml"))
    assert paths.root() == tmp_path / "install"


def test_folders_outside_data_are_not_created_by_asking(monkeypatch, tmp_path):
    monkeypatch.setenv("TOW_ROOT", str(tmp_path / "install"))
    for folder in (paths.keys_dir(), paths.backup_root(), paths.runtime_dir()):
        assert not folder.exists()
    assert paths.run_dir().is_dir()


def test_private_temp_moves_tempfile_and_child_processes_into_the_data_folder(monkeypatch, tmp_path):
    folder = paths.use_private_temp()

    assert folder == tmp_path / "tmp"
    assert tempfile.gettempdir() == str(folder)
    for name in ("TMP", "TEMP", "TMPDIR"):
        assert os.environ[name] == str(folder)
    with tempfile.NamedTemporaryFile(delete=False) as handle:
        created = Path(handle.name)
    assert created.parent == folder


def test_private_temp_sweeps_what_crashed_processes_left(tmp_path):
    folder = tmp_path / "tmp"
    (folder / "old-dir").mkdir(parents=True)
    (folder / "old-dir" / "x.bin").write_bytes(b"x")
    (folder / "old.tmp").write_bytes(b"x")
    (folder / "fresh.tmp").write_bytes(b"x")
    day_ago = time.time() - 2 * paths.TEMP_MAX_AGE_SEC
    for name in ("old-dir", "old.tmp"):
        os.utime(folder / name, (day_ago, day_ago))

    paths.use_private_temp()

    assert sorted(entry.name for entry in folder.iterdir()) == ["fresh.tmp"]


def test_private_temp_without_a_data_folder_changes_nothing(bare, monkeypatch, tmp_path):
    monkeypatch.setattr(paths, "repo_root", lambda: tmp_path / "nowhere")
    before = tempfile.tempdir
    assert paths.use_private_temp() is None
    assert tempfile.tempdir == before


def test_every_cli_command_keeps_its_temporary_files_in_the_data_folder(tmp_path):
    from tow import cli

    assert cli.main(["version"]) == 0
    assert tempfile.gettempdir() == str(tmp_path / "tmp")
    assert os.environ["TMP"] == os.environ["TEMP"] == os.environ["TMPDIR"] == str(tmp_path / "tmp")


def test_the_web_app_keeps_its_temporary_files_in_the_data_folder(tmp_path):
    from fastapi.testclient import TestClient

    from tow.web import app

    with TestClient(app) as client:  # runs the app's startup
        assert client.get("/healthz").status_code == 200
    assert tempfile.gettempdir() == str(tmp_path / "tmp")
