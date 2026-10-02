"""install/install.sh (Linux, macOS) and install/install.ps1 (Windows): portable syntax, the
checks they make, and the uninstall of install.sh on a stub install. The full installs run in
the release workflow (.github/workflows/release.yml)."""

from __future__ import annotations

import importlib.util
import os
import re
import shutil
import subprocess
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SH = ROOT / "install" / "install.sh"
PS1 = ROOT / "install" / "install.ps1"


def _code_lines(text: str) -> list[str]:
    return [line for line in text.splitlines() if line.strip() and not line.lstrip().startswith("#")]


# --- install.sh --------------------------------------------------------------------------------


def test_install_sh_is_plain_posix_sh():
    text = SH.read_text(encoding="utf-8")
    assert text.startswith("#!/bin/sh\n")
    assert "\r" not in text
    bashisms = {
        r"\[\[": "[[ ]]",
        r"^\s*function\s": "function",
        r"^\s*local\s": "local",
        r"\$'": "$'...'",
        r"\becho\s+-[en]": "echo -e/-n",
        r"^\s*source\s": "source",
        r"<<<": "here-string",
        r"\$\{[A-Za-z_]+//": "${var//}",
        r"\[\s[^]]*\s==\s": "== in [ ]",
        r"&>": "&>",
        r"\$RANDOM|\bdeclare\b|\bpushd\b|\bshopt\b": "bash builtins",
        r"^\s*[A-Za-z_]+=\(": "arrays",
    }
    found = [
        f"{name}: {line}"
        for line in _code_lines(text)
        for pattern, name in bashisms.items()
        if re.search(pattern, line)
    ]
    assert found == []


@pytest.mark.allow_system
def test_install_sh_parses_in_a_posix_shell():
    shell = shutil.which("dash") or shutil.which("sh")
    if shell is None:
        pytest.skip("no sh on this machine")
    done = subprocess.run([shell, "-n", str(SH)], capture_output=True, text=True, check=False)
    assert done.returncode == 0, done.stderr


def test_install_sh_checks_every_download_and_never_overwrites():
    text = SH.read_text(encoding="utf-8")
    assert "SOURCE_ASSET=tow-source.tar.gz" in text
    assert "releases/download/$tag/SHA256SUMS" in text
    assert 'fetch "$uv_url.sha256"' in text  # uv against its own release's checksum
    assert text.count('= "$expected"') >= 2  # GitHub's archive, else the release's copy
    assert "TOW is already installed in $dir" in text
    assert '[ -n "$dir" ] || dir=$HOME/TOW' in text
    assert "sudo" not in "".join(_code_lines(text))
    # A failed installation leaves the folder as it found it.
    assert "trap cleanup EXIT" in text
    assert text.index("trap - EXIT") > text.index('"$dir/app/scripts/tow" setup')


def test_install_sh_pins_the_uv_of_the_windows_bundle():
    spec = importlib.util.spec_from_file_location("tow_build_bundle_pins", ROOT / "scripts" / "build-bundle.py")
    assert spec is not None
    assert spec.loader is not None
    bundle = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(bundle)
    assert f"UV_VERSION={bundle.UV_VERSION}\n" in SH.read_text(encoding="utf-8")


def test_install_sh_makes_start_files_that_call_the_apps_scripts():
    text = SH.read_text(encoding="utf-8")
    assert 'start_file="$dir/Start TOW.command"' in text  # macOS: Finder runs it in Terminal
    assert 'start_file="$dir/start-tow"' in text  # Linux
    assert '\'exec "$(dirname "$0")/app/scripts/tow-start" "$@"\'' in text
    assert '\'exec "$(dirname "$0")/app/scripts/tow" stop\'' in text
    assert 'chmod 755 "$start_file" "$stop_file" "$update_file"' in text


def _posix(path: Path) -> str:
    """The path as the shell sees it (Git for Windows' sh wants /c/... for C:\\...)."""
    text = path.as_posix()
    match = re.match(r"^([A-Za-z]):/(.*)$", text)
    return f"/{match.group(1).lower()}/{match.group(2)}" if match else text


@pytest.fixture
def stub_install(tmp_path) -> Path:
    """An install whose tow launcher only writes down what it was asked."""
    root = tmp_path / "my TOW"
    scripts = root / "app" / "scripts"
    scripts.mkdir(parents=True)
    tow = scripts / "tow"
    tow.write_text('#!/bin/sh\nprintf "%s\\n" "$*" >>"$(dirname "$0")/../../../calls.txt"\n', encoding="utf-8")
    tow.chmod(0o755)
    (root / "app" / "pyproject.toml").write_text('[project]\nname = "tow"\n', encoding="utf-8")
    for folder in ("data", "keys", "backup", "runtime/bin"):
        (root / folder).mkdir(parents=True)
    (root / "keys" / "master.key").write_text("not a real key", encoding="utf-8")
    (root / "data" / "state.json").write_text("{}", encoding="utf-8")
    for name in ("config.yaml", "start-tow", "stop-tow", ".update.lock"):
        (root / name).write_text("x", encoding="utf-8")
    return root


def _uninstall(stub_install: Path, home: Path, *options: str) -> subprocess.CompletedProcess[str]:
    shell = shutil.which("dash") or shutil.which("sh")
    if shell is None:
        pytest.skip("no sh on this machine")
    env = {**os.environ, "HOME": _posix(home)}
    env.pop("XDG_DATA_HOME", None)
    return subprocess.run(
        [shell, _posix(SH), "--uninstall", "--dir", _posix(stub_install), *options],
        capture_output=True,
        text=True,
        env=env,
        stdin=subprocess.DEVNULL,
        check=False,
    )


@pytest.mark.allow_system
def test_uninstall_keeps_the_data_and_removes_its_menu_entry(stub_install, tmp_path):
    home = tmp_path / "home"
    menu = home / ".local" / "share" / "applications"
    menu.mkdir(parents=True)
    (menu / "tow.desktop").write_text(f'Exec="{_posix(stub_install)}/start-tow"\n', encoding="utf-8")

    done = _uninstall(stub_install, home, "--yes")

    assert done.returncode == 0, done.stdout + done.stderr
    assert sorted(os.listdir(stub_install)) == [".tow-install", "backup", "config.yaml", "data", "keys"]
    assert (stub_install / "keys" / "master.key").is_file()
    assert (tmp_path / "calls.txt").read_text(encoding="utf-8").split("\n")[:2] == ["autostart off", "stop"]
    assert not (menu / "tow.desktop").exists()
    assert "master.key" in done.stdout


@pytest.mark.allow_system
def test_uninstall_with_purge_removes_everything_and_never_asks_without_a_terminal(stub_install, tmp_path):
    home = tmp_path / "home"
    menu = home / ".local" / "share" / "applications"
    menu.mkdir(parents=True)
    (menu / "tow.desktop").write_text('Exec="/elsewhere/TOW/start-tow"\n', encoding="utf-8")
    done = _uninstall(stub_install, home, "--yes", "--purge")
    assert done.returncode == 0, done.stdout + done.stderr
    assert not stub_install.exists()
    assert (menu / "tow.desktop").exists()  # another install's entry stays


@pytest.mark.allow_system
def test_uninstall_refuses_a_folder_that_is_not_tow(tmp_path):
    (tmp_path / "other").mkdir()
    done = _uninstall(tmp_path / "other", tmp_path / "home", "--yes")
    assert done.returncode == 1
    assert "no TOW install" in done.stderr
    assert (tmp_path / "other").exists()


@pytest.mark.allow_system
@pytest.mark.parametrize("purge", [(), ("--purge",)])
def test_uninstall_never_touches_a_foreign_folder_with_generic_tow_names(tmp_path, purge):
    foreign = tmp_path / "foreign"
    (foreign / "data").mkdir(parents=True)
    (foreign / "keys").mkdir()
    (foreign / "config.yaml").write_text("other app", encoding="utf-8")
    sentinel = foreign / "important.txt"
    sentinel.write_text("keep", encoding="utf-8")

    done = _uninstall(foreign, tmp_path / "home", "--yes", *purge)

    assert done.returncode == 1
    assert "no TOW install" in done.stderr
    assert sentinel.read_text(encoding="utf-8") == "keep"


@pytest.mark.allow_system
def test_uninstall_can_purge_data_kept_by_an_older_installer_only_when_adopted(tmp_path):
    old = tmp_path / "old data"
    (old / "data").mkdir(parents=True)
    (old / "keys").mkdir()
    (old / "config.yaml").write_text("settings", encoding="utf-8")
    (old / "keys" / "master.key").write_text("key", encoding="utf-8")
    assert _uninstall(old, tmp_path / "home", "--yes", "--purge").returncode == 1
    done = _uninstall(old, tmp_path / "home", "--yes", "--purge", "--adopt-data")
    assert done.returncode == 0, done.stderr
    assert not old.exists()


# --- install.ps1 -------------------------------------------------------------------------------


def test_install_ps1_runs_from_irm_iex_on_windows_powershell():
    text = PS1.read_text(encoding="utf-8")
    assert text.isascii()  # irm decodes a release asset in any code page
    code = "\n".join(_code_lines(text))
    assert not re.search(r"(?im)^\s*exit\b", code)  # under iex an exit closes the owner's window
    assert "??" not in code  # Windows PowerShell 5.1 has no ?? and no ternary
    assert not re.search(r"\s\?\s.*\s:\s", code)
    assert "-UseBasicParsing" in code
    assert "SecurityProtocolType]::Tls12" in code
    assert "function Install-Tow" in code  # its settings stay inside the function


def test_install_ps1_checks_the_zip_and_never_overwrites():
    text = PS1.read_text(encoding="utf-8")
    assert "$asset = 'TOW-windows-x64.zip'" in text
    assert "releases/latest/download" in text
    assert "Get-FileHash -LiteralPath $zip -Algorithm SHA256" in text
    assert "TOW is already installed in $Dir" in text
    assert "Join-Path $HOME 'TOW'" in text
    assert "$env:TOW_INSTALL_DIR" in text
    assert "& (Join-Path $Dir 'Start TOW.cmd')" in text
    for option in ("[string]$Version", "[switch]$Autostart", "[switch]$Uninstall", "[switch]$Yes", "[switch]$Purge"):
        assert option in text


@pytest.mark.allow_system
@pytest.mark.parametrize("shell", ["pwsh", "powershell"])
def test_install_ps1_parses(shell):
    program = shutil.which(shell)
    if program is None:
        pytest.skip(f"no {shell} on this machine")
    script = (
        "$errors = $null; "
        f"[void][System.Management.Automation.Language.Parser]::ParseFile('{PS1}', [ref]$null, [ref]$errors); "
        "$errors | ForEach-Object { $_.ToString() }; if ($errors) { exit 1 }"
    )
    done = subprocess.run(
        [program, "-NoProfile", "-NonInteractive", "-Command", script], capture_output=True, text=True, check=False
    )
    assert done.returncode == 0, done.stdout + done.stderr


@pytest.mark.allow_system
def test_install_ps1_refuses_foreign_data_folder_even_with_purge(tmp_path):
    shell = shutil.which("pwsh")
    if shell is None:
        pytest.skip("PowerShell is not installed")
    foreign = tmp_path / "foreign data"
    (foreign / "data").mkdir(parents=True)
    (foreign / "config.yaml").write_text("other app", encoding="utf-8")
    sentinel = foreign / "important.txt"
    sentinel.write_text("keep", encoding="utf-8")
    done = subprocess.run(
        [shell, "-NoProfile", "-File", str(PS1), "-Dir", str(foreign), "-Uninstall", "-Yes", "-Purge"],
        capture_output=True,
        text=True,
        check=False,
    )
    assert done.returncode != 0
    assert "no TOW install" in done.stderr
    assert sentinel.read_text(encoding="utf-8") == "keep"


# --- the release workflow ----------------------------------------------------------------------


def test_the_release_workflow_tests_everything_before_it_uploads():
    import yaml

    workflows = ROOT / ".github" / "workflows"
    text = (workflows / "release.yml").read_text(encoding="utf-8")
    flow = yaml.safe_load(text)
    jobs = flow["jobs"]
    # Every action pinned to a commit; checkout and setup-uv to the very ones CI uses.
    uses = re.findall(r"uses: (\S+)", text)
    assert uses
    assert all(re.fullmatch(r"[\w.-]+/[\w.-]+@[0-9a-f]{40}", use) for use in uses), uses
    ci_pins = set(re.findall(r"uses: (\S+@[0-9a-f]{40})", (workflows / "ci.yml").read_text(encoding="utf-8")))
    assert {use for use in uses if use.startswith(("actions/checkout@", "astral-sh/setup-uv@"))} <= ci_pins
    # Write access only where the release is touched, and only after every test passed.
    assert flow["permissions"] == {"contents": "read"}
    writers = [name for name, job in jobs.items() if job.get("permissions", {}).get("contents") == "write"]
    assert writers == ["publish"]
    assert set(jobs["publish"]["needs"]) == {"gate", "source", "windows", "posix"}
    assert set(jobs["gate"]["strategy"]["matrix"]["os"]) == {"windows-latest", "ubuntu-latest", "macos-latest"}
    windows = "\n".join(step.get("run", "") for step in jobs["windows"]["steps"])
    assert "scripts/build-bundle.py --out dist --source tow-source.tar.gz" in windows
    assert "bundle-smoke.ps1 -Zip dist/TOW-windows-x64.zip -Offline" in windows
    assert "powershell.exe -NoProfile -ExecutionPolicy Bypass -File install/install.ps1" in windows  # 5.1
    assert "-Uninstall -Yes -Purge" in windows
    assert jobs["posix"]["strategy"]["matrix"]["os"] == ["ubuntu-latest", "macos-latest"]
    assert "scripts/install-smoke.sh" in jobs["posix"]["steps"][-1]["run"]
    publish = "\n".join(step.get("run", "") for step in jobs["publish"]["steps"])
    assert "sha256sum TOW-windows-x64.zip install.ps1 install.sh tow-source.tar.gz > SHA256SUMS" in publish
    assert "gh release view" in publish  # created only when missing: existing notes stay
    assert "gh release upload" in publish
    assert "edit" not in publish.split()


def test_the_smoke_scripts_never_use_the_live_port():
    for name in ("bundle-smoke.ps1", "install-smoke.sh"):
        text = (ROOT / "scripts" / name).read_text(encoding="utf-8")
        assert "never on 8787" in text
        assert "TOW_NO_BROWSER" in text
