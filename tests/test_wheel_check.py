"""scripts/wheel_check.py: the wheel must carry every runtime file of src/tow, found by itself."""

from __future__ import annotations

import importlib.util
import zipfile
from pathlib import Path

SCRIPT = Path(__file__).parents[1] / "scripts" / "wheel_check.py"
SRC = Path(__file__).parents[1] / "src" / "tow"


def _load():
    spec = importlib.util.spec_from_file_location("tow_wheel_check_under_test", SCRIPT)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _wheel(path: Path, names: list[str]) -> Path:
    with zipfile.ZipFile(path, "w") as archive:
        for name in names:
            archive.writestr(name, "")
    return path


def test_every_package_and_asset_is_required_without_a_list():
    expected = _load().expected_files(SRC)
    for name in (
        "tow/undo/engine.py",
        "tow/trackers/presets/rutor.py",
        "tow/static/favicon.svg",
        "tow/templates/base.html",
        "tow/locales/ru.json",
        "tow/platform/locks.py",
    ):
        assert name in expected
    assert not any("__pycache__" in name or name.endswith(".pyc") for name in expected)


def test_a_file_left_out_of_the_wheel_is_named(tmp_path):
    check = _load()
    src = tmp_path / "tow"
    for rel in ("__init__.py", "templates/base.html", "static/app.js", "locales/en.json", "newpkg/data.txt"):
        (src / rel).parent.mkdir(parents=True, exist_ok=True)
        (src / rel).write_text("x", encoding="utf-8")
    (src / "__pycache__").mkdir()
    (src / "__pycache__" / "x.cpython-314.pyc").write_bytes(b"")
    full = check.expected_files(src)

    assert check.missing_files(_wheel(tmp_path / "ok.whl", full), src) == []
    partial = _wheel(tmp_path / "bad.whl", [name for name in full if "newpkg" not in name])
    assert check.missing_files(partial, src) == ["tow/newpkg/data.txt"]
    assert check.main([str(partial), str(src)]) == 1


def test_a_wrong_source_folder_does_not_pass_empty(tmp_path):
    check = _load()
    (tmp_path / "empty").mkdir()
    missing = check.missing_files(_wheel(tmp_path / "w.whl", []), tmp_path / "empty")
    assert "tow/__init__.py (not even in the source tree)" in missing
