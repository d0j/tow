"""scripts/update-smoke.py: the archives it builds and the checks it makes (CI runs it for real)."""

import argparse
import importlib.util
import io
import re
import runpy
import tarfile
from pathlib import Path

import pytest

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "update-smoke.py"


@pytest.fixture
def smoke():
    spec = importlib.util.spec_from_file_location("update_smoke_under_test", SCRIPT)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _archive(path, files):
    with tarfile.open(path, "w:gz") as tar:
        top = tarfile.TarInfo("tow")
        top.type = tarfile.DIRTYPE
        tar.addfile(top)
        for name, data in files.items():
            info = tarfile.TarInfo(f"tow/{name}")
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return path


FILES = {
    "pyproject.toml": b'[project]\nname = "tow"\nversion = "1.2.3"\n',
    "src/tow/__init__.py": b'"""TOW."""\n',
    "scripts/update.py": b"# the updater\n",
}


def test_the_broken_copy_differs_only_in_the_package_init(smoke, tmp_path):
    source = _archive(tmp_path / "tow-source.tar.gz", FILES)
    broken = smoke.make_broken(source, tmp_path / "broken.tar.gz")
    good, bad = smoke.archive_files(source), smoke.archive_files(broken)
    assert set(good) == set(bad) == set(FILES)
    assert [name for name in good if good[name] != bad[name]] == ["src/tow/__init__.py"]
    assert smoke.archive_version(broken) == smoke.archive_version(source) == "1.2.3"
    with tarfile.open(broken, "r:gz") as tar:
        names = tar.getnames()
        member = tar.extractfile("tow/src/tow/__init__.py")
        assert member is not None
        text = member.read().decode("utf-8")
    assert names[0] == "tow"
    assert text.startswith('"""TOW."""\n')
    assert smoke.BROKEN_MARK in text


def test_the_broken_copy_changes_the_data_and_cannot_start(smoke, tmp_path, monkeypatch):
    root = tmp_path / "TOW"
    (root / "data").mkdir(parents=True)
    (root / "data" / smoke.SENTINEL).write_text(smoke.BEFORE, encoding="utf-8")
    monkeypatch.setenv("TOW_ROOT", str(root))
    code = tmp_path / "__init__.py"
    code.write_text(smoke.BROKEN_CODE, encoding="utf-8")
    with pytest.raises(SystemExit, match=smoke.BROKEN_MARK):
        runpy.run_path(str(code))
    assert (root / "data" / smoke.SENTINEL).read_text(encoding="utf-8") != smoke.BEFORE
    assert (root / "data" / smoke.ADDED).is_file()


def test_an_archive_without_the_package_is_refused(smoke, tmp_path):
    source = _archive(tmp_path / "tow-source.tar.gz", {"pyproject.toml": FILES["pyproject.toml"]})
    with pytest.raises(smoke.SmokeError, match=re.escape("no src/tow/__init__.py")):
        smoke.make_broken(source, tmp_path / "broken.tar.gz")


def test_the_previous_release_is_checked_against_its_sums(smoke, tmp_path):
    (tmp_path / "install.sh").write_bytes(b"#!/bin/sh\n")
    digest = smoke.sha256(tmp_path / "install.sh")
    (tmp_path / "SHA256SUMS").write_text(f"{digest}  install.sh\n", encoding="ascii")
    smoke.check_sums(tmp_path, ["install.sh"])
    with pytest.raises(smoke.SmokeError, match=re.escape("lists no tow-source.tar.gz")):
        smoke.check_sums(tmp_path, ["install.sh", "tow-source.tar.gz"])
    (tmp_path / "install.sh").write_bytes(b"#!/bin/sh\nexit 1\n")
    with pytest.raises(smoke.SmokeError, match="does not match"):
        smoke.check_sums(tmp_path, ["install.sh"])


def test_the_target_sums_name_the_asset_update_py_reads(smoke, tmp_path):
    archive = _archive(tmp_path / "broken.tar.gz", FILES)
    sums = smoke.parse_sums(smoke.write_sums(archive).read_text(encoding="ascii"))
    assert sums == {"tow-source.tar.gz": smoke.sha256(archive)}


def test_the_app_is_compared_without_its_environment_and_bytecode(smoke, tmp_path):
    app = tmp_path / "app"
    for name, data in FILES.items():
        (app / name).parent.mkdir(parents=True, exist_ok=True)
        (app / name).write_bytes(data)
    (app / ".venv" / "Lib").mkdir(parents=True)
    (app / ".venv" / "Lib" / "site.py").write_text("x", encoding="utf-8")
    (app / "src" / "tow" / "__pycache__").mkdir()
    (app / "src" / "tow" / "__pycache__" / "x.pyc").write_bytes(b"\0")
    assert smoke.tree_files(app) == smoke.archive_files(_archive(tmp_path / "a.tar.gz", FILES))
    assert smoke.project_version(app) == "1.2.3"


def test_the_updater_command_is_read_from_tow_update(smoke, capsys):
    from tow import cli

    cli._cmd_update(argparse.Namespace(ref="v1.2.3"))
    match = smoke.COMMAND_RE.search(capsys.readouterr().out)
    assert match is not None
    assert Path(match.group(2)).name == "update.py"
    assert Path(match.group(1)).name.startswith("python")


def test_the_environment_is_a_new_computer_s(smoke, monkeypatch):
    monkeypatch.setenv("TOW_MASTER_KEY_FILE", "synthetic")
    monkeypatch.setenv("UV_CACHE_DIR", "synthetic")
    monkeypatch.setenv("VIRTUAL_ENV", "synthetic")
    env = smoke.clean_environment()
    assert env["TOW_NO_BROWSER"] == "1"
    assert not {"TOW_MASTER_KEY_FILE", "UV_CACHE_DIR", "VIRTUAL_ENV"} & set(env)


def test_the_cut_off_switch_uses_the_updaters_test_hook(smoke):
    # Before: the smoke test never cut a real switch off, so the start files' refusal and the
    # recovery by the runtime/ copy ran only against fakes.
    from test_update import updater

    assert smoke.CUT_VARIABLE == updater.TEST_CUT_SWITCH
    assert smoke.CUT_EXIT == updater.TEST_CUT_EXIT
    assert smoke.CUT_VARIABLE.startswith("TOW_TEST_")
    source = SCRIPT.read_text(encoding="utf-8")
    assert source.index("self.cut_off_switch(") < source.index("self.check_start_refused()")
    assert smoke.Smoke.start_file() in {"Start TOW.cmd", "Start TOW.command", "start-tow"}


def test_never_on_the_live_port(smoke, tmp_path):
    with pytest.raises(SystemExit):
        smoke.main(["--previous", str(tmp_path), "--source", str(tmp_path / "a.tar.gz"), "--port", "8787"])
