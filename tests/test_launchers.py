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
    assert not any("exit /b" in line for line in text.splitlines() if not line.startswith("rem "))


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
        assert "from tow.supervisor.layout import busy" in text
        assert "TOW is running. Stop it first" in text
    # the check comes before anything is moved or removed
    assert windows.index("import busy") < windows.index('move ".venv" ".venv-old"')
    assert posix.index("import busy") < posix.index("rm -rf .venv")


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
