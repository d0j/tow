from pathlib import Path

import pytest

ROOT = Path(__file__).parents[1]


def test_windows_launchers_use_shared_runtime_env_helper():
    helper = (ROOT / "scripts" / "tow-env.cmd").read_text(encoding="utf-8")
    assert 'set "TOW_EXE=%TOW_APP%\\.venv\\Scripts\\tow.exe"' in helper
    # The key and the access key file are found by TOW itself (tow.store, tow.auth), not here.
    assert "TOW_MASTER_KEY_FILE" not in helper
    assert "TOW_LAN_AUTH_TOKEN_FILE" not in helper
    assert "TOW_ROOT" in helper
    assert 'set "TOW_HOME=%TOW_ROOT%\\data"' in helper
    assert "%LOCALAPPDATA%" not in helper
    assert "%APPDATA%" not in helper
    assert "TOW_MASTER_KEY=" not in helper
    # 1.21: app\tow-local.cmd of installs before 1.18 is no longer read (the folder is found).
    assert "tow-local.cmd" not in helper


def test_windows_launcher_finds_the_install_and_keeps_uv_and_python_inside():
    helper = (ROOT / "scripts" / "tow-env.cmd").read_text(encoding="utf-8")
    # The install is the parent of app\ when config.yaml or data\ is there; else the checkout.
    assert 'if exist "%TOW_PARENT%\\config.yaml" set "TOW_ROOT=%TOW_PARENT%"' in helper
    assert 'if exist "%TOW_PARENT%\\data\\" set "TOW_ROOT=%TOW_PARENT%"' in helper
    runtime_only = 'if /i not "%TOW_ROOT%"=="%TOW_APP%" set '
    for line in (
        'UV_PYTHON_INSTALL_DIR=%TOW_ROOT%\\runtime\\python"',
        'UV_PYTHON_BIN_DIR=%TOW_ROOT%\\runtime\\bin"',
        'UV_CACHE_DIR=%TOW_ROOT%\\runtime\\cache"',
        'UV_PROJECT_ENVIRONMENT=%TOW_APP%\\.venv"',
        'UV_MANAGED_PYTHON=1"',
    ):
        assert f'{runtime_only}"{line}' in helper
    assert 'if exist "%TOW_ROOT%\\runtime\\bin\\uv.exe" set "TOW_UV=%TOW_ROOT%\\runtime\\bin\\uv.exe"' in helper
    # cmd.exe misreads labels in LF files: none here.
    assert not any(line.startswith(":") for line in helper.splitlines())


def test_launcher_output_keeps_cyrillic_readable():
    # Redirected output would otherwise use the ANSI code page and mangle Russian text.
    helper = (ROOT / "scripts" / "tow-env.cmd").read_text(encoding="utf-8")
    assert 'set "PYTHONIOENCODING=utf-8"' in helper


def test_the_five_task_launchers_are_gone():
    # 1.21: the live install runs `tow run` since 02.10.2026; the pre-1.18 task layout is removed.
    for name in ("tow-check.cmd", "tow-serve.cmd", "tow-watchdog.cmd", "tow-backup.cmd", "tow-progress.cmd"):
        assert not (ROOT / "scripts" / name).exists()
    for name in ("tow-restart.py", "restore-snapshot.ps1", "tow-local.example.cmd"):
        assert not (ROOT / "scripts" / name).exists()
    assert not (ROOT / "src" / "tow" / "windows_task.py").exists()


def test_generic_cli_launcher_uses_runtime_env():
    text = (ROOT / "scripts" / "tow.cmd").read_text(encoding="utf-8")
    assert 'call "%~dp0tow-env.cmd"' in text
    last = text.strip().splitlines()[-1]
    # One last line decides, so TOW's exit code is the launcher's (`exit /b` in a block loses it):
    # setup, else the environment's tow.exe, else (not set up yet) uv from the locked environment.
    assert last.startswith('if /i "%~1"=="setup" (call "%~dp0tow-setup.cmd" %*)')
    assert last.index('if exist "%TOW_EXE%" ("%TOW_EXE%" %*)') < last.index('"%TOW_UV%" run --frozen --no-dev')
    exits = [line for line in text.splitlines() if "exit /b" in line and not line.startswith("rem ")]
    # The refusals, before TOW runs: a moved folder, an update cut off while it replaced the code.
    assert exits == ["if defined TOW_MOVED exit /b 3", "if defined TOW_CUT exit /b 3"]


@pytest.mark.allow_system  # the launcher in a temp install whose environment was left behind by a move
def test_a_moved_install_says_to_run_setup_instead_of_a_trampoline_error(tmp_path):
    # QA: after moving the folder `tow.cmd status` printed "uv trampoline failed to canonicalize
    # script path" - tow-start.cmd knew the case, tow.cmd did not.
    import os
    import shutil
    import subprocess

    root = tmp_path / "TOW"
    shutil.copytree(ROOT / "scripts", root / "app" / "scripts")
    (root / "data").mkdir()
    env = {key: value for key, value in os.environ.items() if not key.startswith(("TOW_", "UV_"))}
    if os.name == "nt":
        # What a moved install really has: the environment's own launcher, whose base Python
        # (pyvenv.cfg "home") stayed in the old folder. A made-up python.exe is not a program,
        # and Windows could hold it for minutes - the test then hung instead of tow.cmd.
        import sys

        venv = root / "app" / ".venv"
        scripts = venv / "Scripts"
        scripts.mkdir(parents=True)
        shutil.copy2(Path(sys.prefix) / "Scripts" / "python.exe", scripts / "python.exe")
        (venv / "pyvenv.cfg").write_text(f"home = {tmp_path / 'old' / 'runtime' / 'python'}\n", encoding="utf-8")
        (scripts / "tow.exe").write_bytes(b"")
        argv = ["cmd.exe", "/d", "/c", str(root / "app" / "scripts" / "tow.cmd"), "status"]
    else:
        scripts = root / "app" / ".venv" / "bin"
        scripts.mkdir(parents=True)
        (scripts / "python").symlink_to(tmp_path / "old" / "runtime" / "python" / "bin" / "python3")
        (scripts / "tow").write_text("#!/bin/sh\necho ran\n", encoding="utf-8")
        (scripts / "tow").chmod(0o755)
        argv = ["/bin/sh", str(root / "app" / "scripts" / "tow"), "status"]
    done = subprocess.run(argv, capture_output=True, text=True, env=env, timeout=60, check=False)
    assert done.returncode == 3, done.stdout + done.stderr
    assert "moved or copied" in done.stdout + done.stderr
    assert " setup first" in done.stdout + done.stderr
    assert "ran" not in done.stdout


def _here_check(launcher: str) -> str:
    import re

    text = (ROOT / "scripts" / launcher).read_text(encoding="utf-8")
    pattern = r"here_check='([^']+)'" if launcher == "tow" else r'-I -c "(import os, sys, importlib\.util[^"]+)" >nul'
    matched = re.search(pattern, text)
    assert matched is not None
    return matched[1]


@pytest.mark.parametrize("launcher", ["tow", "tow.cmd"])
@pytest.mark.parametrize(
    ("case", "expected"),
    [
        ("checkout", 0),  # a development checkout: its own code, the developer's Python
        ("install", 0),  # an install: code and base Python inside the folder
        ("copy", 1),  # a copy whose environment runs the original's code
        ("python_outside", 1),  # an install whose base Python is another folder's
        ("not_installed", 1),  # no tow in the environment at all
    ],
)
def test_the_launcher_runs_only_an_environment_of_its_own_folder(monkeypatch, tmp_path, launcher, case, expected):
    # Audit 08.10.2026: a copied install whose original was still there ran the original's code
    # from tow.cmd / scripts/tow (only the start files checked whose environment it is).
    import importlib.util
    import runpy
    import sys

    code = ROOT  # where the imported tow lives (src/tow of this checkout)
    # Another folder's Python: a sibling of the install, never inside it (CI keeps its temp folder
    # next to the checkout, inside the "install" these cases pretend code.parent is).
    elsewhere = code.parent.parent / f"{code.parent.name}-elsewhere" / "python"
    here, app, base = {
        "checkout": (code, code, elsewhere),
        "install": (code.parent, code, code.parent / "runtime" / "python"),
        "copy": (tmp_path / "TOW", tmp_path / "TOW" / "app", tmp_path / "TOW" / "runtime" / "python"),
        "python_outside": (code.parent, code, elsewhere),
        "not_installed": (code, code, elsewhere),
    }[case]
    monkeypatch.setenv("TOW_HERE", str(here))
    monkeypatch.setenv("TOW_APP", str(app))
    monkeypatch.setattr(sys, "base_prefix", str(base))
    if case == "not_installed":
        monkeypatch.setattr(importlib.util, "find_spec", lambda _name: None)
    probe = tmp_path / "launcher-predicate.py"
    probe.write_text(_here_check(launcher), encoding="utf-8")
    with pytest.raises(SystemExit) as exited:
        runpy.run_path(str(probe))  # the launcher's actual predicate
    assert exited.value.code == expected


def test_both_launchers_ask_the_same_question():
    windows, posix = _here_check("tow.cmd"), _here_check("tow")
    assert windows.replace("'", '"') == posix
    assert "TOW_HERE" in (ROOT / "scripts" / "tow.cmd").read_text(encoding="utf-8")  # never the path in the code


@pytest.mark.allow_system  # the launcher in a temp copy of an install whose original is still there
def test_a_copied_install_says_to_run_setup_instead_of_running_the_originals_code(tmp_path):
    import os
    import shutil
    import subprocess
    import sys

    root = tmp_path / "Copy of TOW"
    shutil.copytree(ROOT / "scripts", root / "app" / "scripts")
    (root / "data").mkdir()
    env = {key: value for key, value in os.environ.items() if not key.startswith(("TOW_", "UV_"))}
    venv = root / "app" / ".venv"
    if os.name == "nt":
        # A real environment launcher whose Python runs (the original is still there) and whose
        # tow is the original's code: what a copy's .venv holds.
        scripts = venv / "Scripts"
        scripts.mkdir(parents=True)
        shutil.copy2(Path(sys.prefix) / "Scripts" / "python.exe", scripts / "python.exe")
        shutil.copy2(Path(sys.prefix) / "pyvenv.cfg", venv / "pyvenv.cfg")
        site = venv / "Lib" / "site-packages"
        site.mkdir(parents=True)
        (site / "_tow.pth").write_text(str(ROOT / "src") + "\n", encoding="utf-8")
        (scripts / "tow.exe").write_bytes(b"")
        argv = ["cmd.exe", "/d", "/c", str(root / "app" / "scripts" / "tow.cmd"), "status"]
    else:
        scripts = venv / "bin"
        scripts.mkdir(parents=True)
        # This test's own interpreter: it runs, and its tow is this checkout's - another folder.
        (scripts / "python").write_text(f'#!/bin/sh\nexec "{sys.executable}" "$@"\n', encoding="utf-8")
        (scripts / "python").chmod(0o755)
        (scripts / "tow").write_text("#!/bin/sh\necho ran\n", encoding="utf-8")
        (scripts / "tow").chmod(0o755)
        argv = ["/bin/sh", str(root / "app" / "scripts" / "tow"), "status"]
    done = subprocess.run(argv, capture_output=True, text=True, env=env, timeout=60, check=False)
    assert done.returncode == 3, done.stdout + done.stderr
    assert "moved or copied" in done.stdout + done.stderr
    assert " setup first" in done.stdout + done.stderr
    assert "ran" not in done.stdout


@pytest.mark.allow_system  # the launcher's environment in a temp install
@pytest.mark.parametrize(
    ("given", "ignored"),
    [(None, False), ("old", True), ("root/", False), ("app", False), ("missing", True)],
)
def test_a_tow_root_of_another_folder_is_ignored_by_the_launchers(tmp_path, given, ignored):
    # Audit 08.10.2026: TOW_ROOT=C:\TOW left for the whole account after a move to D:\TOW was
    # kept by the launchers: data\ and a new master key in the old place, TOW started empty.
    import os
    import shutil
    import subprocess

    root = tmp_path / "TOW"
    shutil.copytree(ROOT / "scripts", root / "app" / "scripts")
    (root / "data").mkdir()
    (tmp_path / "old" / "app").mkdir(parents=True)  # the old folder may still be there
    env = {key: value for key, value in os.environ.items() if not key.startswith(("TOW_", "UV_"))}
    if given is not None:
        env["TOW_ROOT"] = {
            "old": str(tmp_path / "old"),
            "root/": str(root) + os.sep,
            "app": str(root / "app"),
            "missing": str(tmp_path / "gone"),
        }[given]
    if os.name == "nt":
        probe = tmp_path / "probe.cmd"
        probe.write_text(
            f'@echo off\r\ncall "{root / "app" / "scripts" / "tow-env.cmd"}"\r\necho root=%TOW_ROOT%\r\n'
            "echo home=%TOW_HOME%\r\n",
            encoding="utf-8",
        )
        argv = ["cmd.exe", "/d", "/c", str(probe)]
    else:
        bin_dir = root / "app" / ".venv" / "bin"
        bin_dir.mkdir(parents=True)
        (bin_dir / "python").write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")  # "this folder's own"
        (bin_dir / "tow").write_text('#!/bin/sh\necho "root=$TOW_ROOT"\n', encoding="utf-8")
        for item in bin_dir.iterdir():
            item.chmod(0o755)
        argv = ["/bin/sh", str(root / "app" / "scripts" / "tow"), "status"]
    done = subprocess.run(argv, capture_output=True, text=True, env=env, timeout=60, check=False)
    assert done.returncode == 0, done.stdout + done.stderr
    lines = done.stdout.splitlines()
    assert f"root={root.resolve()}" in lines or f"root={root}" in lines, done.stdout
    if os.name == "nt":
        assert f"home={root}\\data" in lines
    assert ("is ignored" in done.stderr) is ignored


@pytest.mark.allow_system  # sh on a temp install whose code folder is named in another case
@pytest.mark.parametrize("name", ["app", "App", "APP"])
def test_the_posix_launcher_finds_an_install_whose_code_folder_is_app_in_any_case(tmp_path, name):
    # Audit 08.10.2026: scripts/tow compared the name exactly while tow.paths ignores the case:
    # an install in "App" got the code folder as its root (data/ and keys/ inside App/).
    import os
    import shutil
    import subprocess

    if os.name == "nt":
        pytest.skip("scripts/tow is the Linux and macOS launcher (tow-env.cmd compares with /i)")
    root = tmp_path / "TOW"
    shutil.copytree(ROOT / "scripts", root / name / "scripts")
    (root / "data").mkdir()
    bin_dir = root / name / ".venv" / "bin"
    bin_dir.mkdir(parents=True)
    (bin_dir / "python").write_text("#!/bin/sh\nexit 0\n", encoding="utf-8")
    (bin_dir / "tow").write_text('#!/bin/sh\necho "root=$TOW_ROOT"\n', encoding="utf-8")
    for item in bin_dir.iterdir():
        item.chmod(0o755)
    env = {key: value for key, value in os.environ.items() if not key.startswith(("TOW_", "UV_"))}
    argv = ["/bin/sh", str(root / name / "scripts" / "tow"), "status"]
    done = subprocess.run(argv, capture_output=True, text=True, env=env, timeout=60, check=False)
    assert done.returncode == 0, done.stdout + done.stderr
    assert f"root={root.resolve()}" in done.stdout.splitlines()


def test_setup_installs_python_and_the_environment_inside_the_install():
    for name in ("tow-setup.cmd", "tow"):
        text = (ROOT / "scripts" / name).read_text(encoding="utf-8")
        assert "python install --no-bin" in text  # no copies of Python on the user's PATH
        assert "sync --frozen --no-dev" in text
        # A venv whose Python is gone (moved) or outside the install (pre-1.18) is rebuilt.
        assert 'os.environ["UV_PYTHON_INSTALL_DIR"]' in text or "os.environ['UV_PYTHON_INSTALL_DIR']" in text
    windows = (ROOT / "scripts" / "tow-setup.cmd").read_text(encoding="utf-8")
    assert "--no-registry" in windows
    # Renamed before removal: a running TOW holds its files, and then nothing is removed.
    assert windows.index('move ".venv" ".venv-old"') < windows.index('rmdir /s /q ".venv-old"')
    assert "rm -rf .venv" in (ROOT / "scripts" / "tow").read_text(encoding="utf-8")


def test_posix_launcher_mirrors_the_windows_one():
    text = (ROOT / "scripts" / "tow").read_text(encoding="utf-8")
    assert text.startswith("#!/bin/sh\n")
    assert 'case $(basename -- "$TOW_APP") in\n        [Aa][Pp][Pp])' in text  # any case, like tow.paths
    assert '[ -f "$parent/config.yaml" ] || [ -d "$parent/data" ]' in text
    for line in (
        'export UV_PYTHON_INSTALL_DIR="$TOW_ROOT/runtime/python"',
        'export UV_CACHE_DIR="$TOW_ROOT/runtime/cache"',
        'export UV_PROJECT_ENVIRONMENT="$TOW_APP/.venv"',
        "export UV_MANAGED_PYTHON=1",
        'exec "$TOW_APP/.venv/bin/tow" "$@"',
        'exec "$uv" run --frozen --no-dev --project "$TOW_APP" tow "$@"',
    ):
        assert line in text
    assert "\r" not in text


def test_config_template_is_versioned_and_loopback_only():
    import yaml

    data = yaml.safe_load((ROOT / "config.example.yaml").read_text(encoding="utf-8"))
    assert data["bind"] == "127.0.0.1"
    assert data["allow_lan"] is False


def test_deploy_wraps_update_and_keeps_credentials_out_of_snapshots():
    # Since 1.18 deploy.ps1 is a wrapper; the contract lives in scripts/update.py (and its tests).
    import importlib.util

    deploy = (ROOT / "scripts" / "deploy.ps1").read_text(encoding="utf-8")
    assert "update.py" in deploy
    for parameter in ("$Ref", "$HealthTimeoutSec", "$CheckWaitMinutes", "$KeepSnapshots"):
        assert parameter in deploy
    spec = importlib.util.spec_from_file_location("tow_update_contract", ROOT / "scripts" / "update.py")
    assert spec is not None
    assert spec.loader is not None
    update = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(update)
    assert {"lan-auth.token", "master.key", "sessions.json"} <= update.SKIP_FILES
    assert {"browser-auth", "keys"} <= update.SKIP_DIRS


def test_deploy_reads_the_updates_utf8_and_gives_the_console_its_code_page_back():
    # 08.10.2026: deploy.ps1 written to a file from a fresh console said "TOWтАж" and garbled Russian.
    deploy = (ROOT / "scripts" / "deploy.ps1").read_text(encoding="utf-8")
    run = deploy.index("update.py') --ref")
    assert deploy.index("[Console]::OutputEncoding = [Text.UTF8Encoding]::new($false)") < run
    assert run < deploy.index("finally") < deploy.index("[Console]::OutputEncoding = $encoding")
    assert "exit $code" in deploy


def test_deploy_runs_in_windows_powershell_5_1_and_in_any_folder():
    # A git install set up in Windows PowerShell 5.1 was told to run deploy.ps1, which demanded
    # PowerShell 7; Test-Path without -LiteralPath read a folder like "TOW [old]" as a wildcard.
    import re as regex

    raw = (ROOT / "scripts" / "deploy.ps1").read_bytes()
    assert raw.isascii()  # 5.1 reads a file without a BOM in the ANSI code page
    deploy = raw.decode("ascii")
    assert "#Requires" not in deploy
    code = regex.sub(r"(?s)<#.*?#>", "", deploy)
    code = "\n".join(line.split("#", 1)[0] for line in code.splitlines())
    for seven_only in ("??", "&&", "||", "?."):
        assert seven_only not in code
    assert regex.search(r"\?\s*\S+\s*:", code.replace("::", "")) is None  # no ternary
    for cmdlet in ("Test-Path", "Get-Content"):
        uses = regex.findall(rf"{cmdlet}\s+(-\w+)", code)
        assert uses
        assert set(uses) == {"-LiteralPath"}
    assert "-Encoding UTF8" in code  # pyvenv.cfg is UTF-8; 5.1 would read it as ANSI


def test_setup_takes_no_options_and_never_runs_under_a_running_tow():
    # 02.10.2026: `tow.cmd setup --help` rebuilt the live install's environment under a running TOW.
    root = Path(__file__).resolve().parents[1] / "scripts"
    windows = (root / "tow-setup.cmd").read_text(encoding="utf-8")
    posix = (root / "tow").read_text(encoding="utf-8")
    assert 'call "%~dp0tow-setup.cmd" %*' in (root / "tow.cmd").read_text(encoding="utf-8")
    assert "shift /1" in windows  # a plain shift moves %0 too, and %~dp0 stops naming scripts\
    for text in (windows, posix):
        assert "Usage:" in text
        assert "l.setup_check()" in text
        assert "else 4 if l.busy() else 0" in text  # an older .venv still refuses
        assert "TOW is running from this folder. Stop it first" in text
    # the check comes before anything is moved or removed
    assert windows.index("setup_check") < windows.index('move ".venv" ".venv-old"')
    assert posix.index("setup_check") < posix.index("rm -rf .venv")


def _python_folders(root: Path) -> Path:
    python = root / "runtime" / "python"
    for name in ("cpython-3.14.2-windows-x86_64-none", "cpython-3.13-windows-x86_64-none", "other-3.14-x"):
        (python / name / "keep").mkdir(parents=True)
    (root / "outside" / "cpython-3.12-windows-x86_64-none").mkdir(parents=True)
    return python


def test_setup_removes_a_copied_minor_version_link_before_uv_installs_python():
    # QA 2.10.2026: after copying an install (PORTABLE.md 3) `tow setup` failed: uv's link
    # runtime\python\cpython-3.14-* had been copied as a real folder (os error 145).
    windows = (ROOT / "scripts" / "tow-setup.cmd").read_text(encoding="utf-8")
    posix = (ROOT / "scripts" / "tow").read_text(encoding="utf-8")
    assert windows.index('findstr /r /i "^d[^l]*cpython-[0-9]*\\.[0-9]*-"') < windows.index('" python install')
    assert '"%TOW_ROOT%\\runtime\\python\\cpython-*"' in windows  # only inside the install
    assert posix.index('"$TOW_ROOT"/runtime/python/cpython-*') < posix.index('"$uv" python install')


@pytest.mark.allow_system  # cmd.exe / sh on a temp layout: the script's own lines
def test_the_copied_link_folder_is_removed_and_nothing_else(tmp_path):
    import os
    import shutil
    import subprocess

    python = _python_folders(tmp_path)
    if os.name == "nt":
        target = python / "cpython-3.14.2-windows-x86_64-none"
        link = python / "cpython-3.14-windows-x86_64-none"
        subprocess.run(["cmd.exe", "/d", "/c", "mklink", "/J", str(link), str(target)], check=True, capture_output=True)
        line = next(
            row
            for row in (ROOT / "scripts" / "tow-setup.cmd").read_text(encoding="utf-8").splitlines()
            if row.startswith("for /d %%D in") and "findstr" in row
        )
        script = tmp_path / "clean.cmd"
        script.write_text(f"@echo off\r\n{line}\r\nexit /b 0\r\n", encoding="ascii")
        env = {**os.environ, "TOW_ROOT": str(tmp_path)}
        subprocess.run(["cmd.exe", "/d", "/c", str(script)], check=True, env=env, capture_output=True, timeout=60)
        assert link.is_junction()  # a real link stays
        assert (link / "keep").is_dir()
    else:
        text = (ROOT / "scripts" / "tow").read_text(encoding="utf-8")
        start = text.index('    for entry in "$TOW_ROOT"/runtime/python/cpython-*; do')
        block = text[start : text.index("    done\n", start) + len("    done\n")]
        (python / "cpython-3.14-linux-x86_64-gnu").symlink_to(python / "cpython-3.14.2-windows-x86_64-none")
        sh = shutil.which("sh") or "/bin/sh"
        subprocess.run([sh, "-euc", block], check=True, env={**os.environ, "TOW_ROOT": str(tmp_path)}, timeout=60)
        assert (python / "cpython-3.14-linux-x86_64-gnu").is_symlink()
    assert not (python / "cpython-3.13-windows-x86_64-none").exists()  # the copied link: removed
    assert (python / "cpython-3.14.2-windows-x86_64-none" / "keep").is_dir()  # a full version stays
    assert (python / "other-3.14-x" / "keep").is_dir()
    assert (tmp_path / "outside" / "cpython-3.12-windows-x86_64-none").is_dir()


def test_windows_setup_does_not_treat_a_broken_moved_python_as_a_running_service():
    windows = (ROOT / "scripts" / "tow-setup.cmd").read_text(encoding="utf-8")
    # cmd's `if errorlevel 4` also matches 103: a relocated venv cannot find its old base.
    assert 'if "%ERRORLEVEL%"=="4"' in windows
    assert not any("if errorlevel 4" in line for line in windows.splitlines() if not line.startswith("rem "))


@pytest.mark.parametrize("launcher", ["tow", "tow-setup.cmd"])
@pytest.mark.parametrize("location", ["managed", "child", "sibling", "outside"])
def test_setup_requires_python_inside_the_managed_directory(monkeypatch, tmp_path, launcher, location):
    import re
    import runpy
    import sys

    text = (ROOT / "scripts" / launcher).read_text(encoding="utf-8")
    pattern = r"inside='([^\n]+)'" if launcher == "tow" else r'-c "(import os,sys; base=[^\n]+?)" >nul'
    matched = re.search(pattern, text)
    assert matched is not None
    managed = tmp_path / "runtime" / "python"
    locations = {
        "managed": managed,
        "child": managed / "cpython-test",
        "sibling": managed.with_name("python-foreign"),
        "outside": tmp_path / "external-python",
    }
    monkeypatch.setenv("UV_PYTHON_INSTALL_DIR", str(managed))
    monkeypatch.setattr(sys, "base_prefix", str(locations[location]))
    probe = tmp_path / "launcher-predicate.py"
    probe.write_text(matched[1], encoding="utf-8")
    with pytest.raises(SystemExit) as exited:
        runpy.run_path(str(probe))  # the launcher's actual predicate, not a second implementation
    assert exited.value.code == (location not in {"managed", "child"})


def test_release_smokes_relocate_an_existing_environment():
    workflow = (ROOT / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8")
    assert "-Offline -Move" in workflow
    windows = (ROOT / "scripts" / "bundle-smoke.ps1").read_text(encoding="utf-8")
    posix = (ROOT / "scripts" / "install-smoke.sh").read_text(encoding="utf-8")
    assert "Move-Item -LiteralPath $root -Destination $movedRoot" in windows
    assert 'UV_OFFLINE=1 UV_PYTHON_DOWNLOADS=never TOW_NO_BROWSER=1 "$start"' in posix
    for text in windows, posix:
        assert "sys.base_prefix" in text
        assert "tow.__file__" in text


@pytest.mark.allow_git
def test_posix_launcher_and_hooks_are_executable_in_git():
    # 02.10.2026: the first public commit lost the bits (built from an archive on Windows).
    import shutil
    import subprocess

    root = Path(__file__).resolve().parents[1]
    if shutil.which("git") is None or not (root / ".git").exists():
        pytest.skip("not a git checkout")
    wanted = [
        "scripts/tow",
        "scripts/tow-start",
        "install/install.sh",
        ".githooks/pre-commit",
        ".githooks/pre-push",
        ".githooks/post-commit",
    ]
    listing = subprocess.run(
        ["git", "-C", str(root), "ls-files", "-s", "--", *wanted], capture_output=True, text=True, check=True
    ).stdout.splitlines()
    modes = {line.split("\t", 1)[1]: line.split()[0] for line in listing}
    assert modes == dict.fromkeys(wanted, "100755")
