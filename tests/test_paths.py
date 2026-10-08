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


def test_a_copy_running_the_originals_code_is_told_apart(monkeypatch, tmp_path):
    # Audit 08.10.2026: a copied .venv imports the original's app/src while the original folder
    # is still there; its launcher names the copy as TOW_ROOT.
    original = _checkout(tmp_path / "TOW" / "app")
    copy = _checkout(tmp_path / "Copy of TOW" / "app")
    (tmp_path / "Copy of TOW" / "data").mkdir()
    monkeypatch.setattr(paths, "repo_root", lambda: original)
    monkeypatch.setenv("TOW_ROOT", str(copy.parent))
    assert paths.foreign_code() == copy.parent

    monkeypatch.setattr(paths, "repo_root", lambda: copy)  # after setup: its own code
    assert paths.foreign_code() is None
    monkeypatch.setenv("TOW_ROOT", str(copy))  # an older launcher's TOW_ROOT (the code folder)
    assert paths.foreign_code() is None


def test_a_tow_root_left_from_a_move_is_not_followed(monkeypatch, tmp_path):
    # Audit 08.10.2026: TOW_ROOT=C:\TOW left for the whole account after moving to D:\TOW made
    # TOW create data/ and a new master key in the old place and start with no data.
    import sys

    app = _checkout(tmp_path / "moved" / "TOW" / "app")
    (tmp_path / "moved" / "TOW" / "config.yaml").write_text("{}\n", encoding="utf-8")
    monkeypatch.setattr(paths, "repo_root", lambda: app)
    monkeypatch.setattr(sys, "prefix", str(app / ".venv"))  # this install's own environment
    monkeypatch.delenv("TOW_HOME", raising=False)
    _checkout(tmp_path / "old-copy" / "TOW" / "app")  # the old folder may still be there too
    for old in (tmp_path / "old" / "TOW", tmp_path / "old-copy" / "TOW"):
        monkeypatch.setenv("TOW_ROOT", str(old))
        assert paths.root() == tmp_path / "moved" / "TOW"
        assert paths.data_dir() == tmp_path / "moved" / "TOW" / "data"
        assert paths.root_env_ignored() is True
        assert paths.foreign_code() is None
    assert not (tmp_path / "old" / "TOW").exists()  # nothing created in the old place

    # This install named by TOW_ROOT, also as an older launcher's code folder: followed.
    for own in (tmp_path / "moved" / "TOW", app):
        monkeypatch.setenv("TOW_ROOT", str(own))
        assert paths.root() == tmp_path / "moved" / "TOW"
        assert paths.root_env_ignored() is False
    assert paths.root_env_ignored("") is False


def test_a_copys_environment_running_this_code_is_not_redirected(monkeypatch, tmp_path):
    # The other way round: the environment is the copy's (its .venv still imports this code).
    # TOW_ROOT, the copy, is followed - and the copy is refused for running another folder's code.
    import sys

    original = _checkout(tmp_path / "TOW" / "app")
    (tmp_path / "TOW" / "data").mkdir()
    copy = _checkout(tmp_path / "Copy of TOW" / "app").parent
    (copy / "app" / ".venv").mkdir()
    monkeypatch.setattr(paths, "repo_root", lambda: original)
    monkeypatch.setattr(sys, "prefix", str(copy / "app" / ".venv"))
    monkeypatch.setenv("TOW_ROOT", str(copy))
    assert paths.root() == copy
    assert paths.root_env_ignored() is False
    assert paths.foreign_code() == copy


def test_a_checkout_or_a_root_without_code_has_nothing_to_compare(bare, monkeypatch, tmp_path):
    code = _checkout(tmp_path / "tow")
    monkeypatch.setattr(paths, "repo_root", lambda: code)
    assert paths.foreign_code() is None  # development: the checkout is the root
    monkeypatch.setenv("TOW_ROOT", str(tmp_path / "scratch"))  # tests, a wheel: no app/ there
    assert paths.foreign_code() is None
    (tmp_path / "scratch" / "app").mkdir(parents=True)  # an app/ without TOW's code in it
    assert paths.foreign_code() is None


def test_the_cli_refuses_another_folders_code_before_touching_the_folder(monkeypatch, tmp_path, capsys):
    import json

    from tow import cli

    original = _checkout(tmp_path / "TOW" / "app")
    copy = _checkout(tmp_path / "Copy of TOW" / "app").parent
    monkeypatch.setattr(paths, "repo_root", lambda: original)
    monkeypatch.setenv("TOW_ROOT", str(copy))
    monkeypatch.delenv("TOW_HOME", raising=False)
    monkeypatch.delenv("TOW_CONFIG", raising=False)

    assert cli.main(["status"]) == cli.EXIT_CANNOT_RUN
    said = capsys.readouterr().err
    assert str(original) in said
    assert str(copy) in said
    assert "setup" in said
    assert not (copy / "data").exists()  # nothing created in the copy

    assert cli.main(["status", "--json"]) == cli.EXIT_CANNOT_RUN
    answer = json.loads(capsys.readouterr().out)
    assert answer["ok"] is False
    assert str(copy) in answer["error"]


def test_folders_outside_data_are_not_created_by_asking(monkeypatch, tmp_path):
    monkeypatch.setenv("TOW_ROOT", str(tmp_path / "install"))
    for folder in (paths.keys_dir(), paths.backup_root(), paths.runtime_dir()):
        assert not folder.exists()
    assert paths.run_dir().is_dir()


def test_an_explicit_key_file_is_absolute_or_inside_the_data_folder(monkeypatch, tmp_path):
    monkeypatch.setenv("TOW_HOME", str(tmp_path / "data"))
    monkeypatch.delenv("TOW_MASTER_KEY_FILE", raising=False)
    assert paths.explicit_key_file() is None
    monkeypatch.setenv("TOW_MASTER_KEY_FILE", str(tmp_path / "elsewhere" / "master.key"))
    assert paths.explicit_key_file() == tmp_path / "elsewhere" / "master.key"
    monkeypatch.setenv("TOW_MASTER_KEY_FILE", "keys/master.key")
    assert paths.explicit_key_file() == (tmp_path / "data" / "keys" / "master.key").resolve()
    for outside in ("../master.key", "keys/../../master.key"):
        monkeypatch.setenv("TOW_MASTER_KEY_FILE", outside)
        with pytest.raises(ValueError, match="out of the data folder"):
            paths.explicit_key_file()


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
