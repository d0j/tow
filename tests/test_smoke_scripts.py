"""The smoke scripts the workflows run (scripts/*smoke*): what each kills, reads and replays.

They run on CI runners (`.github/workflows/smokes.yml`, `installers.yml`, `release.yml`); here
their pieces that need no real install: the crash smoke kills only processes of its own install,
the permissions smoke reads access rules by SID, the update smoke's entry-file layouts end with
the files this commit writes and its folder names are NFD or of an exact length when asked, and
the shell and PowerShell smokes parse.
"""

from __future__ import annotations

import base64
import importlib.util
import json
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
SCRIPTS = ROOT / "scripts"


def _script(name: str):
    spec = importlib.util.spec_from_file_location(name.replace("-", "_").removesuffix(".py"), SCRIPTS / name)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


def test_the_update_smoke_replays_every_entry_file_layout_ending_with_this_commits():
    """TC-16: the newest layout of each system is what this commit writes; the fixture's layouts are
    distinct, oldest first, and the Windows ones are files cmd.exe resumes in."""
    data = json.loads((SCRIPTS / "update-smoke-entry-files.json").read_text(encoding="utf-8"))
    root_files = _script("root_files.py")
    for kind in ("windows", "posix"):
        layouts = data[kind]
        assert len({layout["sha256"] for layout in layouts}) == len(layouts) >= 3, kind
        assert layouts[0]["first"] == "v1.22.0", kind
    newest_windows = base64.b64decode(data["windows"][-1]["base64"])
    assert newest_windows == root_files.rendered()["Update TOW.cmd"]
    for layout in data["windows"]:
        assert root_files.resume_offset(base64.b64decode(layout["base64"])) is not None, layout["first"]
    install_sh = (ROOT / "install" / "install.sh").read_text(encoding="utf-8")
    heredoc = install_sh.split("cat >\"$update_file\" <<'EOF'\n", 1)[1].split("\nEOF\n", 1)[0] + "\n"
    assert base64.b64decode(data["posix"][-1]["base64"]).decode("utf-8") == heredoc
    for layout in data["posix"]:
        text = base64.b64decode(layout["base64"]).decode("utf-8")
        assert text.startswith("#!/bin/sh\n"), layout["first"]
        assert '--ref "${1:-latest}"' in text, layout["first"]


def test_the_update_smoke_names_nfd_and_exact_length_folders(tmp_path):
    smoke = _script("update-smoke.py")
    nfd = smoke.Smoke(tmp_path, tmp_path, 18960, tmp_path, nfd=True)
    assert "й" in nfd.folder.name  # и + combining breve, not the composed й
    assert "й" not in nfd.folder.name
    sized = smoke.Smoke(tmp_path, tmp_path, 18960, tmp_path, root_length=len(str(tmp_path)) + 120)
    assert len(str(sized.root)) == len(str(tmp_path)) + 120
    with pytest.raises(smoke.SmokeError):
        smoke.Smoke(tmp_path, tmp_path, 18960, tmp_path, root_length=10)


def test_the_candidate_release_carries_its_version_and_checksum(tmp_path):
    import hashlib
    import io
    import tarfile

    smoke = _script("update-smoke.py")
    source = tmp_path / "tow-source.tar.gz"
    with tarfile.open(source, "w:gz") as archive:
        for name, data in (
            ("tow/pyproject.toml", b'[project]\nname = "tow"\nversion = "1.33.1"\n'),
            ("tow/a.txt", b"a"),
        ):
            info = tarfile.TarInfo(name)
            info.size = len(data)
            archive.addfile(info, io.BytesIO(data))
    smoke.candidate_release(source, tmp_path / "release", "1.99.3")
    built = tmp_path / "release" / "tow-source.tar.gz"
    with tarfile.open(built) as archive:
        assert sorted(archive.getnames()) == ["tow-1.99.3/a.txt", "tow-1.99.3/pyproject.toml"]
        member = archive.extractfile("tow-1.99.3/pyproject.toml")
        assert member is not None
        assert b'version = "1.99.3"' in member.read()
    sums = (tmp_path / "release" / "SHA256SUMS").read_text(encoding="ascii")
    assert sums == f"{hashlib.sha256(built.read_bytes()).hexdigest()}  tow-source.tar.gz\n"


def test_the_crash_smoke_kills_only_processes_of_its_install(tmp_path, monkeypatch):
    crash = _script("crash-smoke.py")
    root = tmp_path / "tow crash smoke Тест 1" / "TOW"
    inside = str(root / "runtime" / "python" / "python")
    monkeypatch.setattr(crash, "program", lambda pid: {1: inside, 2: str(tmp_path / "other" / "python"), 3: ""}[pid])
    assert crash.ours(1, root)
    assert not crash.ours(2, root)
    assert not crash.ours(3, root)  # unknown is never ours
    killed = []
    monkeypatch.setattr(crash.os, "kill", lambda pid, sig: killed.append(pid))
    monkeypatch.setattr(crash, "_kernel32", lambda: pytest.fail("no process may be opened"))
    with pytest.raises(crash.SmokeError, match="not a process of"):
        crash.kill_hard(2, root, "tow run")
    assert killed == []


def test_the_permissions_smoke_reads_sids_not_names():
    permissions = _script("permissions-smoke.py")
    user = "S-1-5-21-1-2-3-1001"
    acl = {
        "owner": user,
        "protected": True,
        "rules": [
            {"sid": user, "type": "Allow"},
            {"sid": "S-1-5-18", "type": "Allow"},
            {"sid": "S-1-5-32-544", "type": "Allow"},
            {"sid": "S-1-3-4", "type": "Allow"},
            {"sid": "S-1-1-0", "type": "Deny"},  # a deny rule lets no one in
        ],
    }
    assert permissions.windows_others(acl, user) == []
    acl["rules"].append({"sid": "S-1-5-32-545", "type": "Allow"})
    acl["rules"].append({"sid": "S-1-5-11", "type": "Allow"})
    assert permissions.windows_others(acl, user) == ["S-1-5-11", "S-1-5-32-545"]
    assert "GetAccessRules($true, $true, [Security.Principal.SecurityIdentifier])" in permissions.ACL_SCRIPT


@pytest.mark.allow_system
def test_the_new_shell_smokes_parse():
    import shutil
    import subprocess

    shell = shutil.which("dash") or shutil.which("sh")
    if shell is None:
        pytest.skip("no sh on this machine")
    for name in ("alpine-smoke.sh", "macos-smoke.sh", "install-smoke.sh"):
        done = subprocess.run([shell, "-n", str(SCRIPTS / name)], capture_output=True, text=True, check=False)
        assert done.returncode == 0, name + done.stderr


@pytest.mark.allow_system
def test_the_nonadmin_smoke_parses_and_stays_ascii():
    import shutil
    import subprocess

    path = SCRIPTS / "nonadmin-smoke.ps1"
    text = path.read_text(encoding="utf-8")
    assert text.isascii()  # Windows PowerShell 5.1 reads a file without a BOM in the ANSI code page
    assert "-join [char[]](0x0422, 0x0435, 0x0441, 0x0442)" in text
    assert "if (-not $env:GITHUB_ACTIONS)" in text  # never creates an account outside CI
    assert "Remove-LocalUser" in text
    program = shutil.which("pwsh")
    if program is None:
        pytest.skip("no pwsh on this machine")
    script = (
        "$errors = $null; "
        f"[void][System.Management.Automation.Language.Parser]::ParseFile('{path}', [ref]$null, [ref]$errors); "
        "$errors | ForEach-Object { $_.ToString() }; if ($errors) { exit 1 }"
    )
    done = subprocess.run(
        [program, "-NoProfile", "-NonInteractive", "-Command", script], capture_output=True, text=True, check=False
    )
    assert done.returncode == 0, done.stdout + done.stderr


def test_install_smoke_builds_its_nfd_name_from_code_points():
    text = (SCRIPTS / "install-smoke.sh").read_text(encoding="utf-8")
    assert "nfd=\" $(printf '\\320\\270\\314\\206')\"" in text  # и + U+0306 in UTF-8
    assert 'dir="$base/tow smoke Тест$nfd $$"' in text
    assert '[ -z "$nfd" ] || moved="$base/tow relocated Тест$nfd $$"' in text
