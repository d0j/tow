"""scripts/build-bundle.py: what can be checked without the internet - the start files, the
README, the code it takes, the cache it keeps and the check of a finished zip. The real build
and a double-click of "Start TOW.cmd" run in the release workflow (scripts/bundle-smoke.ps1)."""

from __future__ import annotations

import importlib.util
import io
import os
import re
import sys
import tarfile
import zipfile
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPT = ROOT / "scripts" / "build-bundle.py"


def _load():
    spec = importlib.util.spec_from_file_location("tow_build_bundle_under_test", SCRIPT)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


bundle = _load()


def _unquoted(line: str) -> str:
    """The parts of a cmd line outside double quotes."""
    return "".join(part for index, part in enumerate(line.split('"')) if index % 2 == 0)


@pytest.mark.parametrize("name", ["Start TOW.cmd", "Stop TOW.cmd", "Update TOW.cmd"])
def test_the_root_files_are_ascii_crlf_and_quote_every_path(name):
    text = bundle.ROOT_FILES[name]
    data = bundle.crlf(text).encode("ascii")  # ASCII: cmd.exe reads the file in the OEM code page
    assert b"\n" not in data.replace(b"\r\n", b"")
    for line in text.splitlines():
        if line.startswith("rem "):
            continue
        # A path with a space, "&" or ")" (C:\Program Files (x86)) breaks an unquoted use.
        assert "%~dp0" not in _unquoted(line), line
        assert "%TOW_ROOT%" not in _unquoted(line), line
        assert "(" not in _unquoted(line).replace(" in (", " in "), f"no blocks: {line}"  # a for's set is not a block
    assert text.startswith("@echo off\n")
    assert text.rstrip().splitlines()[-1].startswith("exit /b %TOW_CODE%")


def test_start_runs_the_apps_start_script_and_stop_the_launcher():
    # The logic lives in app\scripts (it updates with the code); the root files only call it.
    assert 'call "%~dp0app\\scripts\\tow-start.cmd" %*' in bundle.START_CMD
    assert 'call "%~dp0app\\scripts\\tow.cmd" stop' in bundle.STOP_CMD
    assert '"%TOW_UPDATE%" --ref "%TOW_REF%"' in bundle.UPDATE_CMD
    assert "runtime\\update.py" in bundle.UPDATE_CMD
    assert (ROOT / "scripts" / "tow-start.cmd").is_file()


@pytest.mark.allow_system  # cmd.exe on a temp layout: the file's own lines, Python replaced by echo
@pytest.mark.skipif(sys.platform != "win32", reason="cmd.exe")
def test_update_takes_the_environments_python_else_the_newest_real_one(tmp_path):
    # It took the alphabetically last cpython-3* folder: 3.14.8 after 3.14.10, or uv's junction.
    import subprocess

    root = tmp_path / "my TOW (1) & co"
    python = root / "runtime" / "python"
    for name in (
        "cpython-3.14.8-windows-x86_64-none",
        "cpython-3.14.10-windows-x86_64-none",
        "cpython-3.9.20-windows-x86_64-none",
        "cpython-3.15.0a1-windows-x86_64-none",
        "cpython-3.14.11+freethreaded-windows-x86_64-none",
    ):
        (python / name).mkdir(parents=True)
        (python / name / "python.exe").write_bytes(b"MZ")
    link = python / "cpython-3.14.99-windows-x86_64-none"
    target = python / "cpython-3.14.8-windows-x86_64-none"
    subprocess.run(["cmd.exe", "/d", "/c", "mklink", "/J", str(link), str(target)], check=True, capture_output=True)
    lines = bundle.UPDATE_CMD.replace('"%TOW_PY%" "%TOW_UPDATE%" --ref "%TOW_REF%"', 'echo picked:"%TOW_PY%"')
    script = root / "Update TOW.cmd"
    text = "\n".join(line for line in lines.splitlines() if not line.endswith("pause"))
    script.write_bytes(bundle.crlf(text + "\n").encode("ascii"))

    def picked() -> str:
        done = subprocess.run(f'cmd.exe /d /s /c ""{script}""', capture_output=True, timeout=60, check=False)
        found = re.search(rb'picked:"[^"]*\\([^"\\]+)\\python\.exe"', done.stdout)
        return found.group(1).decode("ascii") if found else done.stdout.decode("ascii", "replace")

    assert picked() == "cpython-3.14.10-windows-x86_64-none"
    cfg = root / "app" / ".venv" / "pyvenv.cfg"
    cfg.parent.mkdir(parents=True)
    # Made in another place (moved since): found by its folder name in this runtime\python.
    cfg.write_text(
        "home = D:\\old place\\cpython-3.14.8-windows-x86_64-none\nversion_info = 3.14.8\n", encoding="utf-8"
    )
    assert picked() == "cpython-3.14.8-windows-x86_64-none"
    cfg.write_text("home = D:\\old\\cpython-3.13.1-windows-x86_64-none\n", encoding="utf-8")
    assert picked() == "cpython-3.14.10-windows-x86_64-none"


@pytest.mark.allow_system  # cmd.exe runs a v1.22.0 "Update TOW.cmd" whose update.py writes the new one
@pytest.mark.skipif(sys.platform != "win32", reason="cmd.exe")
def test_an_update_file_written_again_while_it_runs_ends_cleanly(tmp_path):
    # cmd.exe reads a running batch file again after each command, from the byte it stopped at:
    # the new file holds, right there, the line that ends the old run with the update's exit code.
    # Three runs in a row: the v1.22.0 file, the file written then, and the zip's own file. Each
    # runs update.py once (a real recovery once ran it twice: the written file's own mark let the
    # old run go on into the body of the next one).
    import subprocess

    from test_update_archive import UPDATE_CMD_1_22_0

    root = tmp_path / "my TOW (1) & co"
    python = root / "runtime" / "python"
    python.mkdir(parents=True)
    base = Path(sys.base_prefix)
    # the v1.22.0 file takes any runtime\python\cpython-3*; later ones the folder pyvenv.cfg names
    links = [python / "cpython-3.14.0-windows-x86_64-none", python / base.name]
    for link in dict.fromkeys(links):
        subprocess.run(["cmd.exe", "/d", "/c", "mklink", "/J", str(link), str(base)], check=True, capture_output=True)
    try:
        (root / "app" / ".venv").mkdir(parents=True)
        (root / "app" / ".venv" / "pyvenv.cfg").write_text(f"home = {base}\n", encoding="utf-8")
        scripts = root / "app" / "scripts"
        scripts.mkdir(parents=True)
        runs = tmp_path / "runs.txt"
        (scripts / "update.py").write_text(
            "import importlib.util, sys\n"
            "from pathlib import Path\n"
            f"with open({str(runs)!r}, 'a') as handle: handle.write('run\\n')\n"
            f"spec = importlib.util.spec_from_file_location('rf',{str(ROOT / 'scripts' / 'root_files.py')!r})\n"
            "module = importlib.util.module_from_spec(spec)\n"
            "spec.loader.exec_module(module)\n"
            f"print(module.refresh(Path({str(root)!r})))\n"
            "sys.exit(7)\n",
            encoding="utf-8",
        )
        script = root / "Update TOW.cmd"
        module = bundle._ROOT
        # the zip of an earlier version (other wording): the same form as the one built now
        zip_file = module.rendered()["Update TOW.cmd"].replace(b"rem It stops TOW", b"rem It halts TOW")
        written = module.update_file(module.resume_offset(UPDATE_CMD_1_22_0))
        for count, (before, after) in enumerate(
            [
                (UPDATE_CMD_1_22_0, written),
                (written, module.update_file(module.resume_offset(written))),
                (zip_file, module.update_file(module.resume_offset(zip_file))),
            ],
            start=1,
        ):
            script.write_bytes(before)

            done = subprocess.run(
                f'cmd.exe /d /s /c ""{script}" v1.29.0"',
                capture_output=True,
                stdin=subprocess.DEVNULL,
                timeout=120,
                check=False,
            )

            output = done.stdout + done.stderr
            assert b"'Update TOW.cmd'], [])" in output.replace(b'"', b"'"), output  # it was written again
            assert done.returncode == 7, output  # the update's exit code, through the new file's line
            assert b"not recognized" not in output, output
            assert b"Press any key" in output  # the pause of the old file, from the new one
            assert script.read_bytes() == after
            assert runs.read_text(encoding="utf-8").count("run") == count, output  # once per run
            if count > 1:
                assert len(after) == len(before)  # aligned on the old file: it does not grow
    finally:
        for link in dict.fromkeys(links):
            if link.exists():
                os.rmdir(link)  # the junction only, never the Python it points to


def test_start_files_written_again_settle_and_stay_small(tmp_path):
    # A real update wrote "Update TOW.cmd" again at every run, with a longer padding line each time.
    from test_update_archive import UPDATE_CMD_1_22_0

    module = bundle._ROOT
    (tmp_path / "Update TOW.cmd").write_bytes(UPDATE_CMD_1_22_0)
    assert module.refresh(tmp_path) == (["Start TOW.cmd", "Stop TOW.cmd", "Update TOW.cmd"], [])
    first = (tmp_path / "Update TOW.cmd").read_bytes()
    assert module.refresh(tmp_path) == (["Update TOW.cmd"], [])  # the guard line goes, the size stays
    second = (tmp_path / "Update TOW.cmd").read_bytes()
    assert len(second) == len(first)
    for _ in range(3):
        assert module.refresh(tmp_path) == ([], [])  # the same version writes nothing
    assert (tmp_path / "Update TOW.cmd").read_bytes() == second
    assert max(len(line) for line in second.splitlines()) <= 1000
    padded = module.update_file(50_000)
    assert padded is not None
    assert max(len(line) for line in padded.splitlines(keepends=True)) <= 1000
    assert module.resume_offset(padded) == 50_000
    assert module.update_file(3) is None  # nothing fits before that byte


@pytest.mark.allow_system  # cmd.exe (and its PowerShell check) on a temp layout
@pytest.mark.skipif(sys.platform != "win32", reason="cmd.exe")
@pytest.mark.parametrize(
    ("name", "record", "scripts", "said"),
    [
        (
            "Start TOW.cmd",
            "switching",
            False,
            "TOW was not started: an update was cut off while it replaced the code in",
        ),
        (
            "Start TOW.cmd",
            "switching",
            True,
            "TOW was not started: an update was cut off while it replaced the code in",
        ),
        ("Start TOW.cmd", None, False, "its code is incomplete"),
        ("Start TOW.cmd", "accepted", True, "app script: start"),
        ("Stop TOW.cmd", "switching", False, "TOW cannot run: an update was cut off while it replaced the code in"),
        ("Stop TOW.cmd", None, False, "its code is incomplete"),
        ("Stop TOW.cmd", None, True, "app script: stop"),
    ],
)
def test_the_start_and_stop_files_say_when_an_update_was_cut_off(tmp_path, name, record, scripts, said):
    # Before: after a cut that moved app\scripts away, "The system cannot find the path specified."
    # and then "TOW did not start: the reason is above" - with no reason above.
    import json
    import subprocess

    root = tmp_path / "my TOW (1) & co"
    root.mkdir()
    (root / name).write_bytes(bundle.crlf(bundle.ROOT_FILES[name]).encode("ascii"))
    if record:
        switch = {"format": "tow-update-switch/v1", "phase": record, "old": [], "new": []}
        (root / ".update-switch.json").write_text(json.dumps(switch), encoding="utf-8")
    if scripts:
        (root / "app" / "scripts").mkdir(parents=True)
        for script, what in (("tow-start.cmd", "start"), ("tow.cmd", "%1")):
            (root / "app" / "scripts" / script).write_bytes(f"@echo app script: {what}\r\n".encode("ascii"))

    done = subprocess.run(
        f'cmd.exe /d /s /c ""{root / name}""', capture_output=True, stdin=subprocess.DEVNULL, timeout=120, check=False
    )

    output = (done.stdout + done.stderr).decode("ascii", "replace")
    assert said in output, output
    assert "cannot find the path" not in output
    assert "the reason is above" not in output
    if record == "switching":
        assert f'"{root}"' in output  # the folder, without a trailing backslash
    assert done.returncode == (0 if said.startswith("app script") else 3), output


def test_the_readme_says_how_to_start_stop_and_keep_the_key_in_both_languages():
    text = bundle.readme()
    for line in ("Start TOW.cmd", "Stop TOW.cmd", "Update TOW.cmd", "keys\\master.key", "http://127.0.0.1:8787"):
        assert text.count(line) == 2, line  # English and Russian
    # An update replaces app\ but never README.txt next to it: a version there would go stale.
    assert not re.search(r"(?<![\d.])\d+\.\d+\.\d+(?![\d.])", text)  # 127.0.0.1 is no version
    assert "https://github.com/d0j/tow/blob/main/docs/install.md" in text
    assert "https://github.com/d0j/tow/blob/main/docs/ru/install.md" in text
    assert re.search("[а-яё]", text)


def _tarball(path: Path, files: dict[str, bytes], top: str = "tow-1.22.0") -> Path:
    with tarfile.open(path, "w:gz") as tar:
        for name, data in files.items():
            info = tarfile.TarInfo(f"{top}/{name}")
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    return path


def test_the_code_is_the_source_archive_without_tests(tmp_path):
    source = _tarball(
        tmp_path / "src.tar.gz",
        {
            "pyproject.toml": b'version = "1.22.0"\n',
            "src/tow/cli.py": b"",
            "tests/test_x.py": b"",
            ".github/workflows/ci.yml": b"",
            "docs/tests-and-ci.md": b"kept: only the folders are left out",
        },
    )
    app = tmp_path / "TOW" / "app"
    bundle.stage_code(app, source)
    found = sorted(p.relative_to(app).as_posix() for p in app.rglob("*") if p.is_file())
    assert found == ["docs/tests-and-ci.md", "pyproject.toml", "src/tow/cli.py"]
    assert bundle.version_of(app) == "1.22.0"


def test_only_the_wheels_stay_in_the_cache(tmp_path):
    cache = tmp_path / "cache"
    for name in ("archive-v0", "wheels-v6", "interpreter-v4", "builds-v0", "sdists-v9", ".tmpabc", "environments-v2"):
        (cache / name).mkdir(parents=True)
        (cache / name / "x").write_text("x", encoding="utf-8")
    (cache / "CACHEDIR.TAG").write_text("tag", encoding="utf-8")
    bundle.prune_cache(cache)
    assert sorted(p.name for p in cache.iterdir()) == ["CACHEDIR.TAG", "archive-v0", "wheels-v6"]


def _stage(top: Path) -> None:
    for name in bundle.REQUIRED:
        path = top / name
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(bundle.crlf(bundle.ROOT_FILES.get(name, "x")).encode("ascii"))
    python = top / "runtime" / "python" / "cpython-3.14.7-windows-x86_64-none"
    python.mkdir(parents=True)
    (python / "python.exe").write_bytes(b"MZ")
    (top / "runtime" / "cache" / "archive-v0" / "abc").mkdir(parents=True)
    (top / "runtime" / "cache" / "archive-v0" / "abc" / "METADATA").write_text("m", encoding="utf-8")
    (top / "data-is-not-here").mkdir()  # an empty folder is kept as one


def test_a_complete_bundle_passes_the_check(tmp_path):
    top = tmp_path / "TOW"
    _stage(top)
    zip_path = tmp_path / "out" / bundle.ZIP_NAME
    bundle.write_zip(top, zip_path)
    assert bundle.check_zip(zip_path) == []
    with zipfile.ZipFile(zip_path) as archive:
        names = archive.namelist()
    assert all(name.startswith("TOW/") for name in names)
    assert "TOW/Start TOW.cmd" in names
    assert "TOW/data-is-not-here/" in names
    assert names == sorted(names)  # a stable order
    assert not (tmp_path / "out" / f"{bundle.ZIP_NAME}.part").exists()


def test_the_check_finds_what_is_missing_or_must_not_be_there(tmp_path):
    top = tmp_path / "TOW"
    _stage(top)
    (top / "runtime" / "bin" / "uv.exe").unlink()
    (top / "app" / ".venv" / "Scripts").mkdir(parents=True)
    (top / "app" / ".venv" / "Scripts" / "python.exe").write_bytes(b"MZ")
    (top / "Start TOW.cmd").write_bytes(b"@echo off\nrem LF only\n")
    zip_path = tmp_path / bundle.ZIP_NAME
    bundle.write_zip(top, zip_path)
    with zipfile.ZipFile(zip_path, "a") as archive:
        archive.writestr("stray.txt", "outside the folder")
    problems = bundle.check_zip(zip_path)
    assert "missing: runtime/bin/uv.exe" in problems
    assert "must not be in the bundle: app/.venv/Scripts/python.exe" in problems
    assert "Start TOW.cmd: not ASCII with CRLF line ends" in problems
    assert "outside TOW/: stray.txt" in problems


def test_the_bundle_takes_the_python_of_the_project():
    # The Python inside is the one .python-version names (prepare_runtime reads it there).
    assert re.fullmatch(r"3\.\d+\.\d+", (ROOT / ".python-version").read_text(encoding="utf-8").strip())
    assert bundle.UV_URL.endswith(f"/{bundle.UV_VERSION}/uv-x86_64-pc-windows-msvc.zip")


def test_python_bootstrap_uses_an_immutable_official_manifest_for_older_uv():
    import tomllib

    config = tomllib.loads((ROOT / "pyproject.toml").read_text(encoding="utf-8"))
    manifest = config["tool"]["uv"]["python-downloads-json-url"]
    assert re.fullmatch(
        r"https://raw\.githubusercontent\.com/astral-sh/uv/[a-f0-9]{40}/crates/uv-python/download-metadata\.json",
        manifest,
    )


def test_bundle_caches_its_bootstrap_manifest_despite_isolated_builder_config(tmp_path, monkeypatch):
    app = tmp_path / "app"
    app.mkdir()
    app.joinpath("pyproject.toml").write_text(
        '[tool.uv]\npython-downloads-json-url = "https://example.test/pinned.json"\n', encoding="utf-8"
    )
    monkeypatch.setenv("UV_PYTHON_DOWNLOADS_JSON_URL", "https://example.test/unrelated.json")
    env = bundle.uv_env(tmp_path, tmp_path / "venv")
    assert env["UV_NO_CONFIG"] == "1"
    assert env["UV_PYTHON_DOWNLOADS_JSON_URL"] == "https://example.test/pinned.json"


def test_bundle_rejects_a_wrongly_typed_bootstrap_manifest(tmp_path):
    app = tmp_path / "app"
    app.mkdir()
    app.joinpath("pyproject.toml").write_text("[tool.uv]\npython-downloads-json-url = 123\n", encoding="utf-8")
    with pytest.raises(ValueError, match="must be a string"):
        bundle.uv_env(tmp_path, tmp_path / "venv")
