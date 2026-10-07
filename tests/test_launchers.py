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
        scripts = root / "app" / ".venv" / "Scripts"
        scripts.mkdir(parents=True)
        (scripts / "python.exe").write_bytes(b"not a program: its base Python stayed in the old folder")
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
    assert '[ "$(basename -- "$TOW_APP")" = app ]' in text
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
