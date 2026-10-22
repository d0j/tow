"""install/install.sh (Linux, macOS) and install/install.ps1 (Windows): portable syntax, the
checks they make, and the uninstall of install.sh on a stub install. The full installs run in
the release workflow (.github/workflows/release.yml)."""

from __future__ import annotations

import contextlib
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


def test_install_sh_runs_nothing_until_it_is_read_to_the_end():
    # curl | sh runs what has arrived: a cut download must not run half an installation.
    text = SH.read_text(encoding="utf-8")
    assert text.endswith('\nmain "$@"\n')
    body = text[: text.index("\nmain() {\n")]
    commands = [line for line in _code_lines(body) if not line.startswith((" ", "}"))]
    # Before main: only settings and the two message helpers.
    assert all(re.match(r"(set -eu|[A-Z_]+=\S+|say\(\) |die\(\) \{)", line) for line in commands), commands


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


@pytest.mark.allow_system  # sh on a temp layout: the update file install.sh writes, python3 a stub
def test_the_update_file_takes_the_environments_python_else_the_newest_real_one(tmp_path):
    # It took the first cpython-3* folder: uv's cpython-3.14 link, or 3.14.10 before 3.14.8.
    shell = shutil.which("dash") or shutil.which("sh")
    if shell is None:
        pytest.skip("no sh on this machine")
    text = SH.read_text(encoding="utf-8")
    start = text.index("<<'EOF'\n") + len("<<'EOF'\n")
    root = tmp_path / "my TOW"
    python = root / "runtime" / "python"
    for name in (
        "cpython-3.14.8-linux-x86_64-gnu",
        "cpython-3.14.10-linux-x86_64-gnu",
        "cpython-3.9.20-linux-x86_64-gnu",
        "cpython-3.15.0a1-linux-x86_64-gnu",
        "cpython-3.14.11+freethreaded-linux-x86_64-gnu",
    ):
        (python / name / "bin").mkdir(parents=True)
        stub = python / name / "bin" / "python3"
        stub.write_text(f'#!/bin/sh\necho "{name} $*"\n', encoding="utf-8", newline="\n")
        stub.chmod(0o755)
    with contextlib.suppress(OSError):  # Windows without the right to make links: the rest still holds
        (python / "cpython-3.14.99-linux-x86_64-gnu").symlink_to(python / "cpython-3.14.8-linux-x86_64-gnu")
    (root / "update-tow").write_text(text[start : text.index("\nEOF\n", start) + 1], encoding="utf-8", newline="\n")

    def picked() -> str:
        done = subprocess.run(
            [shell, _posix(root / "update-tow"), "v9.9.9"], capture_output=True, text=True, check=False, timeout=60
        )
        assert done.stdout.endswith(" --ref v9.9.9\n"), done.stdout + done.stderr
        return done.stdout.split()[0]

    assert picked() == "cpython-3.14.10-linux-x86_64-gnu"
    cfg = root / "app" / ".venv" / "pyvenv.cfg"
    cfg.parent.mkdir(parents=True)
    # Made in another place (moved since): found by its folder name in this runtime/python.
    cfg.write_text("home = /old place/runtime/python/cpython-3.14.8-linux-x86_64-gnu/bin\n", encoding="utf-8")
    assert picked() == "cpython-3.14.8-linux-x86_64-gnu"
    cfg.write_text("home = /old/cpython-3.13.1-linux-x86_64-gnu/bin\n", encoding="utf-8")
    assert picked() == "cpython-3.14.10-linux-x86_64-gnu"


def _posix(path: Path) -> str:
    """The path as the shell sees it (Git for Windows' sh wants /c/... for C:\\...)."""
    text = path.as_posix()
    match = re.match(r"^([A-Za-z]):/(.*)$", text)
    return f"/{match.group(1).lower()}/{match.group(2)}" if match else text


# What install.sh runs before its first download, besides the stubs a test puts first on PATH.
_SH_TOOLS = ("mkdir", "rm", "mv", "cp", "cat", "cut", "tr", "find", "head", "tail", "sed", "awk", "grep", "tar")
_SH_TOOLS += ("gzip", "chmod", "dirname", "basename", "ls", "sha256sum", "shasum", "openssl")


def _sh_with_stubs(tmp_path: Path, stubs: dict[str, str]) -> tuple[str, str]:
    """The shell and a PATH of only `stubs` (shell scripts by name) and the plain tools: no real
    curl, wget, sysctl or uname, so a test decides which of them exist and what they say."""
    shell = shutil.which("dash") or shutil.which("sh")
    if shell is None:
        pytest.skip("no sh on this machine")
    stub_dir = tmp_path / "stubs"
    stub_dir.mkdir()
    for name, body in stubs.items():
        (stub_dir / name).write_text(f"#!/bin/sh\n{body}\n", encoding="utf-8", newline="\n")
        (stub_dir / name).chmod(0o755)
    if os.name == "nt":
        # Git for Windows: its usr\bin holds the plain tools, and no curl, wget or sysctl.
        tools = Path(shell).parent
        if any((tools / f"{name}.exe").exists() for name in ("curl", "wget", "sysctl")):
            pytest.skip("this sh's own folder has a curl, wget or sysctl")
    else:
        tools = tmp_path / "tools"
        tools.mkdir()
        for name in _SH_TOOLS:
            found = shutil.which(name)
            if found:
                (tools / name).symlink_to(found)
    return shell, os.pathsep.join([str(stub_dir), str(tools)])


def _source_tar(path: Path) -> Path:
    """A source archive of one folder whose scripts/tow only ends."""
    import io
    import tarfile

    with tarfile.open(path, "w:gz") as archive:
        data = b"#!/bin/sh\nexit 0\n"
        info = tarfile.TarInfo("tow-x/scripts/tow")
        info.size = len(data)
        info.mode = 0o755
        archive.addfile(info, io.BytesIO(data))
    return path


def _install_sh(tmp_path: Path, stubs: dict[str, str], *options: str, source: bool = True):
    shell, path = _sh_with_stubs(tmp_path, stubs)
    env = {**os.environ, "PATH": path, "HOME": _posix(tmp_path / "home"), "TOW_INSTALL_DIR": _posix(tmp_path / "TOW")}
    for name in ("TOW_INSTALL_SOURCE", "TOW_INSTALL_SUMS", "TOW_VERSION", "TOW_INSTALL_PORT"):
        env.pop(name, None)
    if source:
        env["TOW_INSTALL_SOURCE"] = _posix(_source_tar(tmp_path / "tow-source.tar.gz"))
    return subprocess.run(
        [shell, _posix(SH), *options],
        capture_output=True,
        text=True,
        env=env,
        stdin=subprocess.DEVNULL,
        timeout=120,
        check=False,
    )


_MAC_UNAME = "case $1 in -s) echo Darwin ;; -m) echo {arch} ;; *) echo Darwin ;; esac"
_FAILED_CURL = 'printf "%s\\n" "$*" >>"$(dirname "$0")/../curl.log"; exit 22'


@pytest.mark.allow_system  # install.sh with stub uname/sysctl/curl in a temp folder; stops at the uv download
@pytest.mark.parametrize(
    ("arch", "sysctl", "target", "intel_note"),
    [
        ("x86_64", "echo 1", "aarch64-apple-darwin", False),  # a Terminal under Rosetta on Apple silicon
        ("x86_64", "echo 0", "x86_64-apple-darwin", True),  # a real Intel Mac
        ("x86_64", 'echo "sysctl: unknown oid" >&2; exit 1', "x86_64-apple-darwin", True),  # older macOS
        ("x86_64", None, "x86_64-apple-darwin", True),  # no sysctl at all
        ("arm64", "echo 0", "aarch64-apple-darwin", False),
    ],
)
def test_install_sh_takes_apple_silicon_uv_under_rosetta(tmp_path, arch, sysctl, target, intel_note):
    # uname -m says x86_64 in a shell Rosetta translates: the Intel uv and Python, slower, and an
    # Intel-only build step for one package, on a Mac that runs the native ones.
    stubs = {"uname": _MAC_UNAME.format(arch=arch), "curl": _FAILED_CURL}
    if sysctl is not None:
        stubs["sysctl"] = f'[ "$*" = "-n sysctl.proc_translated" ] || exit 1\n{sysctl}'
    done = _install_sh(tmp_path, stubs)
    assert done.returncode == 1, done.stdout + done.stderr
    assert f"uv {_sh_uv_version()} ({target})" in done.stdout, done.stdout + done.stderr
    assert f"/uv-{target}.tar.gz" in (tmp_path / "curl.log").read_text(encoding="utf-8")
    assert ("an Intel Mac needs a Rust toolchain" in done.stdout) is intel_note
    assert ("Rosetta" in done.stdout) is (arch == "x86_64" and target.startswith("aarch64"))
    assert not (tmp_path / "TOW").exists()  # the failed download leaves nothing


_LINUX_UNAME = "case $1 in -s) echo Linux ;; -m) echo {arch} ;; *) echo Linux ;; esac"


@pytest.mark.allow_system  # install.sh with a stub uname/curl in a temp folder; refused before any download
@pytest.mark.parametrize("arch", ["armv7l", "armv6l", "armv8l", "i686", "riscv64", "s390x"])
def test_install_sh_refuses_a_system_outside_the_contract_before_anything(tmp_path, arch):
    # B13: ARMv7 was accepted, then its setup stopped half way (six packages to compile on glibc,
    # no Python at all on musl, docs/PORTABLE.md 1b); the other systems were refused already.
    done = _install_sh(tmp_path, {"uname": _LINUX_UNAME.format(arch=arch), "curl": _FAILED_CURL})
    assert done.returncode == 1, done.stdout + done.stderr
    assert f"this computer (Linux {arch}) is not supported" in done.stderr, done.stdout + done.stderr
    assert not (tmp_path / "curl.log").exists()  # nothing downloaded
    assert not (tmp_path / "TOW").exists()  # nothing written


@pytest.mark.allow_system  # install.sh with a stub uname/ldd/curl in a temp folder; stops at the uv download
@pytest.mark.parametrize(
    ("arch", "ldd", "target"),
    [
        ("x86_64", "echo 'ldd (GNU libc) 2.39'", "x86_64-unknown-linux-gnu"),
        ("aarch64", "echo 'ldd (GNU libc) 2.39'", "aarch64-unknown-linux-gnu"),
        ("x86_64", "echo 'musl libc (x86_64)' >&2; exit 1", "x86_64-unknown-linux-musl"),
        ("aarch64", "echo 'musl libc (aarch64)' >&2; exit 1", "aarch64-unknown-linux-musl"),
    ],
)
def test_install_sh_takes_the_uv_of_a_supported_linux(tmp_path, arch, ldd, target):
    stubs = {"uname": _LINUX_UNAME.format(arch=arch), "ldd": ldd, "curl": _FAILED_CURL}
    done = _install_sh(tmp_path, stubs)
    assert done.returncode == 1, done.stdout + done.stderr
    assert f"uv {_sh_uv_version()} ({target})" in done.stdout, done.stdout + done.stderr
    assert f"/uv-{target}.tar.gz" in (tmp_path / "curl.log").read_text(encoding="utf-8")
    assert not (tmp_path / "TOW").exists()


def _sh_uv_version() -> str:
    match = re.search(r"^UV_VERSION=(\S+)$", SH.read_text(encoding="utf-8"), re.MULTILINE)
    assert match is not None
    return match.group(1)


_BUSYBOX_WGET = (
    'case $1 in --help) echo "BusyBox v1.36.1 (2024-06-10) multi-call binary."\n'
    'echo "Usage: wget [-cqS] [--spider] [-O FILE] [-o LOGFILE] [--header STR] [-T SEC] URL..." ;;\n'
    '*) printf "%s\\n" "$*" >>"$(dirname "$0")/../wget.log"; echo "wget: unrecognized option" >&2; exit 1 ;; esac'
)
_GNU_WGET = (
    'case $1 in --help) echo "GNU Wget 1.24.5, a non-interactive network retriever."\n'
    'echo "       --max-redirect              maximum redirections allowed per page"\n'
    'echo "       --https-only                only follow secure HTTPS links" ;;\n'
    '*) printf "%s\\n" "$*" >>"$(dirname "$0")/../wget.log"\n'
    'case "$*" in *--max-redirect=0*)\n'
    'echo "  Location: https://github.com/d0j/tow/releases/tag/v9.9.9" >&2; exit 8 ;; esac\n'
    "exit 8 ;; esac"
)


@pytest.mark.allow_system  # install.sh with a stub wget and no curl in a temp folder; no download happens
@pytest.mark.parametrize("version", [("--version", "v9.9.8"), ()], ids=["release-files", "latest-tag"])
def test_install_sh_asks_for_curl_when_wget_cannot_keep_to_https(tmp_path, version):
    # BusyBox wget has no --https-only and no --max-redirect: every download failed as "release
    # ... has no SHA256SUMS" or "could not find the latest release", which pointed nowhere.
    stubs = {"uname": _MAC_UNAME.format(arch="arm64"), "wget": _BUSYBOX_WGET}
    done = _install_sh(tmp_path, stubs, *version, source=False)
    assert done.returncode == 1, done.stdout + done.stderr
    assert "install curl" in done.stderr, done.stdout + done.stderr
    assert not (tmp_path / "wget.log").exists()  # refused before any download, with no weaker one
    assert not (tmp_path / "TOW").exists()


@pytest.mark.allow_system  # install.sh with a stub GNU wget and no curl in a temp folder; downloads fail
@pytest.mark.parametrize("version", [("--version", "v9.9.8"), ()], ids=["release-files", "latest-tag"])
def test_install_sh_keeps_gnu_wget_on_https(tmp_path, version):
    done = _install_sh(tmp_path, {"uname": _MAC_UNAME.format(arch="arm64"), "wget": _GNU_WGET}, *version, source=False)
    assert done.returncode == 1, done.stdout + done.stderr
    tag = version[1] if version else "v9.9.9"  # the latest one from its redirect
    assert f"release {tag} has no SHA256SUMS" in done.stderr, done.stdout + done.stderr
    calls = (tmp_path / "wget.log").read_text(encoding="utf-8").splitlines()
    if not version:
        assert calls[0] == "-q -S --max-redirect=0 -O /dev/null https://github.com/d0j/tow/releases/latest"
    assert calls[-1].startswith("-q --https-only -O ")
    assert calls[-1].endswith(f" https://github.com/d0j/tow/releases/download/{tag}/SHA256SUMS")
    assert not (tmp_path / "TOW").exists()


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
    # One download for 5.1 and 7 to a literal path (test_install_ps1_downloads_into_a_folder_with_brackets).
    assert "Invoke-WebRequest" not in code
    assert "$client.DownloadFile($Uri, $Path)" in code
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


def test_install_ps1_closes_the_install_folder_before_anything_is_written_in_it():
    text = PS1.read_text(encoding="utf-8")
    grant = "/inheritance:r /grant:r \"*${sid}:(OI)(CI)F\" '*S-1-5-18:(OI)(CI)F' '*S-1-5-32-544:(OI)(CI)F'"
    assert grant in text
    assert "Join-Path $env:SystemRoot 'System32\\icacls.exe'" in text
    # Only a folder this run created or this account owns, never through a link.
    assert "GetOwner([Security.Principal.SecurityIdentifier])" in text
    assert "if (-not $New -and $owner -ne $sid) { return $false }" in text
    assert "[IO.FileAttributes]::ReparsePoint) { return $false }" in text
    close_root = text.index("$rootClosed = Close-Folder $Dir $created")
    assert close_root < text.index('Save-Download "$base/SHA256SUMS"') < text.index("ExtractToFile")
    # keys\ and data\ on their own when the folder stays open (another account's).
    assert "foreach ($name in @('keys', 'data'))" in text
    assert "if (-not (Close-Folder $folder $new))" in text
    assert text.index("Close-Folder $folder $new") < text.index("& (Join-Path $Dir 'Start TOW.cmd')")


def test_install_ps1_refuses_a_long_folder_before_writing_anything():
    # A folder over the limit of docs/PORTABLE.md 3 failed in the first start, after the install
    # was in place.
    text = PS1.read_text(encoding="utf-8")
    first_write = text.index("New-Item -ItemType Directory -Force -Path $Dir")
    long = text.index("if ($Dir.Length -gt 110 -and $longPaths -ne 1)")
    assert "-Name LongPathsEnabled" in text
    assert "longer than about\n  110 characters" in (ROOT / "docs" / "PORTABLE.md").read_text(encoding="utf-8")
    assert long < first_write
    assert long < text.index('Save-Download "$base/SHA256SUMS"')


def test_install_ps1_refuses_windows_on_arm_before_writing_anything():
    # Windows on ARM got the x64 zip and failed later, inside its x64 uv and Python.
    text = PS1.read_text(encoding="utf-8")
    arm = text.index("-contains 'ARM64'")
    assert "Session Manager\\Environment' -Name PROCESSOR_ARCHITECTURE" in text  # emulated x64 says AMD64
    assert "$env:PROCESSOR_ARCHITEW6432, $machine) -contains 'ARM64'" in text  # a 32-bit shell on ARM64
    assert "Windows on ARM is not supported" in text
    assert arm < text.index("New-Item -ItemType Directory -Force -Path $Dir")
    for guide in ("install.md", "ru/install.md"):
        assert "ARM" in (ROOT / "docs" / guide).read_text(encoding="utf-8").split("## macOS")[0]


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
    # A sentence, not a PowerShell error record (QA: "At ...install.ps1:72 char:...", CategoryInfo).
    assert done.stderr.strip().startswith("TOW: no TOW install in ")
    assert "CategoryInfo" not in done.stderr
    assert "char:" not in done.stderr
    assert sentinel.read_text(encoding="utf-8") == "keep"


@pytest.mark.allow_system  # install.ps1 refusing a folder; a missing local zip stands for the download
@pytest.mark.parametrize("shell", ["pwsh", "powershell"])
@pytest.mark.parametrize(
    ("name", "refused"),
    [("x%PATH%y", True), ("x!a!y", True), ("a^b!c", True), ("x 50%", False), ("a^b", False), ("b!ang", False)],
)
def test_install_ps1_refuses_only_a_folder_cmd_cannot_start_from(tmp_path, shell, name, refused):
    # QA 16.10.2026: cmd.exe reads the path of the start file it is given before the file runs, so
    # %NAME% (and with delayed expansion !NAME!, or ^ next to !) in it changed the path: the install
    # was written, then did not start. One % or ! alone, or ^ alone, start (test_launchers.py).
    program = shutil.which(shell)
    if program is None or (not refused and os.name != "nt"):
        pytest.skip(f"no {shell} here" if program is None else "the accepted folder goes on to Windows-only steps")
    folder = tmp_path / name / "TOW"
    env = {**os.environ, "TOW_INSTALL_SOURCE": str(tmp_path / "no-such.zip"), "TOW_NO_BROWSER": "1"}
    done = subprocess.run(
        [program, "-NoProfile", "-NonInteractive", "-File", str(PS1), "-Dir", str(folder), "-Yes"],
        capture_output=True,
        text=True,
        env=env,
        timeout=120,
        check=False,
    )
    assert done.returncode != 0  # refused, or (accepted) no zip to unpack
    assert ("TOW: Windows cannot start TOW from" in done.stderr) is refused, done.stdout + done.stderr
    assert not folder.exists()  # refused before anything was written; an accepted one cleaned up
    text = PS1.read_text(encoding="utf-8")
    assert text.index("Windows cannot start TOW from") < text.index("New-Item -ItemType Directory -Force -Path $Dir")


def _stub_zip(path: Path, extra: dict[str, str] | None = None) -> str:
    """A TOW-windows-x64.zip whose start file only ends: its SHA256SUMS line."""
    import hashlib
    import zipfile

    files = {
        "TOW/Start TOW.cmd": "@exit /b 0\r\n",
        "TOW/config.yaml": "port: 18996\n",
        "TOW/README.txt": "TOW\n",
        "TOW/app/scripts/tow.cmd": "@exit /b 0\r\n",
        "TOW/runtime/python/cpython-3.14.8-windows-x86_64-none/Lib/deep/file.txt": "x\n",
        **(extra or {}),
    }
    with zipfile.ZipFile(path, "w") as archive:
        archive.writestr("TOW/runtime/", "")
        for name, text in files.items():
            archive.writestr(name, text)
    return f"{hashlib.sha256(path.read_bytes()).hexdigest()}  TOW-windows-x64.zip\n"


@pytest.mark.allow_system  # install.ps1 on a local stub zip in a temp folder; its start file only ends
@pytest.mark.skipif(os.name != "nt", reason="install.ps1 installs on Windows")
@pytest.mark.parametrize("shell", ["pwsh", "powershell"])
def test_install_ps1_unpacks_each_file_into_its_place_and_keeps_what_is_there(tmp_path, shell):
    # QA 16.10.2026: the zip went to <folder>\.install\unpacked\TOW first, 22 characters deeper
    # than the install itself, so a folder the length check allowed could fail while unpacking.
    program = shutil.which(shell)
    if program is None:
        pytest.skip(f"no {shell} here")
    assert "unpacked" not in "\n".join(_code_lines(PS1.read_text(encoding="utf-8")))
    zip_path = tmp_path / "TOW-windows-x64.zip"
    sums = tmp_path / "SHA256SUMS"
    sums.write_text(_stub_zip(zip_path), encoding="utf-8")
    folder = tmp_path / "Мой TOW (1)"
    folder.mkdir()
    (folder / ".tow-install").write_text("TOW portable install v1\n", encoding="utf-8")
    (folder / "config.yaml").write_text("port: 18995\n", encoding="utf-8")  # an earlier TOW's settings
    env = {**os.environ, "TOW_INSTALL_SOURCE": str(zip_path), "TOW_INSTALL_SUMS": str(sums), "TOW_NO_BROWSER": "1"}
    argv = [program, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(PS1)]
    done = subprocess.run(
        [*argv, "-Dir", str(folder), "-Yes"], capture_output=True, text=True, env=env, timeout=180, check=False
    )
    assert done.returncode == 0, done.stdout + done.stderr
    assert (folder / "config.yaml").read_text(encoding="utf-8") == "port: 18995\n"
    assert (folder / "app" / "scripts" / "tow.cmd").is_file()
    deep = folder / "runtime" / "python" / "cpython-3.14.8-windows-x86_64-none" / "Lib" / "deep" / "file.txt"
    assert deep.is_file()
    assert not (folder / ".install").exists()
    assert not (folder / "TOW").exists()


@pytest.mark.allow_system  # install.ps1 on a local stub zip in a temp folder; its start file only ends
@pytest.mark.skipif(os.name != "nt", reason="install.ps1 installs on Windows")
def test_install_ps1_in_windows_powershell_started_from_powershell_7(tmp_path):
    # QA 16.10.2026: Windows PowerShell 5.1 started from a PowerShell 7 window inherits 7's module
    # folders; Get-FileHash and Get-Acl then failed to load and the install stopped.
    powershell, pwsh = shutil.which("powershell"), shutil.which("pwsh")
    if powershell is None or pwsh is None:
        pytest.skip("needs both Windows PowerShell and PowerShell 7")
    seven = subprocess.run(
        [pwsh, "-NoProfile", "-NonInteractive", "-Command", "$env:PSModulePath"],
        capture_output=True,
        text=True,
        timeout=60,
        check=True,
    ).stdout.strip()
    zip_path = tmp_path / "TOW-windows-x64.zip"
    sums = tmp_path / "SHA256SUMS"
    sums.write_text(_stub_zip(zip_path), encoding="utf-8")
    folder = tmp_path / "TOW"
    env = {
        **os.environ,
        "PSModulePath": seven,
        "TOW_INSTALL_SOURCE": str(zip_path),
        "TOW_INSTALL_SUMS": str(sums),
        "TOW_NO_BROWSER": "1",
    }
    script = f"& '{PS1}' -Dir '{folder}' -Yes; 'module path kept: ' + ($env:PSModulePath -eq '{seven}')"
    done = subprocess.run(
        [powershell, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-Command", script],
        capture_output=True,
        text=True,
        env=env,
        timeout=180,
        check=False,
    )
    assert "the download matches the release's SHA256SUMS" in done.stdout, done.stdout + done.stderr
    assert (folder / "app" / "scripts" / "tow.cmd").is_file()
    assert "module path kept: True" in done.stdout  # the caller's session gets its own value back


@pytest.mark.allow_system  # install.ps1 on a local stub zip in a temp folder; refused before the start
@pytest.mark.skipif(os.name != "nt", reason="install.ps1 installs on Windows")
def test_install_ps1_refuses_a_zip_entry_outside_its_tow_folder(tmp_path):
    program = shutil.which("pwsh")
    if program is None:
        pytest.skip("no pwsh here")
    zip_path = tmp_path / "TOW-windows-x64.zip"
    sums = tmp_path / "SHA256SUMS"
    sums.write_text(_stub_zip(zip_path, {"TOW/../outside.txt": "x"}), encoding="utf-8")
    folder = tmp_path / "TOW"
    env = {**os.environ, "TOW_INSTALL_SOURCE": str(zip_path), "TOW_INSTALL_SUMS": str(sums), "TOW_NO_BROWSER": "1"}
    done = subprocess.run(
        [program, "-NoProfile", "-NonInteractive", "-File", str(PS1), "-Dir", str(folder), "-Yes"],
        capture_output=True,
        text=True,
        env=env,
        timeout=180,
        check=False,
    )
    assert done.returncode != 0
    assert "unexpected entry" in done.stderr, done.stdout + done.stderr
    assert not (tmp_path / "outside.txt").exists()
    assert not folder.exists()  # a folder this run created goes again


@pytest.mark.allow_system  # install.ps1 downloading from a loopback test server into a temp folder
@pytest.mark.skipif(os.name != "nt", reason="install.ps1 installs on Windows")
@pytest.mark.parametrize("shell", ["powershell", "pwsh"])
@pytest.mark.parametrize("matching", [True, False], ids=["checked", "mismatch"])
def test_install_ps1_downloads_into_a_folder_with_brackets(tmp_path, shell, matching, loopback_http_server):
    # QA: Windows PowerShell 5.1 reads Invoke-WebRequest -OutFile as a wildcard, so in a folder like
    # "D:\[x] TOW" the download failed (and an escaped path would be a different file on 7).
    import http.server
    import threading
    import zipfile

    program = shutil.which(shell)
    if program is None:
        pytest.skip(f"no {shell} here")
    zip_path = tmp_path / "served" / "TOW-windows-x64.zip"
    zip_path.parent.mkdir()
    sums = _stub_zip(zip_path)
    if not matching:
        sums = ("0" * 64) + sums[64:]
    served = {"/v9.9.9/SHA256SUMS": sums.encode(), "/v9.9.9/TOW-windows-x64.zip": zip_path.read_bytes()}
    asked: list[str] = []

    class Release(http.server.BaseHTTPRequestHandler):
        def do_GET(self):
            asked.append(self.path)
            body = served.get(self.path)
            self.send_response(200 if body is not None else 404)
            self.send_header("Content-Type", "application/octet-stream")
            self.send_header("Content-Length", str(len(body or b"")))
            self.end_headers()
            self.wfile.write(body or b"")

        def log_message(self, *args):
            pass

    server = loopback_http_server(Release)
    threading.Thread(target=server.serve_forever, daemon=True).start()
    try:
        parent = tmp_path / "case" / "[x] TOW"
        parent.mkdir(parents=True)
        folder = parent / "TOW"
        env = {
            **os.environ,
            "TOW_INSTALL_URL": f"http://127.0.0.1:{server.server_address[1]}/v9.9.9",
            "TOW_NO_BROWSER": "1",
        }
        for name in ("TOW_INSTALL_SOURCE", "TOW_INSTALL_SUMS", "TOW_VERSION", "TOW_INSTALL_DIR"):
            env.pop(name, None)
        argv = [program, "-NoProfile", "-NonInteractive", "-ExecutionPolicy", "Bypass", "-File", str(PS1)]
        done = subprocess.run(
            [*argv, "-Dir", str(folder), "-Yes"], capture_output=True, text=True, env=env, timeout=180, check=False
        )
    finally:
        server.shutdown()
        server.server_close()
    assert asked == ["/v9.9.9/SHA256SUMS", "/v9.9.9/TOW-windows-x64.zip"], done.stdout + done.stderr
    assert os.listdir(tmp_path / "case") == ["[x] TOW"]  # no file under another name
    if matching:
        assert done.returncode == 0, done.stdout + done.stderr
        assert "the download matches the release's SHA256SUMS" in done.stdout
        assert os.listdir(parent) == ["TOW"]
        with zipfile.ZipFile(zip_path) as archive:
            for entry in archive.namelist():
                if not entry.endswith("/"):
                    assert (parent / entry).read_bytes() == archive.read(entry), entry
        assert not (folder / ".install").exists()
    else:
        assert done.returncode != 0
        assert "TOW: TOW-windows-x64.zip does not match the release's SHA256SUMS" in done.stderr, done.stderr
        assert os.listdir(parent) == []  # the folder this run made goes again


@pytest.mark.allow_system
@pytest.mark.parametrize("shell", ["pwsh", "powershell"])
def test_install_ps1_takes_test_downloads_only_from_this_computer(tmp_path, shell):
    program = shutil.which(shell)
    if program is None:
        pytest.skip(f"no {shell} here")
    folder = tmp_path / "TOW"
    env = {**os.environ, "TOW_INSTALL_URL": "http://example.com/v9.9.9", "TOW_NO_BROWSER": "1"}
    env.pop("TOW_INSTALL_SOURCE", None)
    done = subprocess.run(
        [program, "-NoProfile", "-NonInteractive", "-File", str(PS1), "-Dir", str(folder), "-Yes"],
        capture_output=True,
        text=True,
        env=env,
        timeout=120,
        check=False,
    )
    assert done.returncode != 0
    assert "TOW_INSTALL_URL" in done.stderr, done.stdout + done.stderr
    assert not folder.exists()


def test_install_ps1_prints_a_runnable_autostart_hint_and_the_real_port():
    text = PS1.read_text(encoding="utf-8")
    # A quoted path alone is only a string in PowerShell: `& ` runs it.
    assert 'Write-Host "  Autostart: & `"$launcher`" autostart on"' in text
    # A reinstall around kept data shows the port of its config.yaml, not 8787 whatever it says.
    assert "'(?m)^port:\\s*(\\d+)\\s*$'" in text
    assert '$page = "http://127.0.0.1:$shown"' in text
    assert "if ($PSCommandPath) { exit 1 }" in text  # a file run fails; `irm | iex` keeps its window


# --- the release workflow ----------------------------------------------------------------------


def _workflow(name):
    import yaml

    text = (ROOT / ".github" / "workflows" / f"{name}.yml").read_text(encoding="utf-8")
    return text, yaml.safe_load(text)


def _runs(job):
    return "\n".join(step.get("run", "") for step in job["steps"])


def test_every_workflow_is_checked_here():
    found = sorted(path.stem for path in (ROOT / ".github" / "workflows").glob("*.yml"))
    assert found == ["autostart", "ci", "installers", "real", "release", "smokes"]


@pytest.mark.parametrize("name", ["ci", "installers", "real", "autostart", "smokes"])
def test_only_main_gate_writes_shared_uv_cache(name):
    _text, flow = _workflow(name)
    found = 0
    for job_id, job in flow["jobs"].items():
        for step in job.get("steps", []):  # a job that calls a reusable workflow has none
            if not step.get("uses", "").startswith("astral-sh/setup-uv@"):
                continue
            found += 1
            options = step.get("with", {})
            if name == "smokes":  # the commit it is given, in the default branch's scope: no cache
                assert options == {"enable-cache": False}, job_id
                continue
            assert options.get("restore-cache", True) is True
            if name == "ci" and job_id == "gate":
                assert options["save-cache"] == "${{ github.event_name == 'push' && github.ref == 'refs/heads/main' }}"
            else:
                assert options["save-cache"] is False
    assert found == {"ci": 2, "installers": 3, "real": 4, "autostart": 1, "smokes": 6}[name]


@pytest.mark.parametrize("name", ["ci", "release", "installers", "real", "autostart", "smokes"])
def test_every_workflow_pins_its_actions_and_only_reads_by_default(name):
    text, flow = _workflow(name)
    # Every action pinned to a commit (the repository requires it); checkout and setup-uv to the
    # very ones CI uses.
    # A reusable workflow of this repository (./.github/workflows/...) is this commit's own file.
    local = re.findall(r"uses: (\./\.github/workflows/\S+)", text)
    assert all((ROOT / use).is_file() for use in local), local
    uses = [use for use in re.findall(r"uses: (\S+)", text) if use not in local]
    assert uses
    assert all(re.fullmatch(r"[\w.-]+/[\w.-]+@[0-9a-f]{40}", use) for use in uses), uses
    ci_pins = set(re.findall(r"uses: (\S+@[0-9a-f]{40})", _workflow("ci")[0]))
    assert {use for use in uses if use.startswith(("actions/checkout@", "astral-sh/setup-uv@"))} <= ci_pins
    assert flow["permissions"] == {"contents": "read"}
    assert "persist-credentials: false" in text
    for job_id, job in flow["jobs"].items():
        if job_id not in ("source", "publish"):
            assert "permissions" not in job, job_id


def test_installers_run_the_release_smokes_before_a_tag():
    _text, flow = _workflow("installers")
    _release_text, release = _workflow("release")
    triggers = flow[True]  # YAML 1.1 reads the key `on` as True
    paths = triggers["pull_request"]["paths"]
    for path in ("scripts/**", "install/**", "src/tow/update*", "src/tow/web_update.py"):
        assert path in paths
    # What the updater relies on in the code it installs (before: a change there ran no update test;
    # 1.31.0 broke the restore point an update from Settings makes first).
    # TC-08: the bundle starts and stops TOW through the whole OS layer (platform/windows.py,
    # posix.py), and `tow run` reads lifecycle.py at start.
    for path in (
        "src/tow/store.py",
        "src/tow/storage/**",
        "src/tow/platform/**",
        "src/tow/lifecycle.py",
        "src/tow/paths.py",
        "src/tow/supervisor/**",
        "src/tow/restore_points.py",
        "src/tow/snapshots/**",
        "src/tow/cli/**",
        "src/tow/locales/**",
    ):
        assert path in paths
    for path in paths:  # no filter names a file that is gone
        assert next(ROOT.glob(path.removesuffix("/**")), None), path
    assert triggers["schedule"]
    assert "workflow_dispatch" in triggers
    jobs = flow["jobs"]
    # The same smokes as the release (after the step that makes the source archive).
    tail = [step.get("run") for step in jobs["windows"]["steps"][-3:]]
    assert tail == [step.get("run") for step in release["jobs"]["windows"]["steps"][-4:-1]]
    assert jobs["update"]["strategy"] == release["jobs"]["update"]["strategy"]
    assert jobs["update"]["steps"][-1]["run"] == release["jobs"]["update"]["steps"][-1]["run"]
    assert "git archive --format=tar.gz --prefix=tow/ -o tow-source.tar.gz HEAD" in _runs(jobs["update"])
    # Weekly and by hand (never on a pull request) every large selection scenario is measured;
    # the gate measures only the costliest of each kind.
    selection = jobs["selection"]
    assert selection["if"] == "github.event_name != 'pull_request'"
    assert selection["steps"][-1]["env"] == {"TOW_SELECTION_WORK": "all"}
    assert "tests/test_selection_work_bounds.py" in selection["steps"][-1]["run"]


def test_real_systems_run_on_their_code_weekly_and_by_hand_never_unbounded():
    """real.yml: the only place the tests/integration tests run (their variables are set nowhere
    else), on pull requests that touch what they test and when that reaches main, weekly and by
    hand; every job bounded."""
    text, flow = _workflow("real")
    assert flow["name"] == "real systems"
    triggers = flow[True]  # YAML 1.1 reads the key `on` as True
    assert triggers["schedule"]
    assert "workflow_dispatch" in triggers
    # Before, a change to the test helpers, the OS layer, the launchers or a client library in
    # uv.lock never ran these tests before the weekly run.
    assert set(triggers["pull_request"]["paths"]) == {
        "src/tow/clients/**",
        "src/tow/plugins.py",
        "src/tow/autostart/**",
        "src/tow/check/**",
        "src/tow/supervisor/**",
        "src/tow/torrent.py",
        "src/tow/folders.py",
        "src/tow/config.py",
        "src/tow/store.py",
        "src/tow/storage/**",
        "src/tow/bundle/**",  # the export the file system test publishes (C04)
        "src/tow/platform/**",
        "src/tow/paths.py",  # TC-08: the install's folders, which autostart reads
        "src/tow/lifecycle.py",  # read by `tow run` at start
        "src/tow/cli/**",
        "install/**",
        "scripts/build-bundle.py",
        "scripts/root_files.py",  # the bundle's start files the Windows autostart install uses
        "scripts/tow*",
        "tests/integration/**",
        "tests/helpers.py",
        "tests/conftest.py",
        "pyproject.toml",
        "uv.lock",
        ".python-version",
        ".github/workflows/real.yml",
        ".github/workflows/autostart.yml",
    }
    assert triggers["push"] == {"branches": ["main"], "paths": triggers["pull_request"]["paths"]}
    assert "paths: *real_paths" in text  # one list (a YAML anchor): a path added once is in both
    for path in triggers["pull_request"]["paths"]:  # no filter names a file that is gone
        assert next(ROOT.glob(path.removesuffix("/**")), None), path
    jobs = flow["jobs"]
    assert set(jobs) == {"clients", "clients-windows", "probes", "filesystems", "autostart"}
    # The autostart job lives in autostart.yml, which release.yml runs too (TC-03).
    assert jobs["autostart"]["uses"] == "./.github/workflows/autostart.yml"
    reusable_text, reusable = _workflow("autostart")
    autostart = reusable["jobs"]["autostart"]
    # The autostart job also updates under the OS manager and rolls back (TC-05): 70 minutes.
    bounded = ((jobs["clients"], 40), (jobs["clients-windows"], 40), (jobs["probes"], 20), (jobs["filesystems"], 25))
    for job, limit in (*bounded, (autostart, 70)):
        assert job["timeout-minutes"] <= limit
        assert all(step.get("timeout-minutes") for step in job["steps"] if "pytest" in step.get("run", ""))
    assert jobs["clients"]["runs-on"] == "ubuntu-24.04"
    assert jobs["clients"]["env"]["TOW_REAL_CLIENTS"] == "1"
    assert "tests/integration/test_real_clients.py" in _runs(jobs["clients"])
    # TC-14: systemd of both Ubuntu LTS versions.
    assert set(autostart["strategy"]["matrix"]["os"]) == {
        "windows-latest",
        "macos-latest",
        "ubuntu-24.04",
        "ubuntu-26.04",
    }
    assert autostart["env"]["TOW_REAL_AUTOSTART"] == "1"
    assert autostart["env"]["TOW_REAL_AUTOSTART_PORT"] != "8787"
    assert "tests/integration/test_real_autostart.py" in _runs(autostart)
    # Autostart is registered only for a runtime install made as the owner makes one.
    assert "install/install.sh --dir" in _runs(autostart)
    assert "install/install.ps1 -Dir" in _runs(autostart)
    # The variables that let these tests through the guard are set by these workflows only.
    for other in ("ci", "installers", "release"):
        assert "TOW_REAL_" not in _workflow(other)[0], other
    assert "8787" not in text
    assert "8787" not in reusable_text


def test_a_release_waits_for_the_real_autostart_of_its_commit_and_never_for_a_skip():
    """TC-03: real.yml was optional and passed with a skip; a release never waited for it. The
    autostart job is one reusable workflow: the release runs it for the commit it checked, and
    publishes only after it passed on all three systems; there and weekly, a runner that cannot
    run it fails instead of skipping."""
    _text, flow = _workflow("autostart")
    call = flow[True]["workflow_call"]  # YAML 1.1 reads the key `on` as True
    assert set(flow[True]) == {"workflow_call"}
    assert call["inputs"]["ref"] == {
        "description": "The commit to install and test",
        "required": True,
        "type": "string",
    }
    assert call["inputs"]["strict"]["type"] == "boolean"
    assert call["inputs"]["strict"]["default"] is False
    job = flow["jobs"]["autostart"]
    assert job["steps"][0]["with"] == {"ref": "${{ inputs.ref }}", "persist-credentials": False}
    systemd = next(step for step in job["steps"] if "loginctl enable-linger" in step.get("run", ""))
    assert systemd["env"] == {"STRICT": "${{ inputs.strict }}"}
    run = systemd["run"]
    assert run.index('if [ "$STRICT" = true ]; then') < run.index("exit 1") < run.index("TOW_REAL_AUTOSTART_SKIP=")
    release = _workflow("release")[1]["jobs"]
    assert release["autostart"] == {
        "needs": ["source"],
        "uses": "./.github/workflows/autostart.yml",
        "with": {"ref": "${{ needs.source.outputs.commit }}", "strict": True},
    }
    assert "autostart" in release["publish"]["needs"]
    real = _workflow("real")[1]["jobs"]["autostart"]
    assert real["with"] == {"ref": "${{ github.sha }}", "strict": "${{ github.event_name == 'schedule' }}"}


def test_real_clients_run_when_the_client_discovery_changes():
    """The real torrent clients are loaded through the shared plugin scan (tow.plugins), which
    lives outside src/tow/clients: a change to it runs real.yml too."""
    from tow import plugins
    from tow.clients import spec

    assert spec.plugins is plugins  # the discovery of the client modules
    paths = _workflow("real")[1][True]["pull_request"]["paths"]
    assert Path(plugins.__file__).resolve().relative_to(ROOT).as_posix() in paths


def test_ci_keeps_the_required_check_names_and_audits_once():
    _text, flow = _workflow("ci")
    jobs = flow["jobs"]
    # Branch protection requires these names: gate (<os>) x4 and install.sh (<os>) x3.
    assert jobs["gate"]["name"] == "gate (${{ matrix.os }})"
    assert jobs["install"]["name"] == "install.sh (${{ matrix.os }})"
    assert jobs["gate"]["strategy"]["matrix"]["os"] == [
        "windows-latest",
        "ubuntu-latest",
        "ubuntu-26.04",
        "macos-latest",
    ]
    assert jobs["install"]["strategy"]["matrix"]["os"] == ["ubuntu-latest", "ubuntu-26.04", "macos-latest"]
    # uv.lock is the same on every OS: pip-audit runs on one runner only.
    audited = [row["os"] for row in jobs["gate"]["strategy"]["matrix"]["include"] if row.get("audit")]
    assert audited == ["ubuntu-latest"]
    assert "${{ matrix.audit && '-Audit' || '' }}" in _runs(jobs["gate"])


def test_ci_starts_the_windows_bundle_on_every_commit():
    """TC-01: Windows, the owner's system, had its bundle started only in path-filtered or tag
    workflows. ci.yml (no path filter) builds it once and runs the start/offline/move smoke of
    the release on it; a docs-only pull request reports the check without the work."""
    _text, flow = _workflow("ci")
    _release_text, release = _workflow("release")
    job = flow["jobs"]["bundle"]
    assert job["name"] == "Windows bundle smoke"  # the check's name: branch protection may require it
    assert job["runs-on"] == "windows-latest"
    assert "strategy" not in job
    runs = [step.get("run", "") for step in job["steps"]]
    assert sum("scripts/build-bundle.py" in run for run in runs) == 1  # one build
    smoke = "./scripts/bundle-smoke.ps1 -Zip dist/TOW-windows-x64.zip -Offline -Move"
    assert runs[-1] == smoke
    assert smoke in _runs(release["jobs"]["windows"])  # the release's own smoke
    assert 'git archive --format=tar.gz --prefix=tow/ -o "$RUNNER_TEMP/tow-source.tar.gz" HEAD' in runs
    assert job["timeout-minutes"] <= 30


def test_a_docs_only_pull_request_skips_the_heavy_work_but_reports_every_check():
    """Every pull request ran the gate on four systems and install.sh on three, docs-only ones too.
    No path filter (a required check it never started would stay pending): the jobs keep their
    names and always report; their steps do the work unless the change is docs only, and the
    ubuntu-latest gate always does (the docs have tests). Pushes and runs by hand run everything."""
    _text, flow = _workflow("ci")
    triggers = flow[True]  # YAML 1.1 reads the key `on` as True
    assert triggers["pull_request"] is None  # every pull request, no paths or paths-ignore
    jobs = flow["jobs"]
    changes = jobs["changes"]
    assert changes["outputs"] == {"code": "${{ steps.diff.outputs.code }}"}
    assert changes["steps"][0]["with"]["fetch-depth"] == 0
    diff = changes["steps"][-1]
    assert 'if [ "$EVENT" != "pull_request" ]; then' in diff["run"]
    assert 'git diff --name-only "$BASE" HEAD' in diff["run"]
    assert diff["env"]["BASE"] == "${{ github.event.pull_request.base.sha }}"
    assert "github.event.pull_request" not in diff["run"]  # through env only: no expression in a script
    for job_id in ("gate", "install", "bundle"):
        job = jobs[job_id]
        assert job["needs"] == "changes"
        assert job["if"] == "${{ !cancelled() }}"  # also when `changes` failed: then everything runs
        assert "!= 'false'" in job["env"]["RUN"]
        assert "if" not in job["steps"][0]  # the checkout
        assert all(step["if"].startswith("env.RUN == 'true'") for step in job["steps"][1:]), job_id
    assert "matrix.os == 'ubuntu-latest'" in jobs["gate"]["env"]["RUN"]
    assert "ubuntu-latest" not in jobs["install"]["env"]["RUN"]


def test_dependabot_waits_a_week_and_groups_the_actions():
    """A freshly published (possibly compromised, then yanked) version was proposed the same
    week, and every action came in a pull request of its own, each running the full matrix."""
    import yaml

    updates = yaml.safe_load((ROOT / ".github" / "dependabot.yml").read_text(encoding="utf-8"))["updates"]
    by_kind = {entry["package-ecosystem"]: entry for entry in updates}
    assert set(by_kind) == {"github-actions", "uv"}
    for entry in updates:
        assert entry["cooldown"]["default-days"] >= 7
    assert by_kind["github-actions"]["groups"] == {"actions": {"patterns": ["*"]}}
    # uv_build follows the bundle's uv by hand (test_bundle: the uv the bundle carries builds TOW).
    assert by_kind["uv"]["ignore"] == [{"dependency-name": "uv-build"}]


def test_ci_cancels_only_a_pull_requests_older_run():
    """Two merges close together cancelled the first one's run on main."""
    flow = _workflow("ci")[1]
    assert flow["concurrency"]["cancel-in-progress"] == "${{ github.event_name == 'pull_request' }}"


def test_ci_lets_the_gate_stop_a_hung_test_run_itself():
    """The job's limit equalled the gate's 30-minute deadline for the test run alone: GitHub ended
    the job first, and the gate never named and stopped the hung processes."""
    gate = (ROOT / "scripts" / "gate.ps1").read_text(encoding="utf-8")
    deadline = re.search(r"\$PytestDeadlineSec = .* else \{ (\d+) \}", gate)
    assert deadline is not None
    job = _workflow("ci")[1]["jobs"]["gate"]
    assert job["timeout-minutes"] * 60 >= int(deadline.group(1)) + 10 * 60
    assert not any("TOW_GATE_PYTEST_DEADLINE_SEC" in str(step.get("env", {})) for step in job["steps"])


def test_ci_compiles_the_base_python_files_with_a_real_python_3_11():
    # ast.parse(feature_version=(3, 11)) accepts f"{d["a"]}" (3.12), which Python 3.11 refuses.
    import tomllib

    _text, flow = _workflow("ci")
    gate = flow["jobs"]["gate"]
    rows = [row["os"] for row in gate["strategy"]["matrix"]["include"] if row.get("py311")]
    assert rows == ["ubuntu-latest"]  # one OS: syntax does not depend on it
    step = next(step for step in gate["steps"] if step.get("if") == "env.RUN == 'true' && matrix.py311")
    assert "uv python install 3.11" in step["run"]
    compiled = re.search(r'"\$\(uv python find --no-project 3\.11\)" -m py_compile (.+)', step["run"])
    assert compiled is not None
    # The files ruff keeps at 3.11 syntax are the ones Python 3.11 compiles.
    ruff = tomllib.loads((ROOT / "ruff.toml").read_text(encoding="utf-8"))
    assert set(compiled.group(1).split()) == set(ruff["per-file-target-version"])
    assert set(ruff["per-file-target-version"].values()) == {"py311"}
    # Compiling proves the syntax only: the updater's harness (import, unpack, uv's environment,
    # retried renames, recovery) runs on that Python 3.11 alone, without TOW or pytest.
    assert '"$(uv python find --no-project 3.11)" -I tests/updater_stdlib.py' in step["run"].splitlines()[-1]
    harness = (ROOT / "tests" / "updater_stdlib.py").read_text(encoding="utf-8")
    assert not re.search(r"^\s*(import|from)\s+(tow|pytest)\b", harness, re.MULTILINE)


def test_the_release_workflow_tests_everything_before_it_uploads():
    _text, flow = _workflow("release")
    jobs = flow["jobs"]
    # The gate is not run again: the tag must be on origin/main and its commit must have passed ci.
    assert "gate" not in jobs
    assert jobs["source"]["permissions"] == {"contents": "read", "actions": "read"}
    source = _runs(jobs["source"])
    assert 'git merge-base --is-ancestor "$commit" refs/remotes/origin/main' in source
    assert '[ "$message" = "TOW ${TAG#v}" ]' in source
    assert "actions/workflows/ci.yml/runs?head_sha=$COMMIT" in source
    assert "completed/success" in source
    # Any ci run of the same content counts (the pull request's): the run on main is not awaited.
    assert 'TREE=$(git rev-parse "$COMMIT^{tree}")' in source
    assert "select(.head_commit.tree_id == env.TREE)" in source
    assert jobs["source"]["steps"][0]["with"]["fetch-depth"] == 0
    # Write access only where the release is touched, and only after every test passed.
    writers = [name for name, job in jobs.items() if job.get("permissions", {}).get("contents") == "write"]
    assert writers == ["publish"]
    assert jobs["publish"]["permissions"] == {"contents": "write"}
    assert set(jobs["publish"]["needs"]) == {"source", "windows", "posix", "update", "smokes", "autostart"}
    for job_id in ("windows", "posix", "update", "autostart"):
        assert jobs[job_id]["needs"] == ["source"]
    windows = _runs(jobs["windows"])
    assert "scripts/build-bundle.py --out dist --source tow-source.tar.gz" in windows
    assert "bundle-smoke.ps1 -Zip dist/TOW-windows-x64.zip -Offline" in windows
    assert "install-smoke.ps1 -Zip dist/TOW-windows-x64.zip" in windows
    assert jobs["posix"]["strategy"]["matrix"]["os"] == ["ubuntu-latest", "ubuntu-26.04", "macos-latest"]
    assert "scripts/install-smoke.sh" in jobs["posix"]["steps"][-1]["run"]
    assert "scripts/update-smoke.py --previous" in _runs(jobs["update"])


def test_the_release_stays_a_draft_until_every_asset_is_read_back():
    _text, flow = _workflow("release")
    publish = _runs(flow["jobs"]["publish"])
    assert "sha256sum TOW-windows-x64.zip install.ps1 install.sh tow-source.tar.gz > SHA256SUMS" in publish
    create = next(line for line in publish.splitlines() if "gh release create" in line)
    assert "--draft" in create
    assert "--verify-tag" in create
    order = [
        publish.index("gh release upload"),
        publish.index('[ "$(names)" = "$expected" ] || {'),
        publish.index("cmp readback/SHA256SUMS assets/SHA256SUMS"),
        publish.index("--draft=false"),
    ]
    assert order == sorted(order)
    assert "sha256sum -c SHA256SUMS" in publish
    # A public release's files are never replaced; its notes and title are never edited.
    assert publish.index("is public and complete") < publish.index("gh release upload")
    for line in publish.splitlines():
        if "gh release edit" in line:
            assert "--notes" not in line
            assert "--title" not in line


def test_the_windows_installer_smoke_covers_powershell_5_and_the_data():
    text = (ROOT / "scripts" / "install-smoke.ps1").read_text(encoding="utf-8")
    assert "powershell.exe -NoProfile -ExecutionPolicy Bypass -File $installer" in text  # 5.1
    assert "-Uninstall -Yes -Purge" in text
    assert "the reinstall replaced keys\\master.key" in text


def test_a_passed_bundle_smoke_ends_with_exit_0():
    # CI 16.10.2026: -ExpectTooLong passed, but the refused start file's exit code 3 stayed in
    # $LASTEXITCODE, and a pwsh CI step ends with it: the step failed after "passed".
    code = [line.strip() for line in _code_lines((ROOT / "scripts" / "bundle-smoke.ps1").read_text(encoding="utf-8"))]
    code = [line for line in code if line]
    assert code[-2:] == ["if ($failed) { throw $failed }", "exit 0"]
    # The refusal run ends early: with exit 0 too (a return ended the script with that code).
    refused = code.index("Step 'passed: refused before setup, nothing written'")
    assert code[refused + 1] == "exit 0"


@pytest.mark.allow_system
@pytest.mark.parametrize("name", ["bundle-smoke.ps1", "install-smoke.ps1"])
def test_the_windows_smoke_scripts_parse(name):
    program = shutil.which("pwsh")
    if program is None:
        pytest.skip("no pwsh on this machine")
    script = (
        "$errors = $null; "
        f"[void][System.Management.Automation.Language.Parser]::ParseFile('{ROOT / 'scripts' / name}', [ref]$null,"
        " [ref]$errors); $errors | ForEach-Object { $_.ToString() }; if ($errors) { exit 1 }"
    )
    done = subprocess.run(
        [program, "-NoProfile", "-NonInteractive", "-Command", script], capture_output=True, text=True, check=False
    )
    assert done.returncode == 0, done.stdout + done.stderr


@pytest.mark.allow_system
def test_the_bundle_smoke_ignores_runner_noise_but_not_ours(tmp_path):
    # CI: a Mozilla folder appeared in AppData during the smoke, which failed as "the bundle
    # wrote outside its folder". Only names TOW, uv or Python could make count there.
    program = shutil.which("pwsh")
    if program is None:
        pytest.skip("no pwsh on this machine")
    probe = tmp_path / "probe.ps1"
    probe.write_text(
        "$ast = [Management.Automation.Language.Parser]::ParseFile($args[0], [ref]$null, [ref]$null)\n"
        "$functions = $ast.FindAll({ $args[0] -is [Management.Automation.Language.FunctionDefinitionAst] }, $true)\n"
        "foreach ($f in $functions) {\n"
        "  if ($f.Name -in 'Test-Ours', 'Compare-Outside') { . ([scriptblock]::Create($f.Extent.Text)) } }\n"
        "$installName = 'tow smoke abc123'\n"
        "$before = [ordered]@{ 'uv' = 3; 'entries of R' = 'A|B'; 'entries of L' = 'X' }\n"
        "function Probe([int]$uv, [string]$r, [string]$l) {\n"
        "  $after = [ordered]@{ 'uv' = $uv; 'entries of R' = $r; 'entries of L' = $l }\n"
        "  '=' + ((Compare-Outside $before $after) -join ',') }\n"
        "Probe 3 'A|B|Mozilla' ''\n"
        "Probe 3 'A|B|Python' 'X'\n"
        "Probe 3 'A|B' 'X|old tow smoke ABC123'\n"
        "Probe 5 'A|B' 'X'\n",
        encoding="utf-8",
    )
    done = subprocess.run(
        [program, "-NoProfile", "-NonInteractive", "-File", str(probe), str(ROOT / "scripts" / "bundle-smoke.ps1")],
        capture_output=True,
        text=True,
        check=False,
    )
    assert done.returncode == 0, done.stdout + done.stderr
    lines = done.stdout.splitlines()
    assert "  ignored as runner noise in R: +Mozilla" in lines
    assert "  ignored as runner noise in L: -X" in lines
    assert [line for line in lines if line.startswith("=")] == ["=", "=entries of R", "=entries of L", "=uv"]


@pytest.mark.allow_system
def test_the_posix_install_smoke_ignores_runner_noise_in_home():
    shell = shutil.which("dash") or shutil.which("sh")
    if shell is None:
        pytest.skip("no sh on this machine")
    text = (ROOT / "scripts" / "install-smoke.sh").read_text(encoding="utf-8")
    assert '"$(ls -A "$HOME" | grep -iE "$ours" |' in text  # only our names are compared
    found = re.search(r"^ours=.*?$.*?^noise\(\) \{.*?^\}$", text, re.MULTILINE | re.DOTALL)
    assert found is not None
    before, after = "a\\nb\\nX", "a\\nb\\nMozilla\\npython3\\ntow smoke 1"
    script = found.group(0) + f"\nnoise \"$(printf '{before}')\" \"$(printf '{after}')\"\n"
    done = subprocess.run([shell, "-c", script], capture_output=True, text=True, check=False)
    assert done.returncode == 0, done.stderr
    assert done.stdout.split() == ["Mozilla", "X"]


@pytest.mark.allow_system  # sh with the smoke's own helpers on a temp folder
def test_the_posix_install_smoke_gives_only_the_tested_programs_a_sentinel_temp(tmp_path):
    # TC-new: the POSIX smoke never saw uv or TOW write into the system's temporary folder; the
    # sentinel goes to the tested programs alone (the smoke's own tools keep the runner's).
    shell = shutil.which("dash") or shutil.which("sh")
    if shell is None:
        pytest.skip("no sh on this machine")
    text = (ROOT / "scripts" / "install-smoke.sh").read_text(encoding="utf-8")
    code = _code_lines(text)
    for line in code:
        if re.search(r'"\$(start|stop|installer)"|/app/scripts/tow" status', line) and "[ -x" not in line:
            assert line.lstrip().startswith("tested "), line
    assert 'TMPDIR="$sentinel" TMP="$sentinel" TEMP="$sentinel"' in text
    assert [line for line in code if line.startswith("temp_untouched ")] == [
        'temp_untouched "install, start, stop"',
        'temp_untouched "offline setup and start of the moved install"',
        'temp_untouched "reinstall, start, stop, uninstall"',
    ]
    assert text.index('temp_untouched "reinstall') > text.index('--uninstall --yes --purge --dir "$dir"')
    assert 'rm -rf "$dir" "$sums" "$sentinel"' in text
    helpers = re.search(r"^say\(\) .*?^healthy\(\)", text, re.MULTILINE | re.DOTALL)
    assert helpers is not None
    sentinel = tmp_path / "sentinel"
    sentinel.mkdir()
    script = (
        helpers.group(0).removesuffix("healthy()")
        + f'\nsentinel="{_posix(sentinel)}"\ntested sh -c \'printf "%s" "$TMPDIR" > "$0"\' "{_posix(tmp_path)}/seen"\n'
        + 'temp_untouched "first"\ntested sh -c \': > "$TMPDIR/uv-123.lock"\'\ntemp_untouched "second"\n'
    )
    done = subprocess.run([shell, "-c", script], capture_output=True, text=True, check=False, timeout=60)
    assert done.returncode == 1, done.stdout + done.stderr
    assert (tmp_path / "seen").read_text(encoding="utf-8") == _posix(sentinel)
    assert "FAILED: written into TMPDIR (second): uv-123.lock" in done.stderr


@pytest.mark.parametrize(("workflow", "job_id"), [("ci", "gate"), ("ci", "install"), ("release", "posix")])
def test_linux_runners_keep_both_lts_versions_without_renaming_required_checks(workflow, job_id):
    import yaml

    flow = yaml.safe_load((ROOT / ".github" / "workflows" / f"{workflow}.yml").read_text(encoding="utf-8"))
    job = flow["jobs"][job_id]
    matrix = job["strategy"]["matrix"]
    overrides = {row["os"]: row["runner"] for row in matrix.get("include", [])}
    assert overrides == {"ubuntu-latest": "ubuntu-24.04"}
    assert len(matrix["os"]) == len(set(matrix["os"]))
    actual = {overrides.get(label, label) for label in matrix["os"]}
    expected = {"ubuntu-24.04", "ubuntu-26.04", "macos-latest"}
    if job_id == "gate":
        expected.add("windows-latest")
    assert actual == expected
    assert job["runs-on"] == "${{ matrix.runner || matrix.os }}"
    prefix = "gate" if job_id == "gate" else "install.sh"
    assert job["name"] == f"{prefix} (${{{{ matrix.os }}}})"


def test_release_archive_and_publish_use_a_pinned_linux_runner():
    import yaml

    jobs = yaml.safe_load((ROOT / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8"))["jobs"]
    assert jobs["source"]["runs-on"] == jobs["publish"]["runs-on"] == "ubuntu-24.04"


def test_release_jobs_build_and_publish_the_commit_the_source_job_checked():
    # Before: each later job checked out the tag again (a tag moved in between built unchecked
    # code), uv's cache came from other runs, and every stable tag became "latest", an older
    # line's fix too.
    import yaml

    text = (ROOT / ".github" / "workflows" / "release.yml").read_text(encoding="utf-8")
    jobs = yaml.safe_load(text)["jobs"]
    assert jobs["source"]["outputs"] == {"commit": "${{ steps.tag.outputs.commit }}"}
    assert any(step.get("id") == "tag" and "GITHUB_OUTPUT" in step["run"] for step in jobs["source"]["steps"])
    checkouts = setups = 0
    called = {name: job for name, job in jobs.items() if "uses" in job}
    assert set(called) == {"autostart", "smokes"}  # reusable workflows: they check out the commit given
    for job in called.values():
        assert job["with"]["ref"] == "${{ needs.source.outputs.commit }}"
        assert "source" in job["needs"]
    for name, job in jobs.items():
        for step in job.get("steps", []):
            uses = step.get("uses", "")
            if uses.startswith("actions/checkout@"):
                checkouts += 1
                want = "${{ env.TAG }}" if name == "source" else "${{ needs.source.outputs.commit }}"
                assert step["with"]["ref"] == want, name
                if name != "source":
                    assert "source" in job["needs"], name
            if uses.startswith("astral-sh/setup-uv@"):
                setups += 1
                assert step["with"]["enable-cache"] is False, name
    assert checkouts == len(jobs) - len(called)
    assert setups == 2
    publish = "\n".join(step.get("run", "") for step in jobs["publish"]["steps"])
    assert "--latest=$latest" in publish
    assert "\nlatest=true\n" not in publish
    assert "sort -V | tail -n 1" in publish
    assert '[ "$highest" != "$TAG" ] || latest=true' in publish
    assert "git get-tar-commit-id" in text


def test_the_smokes_and_real_autostart_install_into_cyrillic_folders():
    """TC-06: Cyrillic folders (many owners have them) were used by the Windows bundle smoke only.
    The Windows PowerShell scripts stay ASCII and make the name from its code points."""
    sh = (ROOT / "scripts" / "install-smoke.sh").read_text(encoding="utf-8")
    assert 'dir="$base/tow smoke Тест $$"' in sh
    assert 'moved="$base/tow relocated Тест $$"' in sh
    ps1 = (ROOT / "scripts" / "install-smoke.ps1").read_text(encoding="utf-8")
    assert ps1.isascii()
    assert "$cyrillic = -join [char[]](0x0422, 0x0435, 0x0441, 0x0442)" in ps1
    assert '("TOW install $cyrillic "' in ps1
    assert "".join(map(chr, (0x0422, 0x0435, 0x0441, 0x0442))) == "Тест"
    smoke = (ROOT / "scripts" / "update-smoke.py").read_text(encoding="utf-8")
    assert 'self.folder = base / f"tow update smoke Тест {os.getpid()}"' in smoke
    runs = _runs(_workflow("autostart")[1]["jobs"]["autostart"])
    assert 'dir="$RUNNER_TEMP/tow autostart Тест"' in runs
    assert "('TOW autostart ' + (-join [char[]](0x0422, 0x0435, 0x0441, 0x0442)))" in runs


def test_the_smoke_scripts_never_use_the_live_port():
    names = ("bundle-smoke.ps1", "install-smoke.sh", "install-smoke.ps1", "update-smoke.py", "crash-smoke.py")
    names += ("alpine-smoke.sh", "macos-smoke.sh", "nonadmin-smoke.ps1")
    for name in names:
        text = (ROOT / "scripts" / name).read_text(encoding="utf-8")
        assert "never on 8787" in text
        assert "TOW_NO_BROWSER" in text
