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


@pytest.mark.parametrize(("os_name", "reuse"), [("posix", True), ("nt", False)])
def test_the_crash_smokes_unrelated_listener_binds_past_time_wait_only_outside_windows(monkeypatch, os_name, reuse):
    # Before: the listener bound without SO_REUSEADDR, so on Linux and macOS the stopped server's
    # connections in TIME_WAIT made its bind fail ("Address already in use") with nothing listening.
    import os
    import types

    crash = _script("crash-smoke.py")
    calls: list[tuple] = []

    class Stop(Exception):
        pass

    class Socket:
        def setsockopt(self, *args):
            calls.append(("setsockopt", *args))

        def bind(self, address):
            calls.append(("bind", address))

        def listen(self, backlog):
            calls.append(("listen", backlog))

        def accept(self):
            raise Stop

    fake = types.SimpleNamespace(socket=Socket, SOL_SOCKET=1, SO_REUSEADDR=2)
    monkeypatch.setitem(sys.modules, "socket", fake)
    monkeypatch.setattr(os, "name", os_name)
    monkeypatch.setattr(sys, "argv", ["-c", "18959"])
    with pytest.raises(Stop):
        exec(crash.LISTENER, {"__name__": "listener"})  # noqa: S102 - the smoke's own child code
    expected = [("setsockopt", 1, 2, 1)] if reuse else []
    assert calls == [*expected, ("bind", ("127.0.0.1", 18959)), ("listen", 16)]


def test_the_permissions_smoke_reads_acls_without_powershell_7s_module_path(monkeypatch, tmp_path):
    # Before: a PSModulePath inherited from PowerShell 7 (the standard-account smoke runs under
    # pwsh) made Windows PowerShell load PowerShell 7's Microsoft.PowerShell.Security: Get-Acl failed.
    import subprocess

    permissions = _script("permissions-smoke.py")
    seen: dict = {}

    def run(argv, *, env=None, timeout=120):
        seen.update(argv=argv, env=env)
        return subprocess.CompletedProcess(
            argv, 0, stdout='{"owner": "S-1-5-18", "rules": {"sid": "S-1-5-18"}}', stderr=""
        )

    monkeypatch.setattr(permissions, "_run", run)
    monkeypatch.setenv("PSModulePath", r"C:\Program Files\PowerShell\7\Modules")
    acl = permissions.windows_acl(tmp_path)
    assert seen["argv"][0] == "powershell.exe"
    assert not {name.upper() for name in seen["env"]} & {"PSMODULEPATH"}
    assert seen["env"]["TOW_SMOKE_ACL_PATH"] == str(tmp_path)
    assert acl["rules"] == [{"sid": "S-1-5-18"}]


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
def test_the_rosetta_smoke_reads_the_interpreter_not_its_config_script(tmp_path):
    # Before: the first bin/python3* file was taken, which can be python3.X-config (a shell
    # script), and `lipo -archs` stopped the Rosetta leg with "can't figure out the architecture".
    import re
    import shutil
    import subprocess

    shell = shutil.which("sh")
    if shell is None:
        pytest.skip("no sh on this machine")
    text = (SCRIPTS / "macos-smoke.sh").read_text(encoding="utf-8")
    line = re.search(r"^\s*(python=\$\(find .*\))$", text, re.MULTILINE)
    assert line is not None
    bin_dir = tmp_path / "runtime" / "python" / "cpython-3.14.8-macos-aarch64-none" / "bin"
    bin_dir.mkdir(parents=True)
    for name in ("python3.14-config", "python3-config", "python3.14"):
        (bin_dir / name).write_text("#!/bin/sh\n", encoding="utf-8")
    script = f'dir="{tmp_path.as_posix()}"\n{line.group(1)}\nprintf "%s" "$python"\n'
    done = subprocess.run([shell, "-c", script], capture_output=True, text=True, check=False)
    assert done.returncode == 0, done.stderr
    assert done.stdout.rsplit("/", 1)[-1] == "python3.14"
    assert '|| fail "lipo cannot read $python"' in text  # an unreadable file says so


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
    # A new account's profile may lack AppData: GetFolderPath answered "" and stopped the run.
    assert "[Environment]::GetFolderPath($Name, [Environment+SpecialFolderOption]::Create)" in text
    assert "CurrentVersion\\ProfileList\\$($report.sid)" in text
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


def _netns_views():
    """What the D01 smoke's clients report when TOW tells the two stacks apart as it should."""
    clean = {"status": 200, "markers": []}
    network = {
        "page": {"status": 303, "location": "/login", "session": ""},
        "json": {"status": 401, "location": "", "session": ""},
        "wrong": 401,
        "signin": [
            {"status": 303, "location": "/", "session": "s1"},
            {"status": 303, "location": "/", "session": "s2"},
        ],
        "sessions": [True, True, True],
        "signed_in": 200,
        "surfaces": {"/": dict(clean), "/log.json": dict(clean)},
        "local_only": {"status": 303, "location": "/settings?open=access", "session": ""},
        "after_signout": {"signed_out": {"status": 303, "location": "/login?again=1", "session": ""}, "other": 200},
    }
    local = {"page": 200, "surfaces": {"/": {"status": 200, "markers": ["e01.mkv"]}, "/log.json": dict(clean)}}
    return network, local


def test_the_netns_smoke_passes_only_two_views_told_apart():
    """D01: the network sees the sign-in page, its own sessions and no path or key; this computer
    sees the planted error."""
    smoke = _script("netns-smoke.py")
    network, local = _netns_views()
    assert smoke.judge(network, local) == []
    broken = [
        ("page", {"status": 200, "location": "", "session": ""}, "sign-in page"),
        ("sessions", [True, True, False], "two sessions"),
        ("local_only", {"status": 200, "location": "", "session": ""}, "was not refused"),
        ("surfaces", {"/": {"status": 200, "markers": ["/home/owner-netns"]}}, "must not"),
    ]
    for key, value, said in broken:
        changed, _ = _netns_views()
        changed[key] = value
        assert any(said in problem for problem in smoke.judge(changed, local)), key
    changed, _ = _netns_views()
    changed["after_signout"]["other"] = 303
    assert any("the other out too" in problem for problem in smoke.judge(changed, local))
    _, unplanted = _netns_views()
    unplanted["surfaces"]["/"]["markers"] = []
    assert any("was not planted" in problem for problem in smoke.judge(network, unplanted))
    _, keys = _netns_views()
    keys["surfaces"]["/"]["markers"] = ["e01.mkv", "SECRETUK"]
    assert smoke.judge(network, keys) == []  # this computer sees every word of a row's error
    changed, _ = _netns_views()
    changed["surfaces"]["/log.json"]["markers"] = ["SECRETPK"]
    assert any("must not" in problem for problem in smoke.judge(changed, local))


def test_the_netns_smoke_runs_its_programs_as_this_account_with_a_minimal_environment(monkeypatch):
    smoke = _script("netns-smoke.py")
    monkeypatch.setenv("TOW_ROOT", "/somewhere")
    monkeypatch.setenv("UV_CACHE_DIR", "/cache")
    env = smoke.minimal_env()
    assert not [name for name in env if name.startswith(("TOW_ROOT", "UV_"))]
    assert env["TOW_NO_BROWSER"] == "1"
    argv = smoke.as_me_in("tow-a-1", ["/x/tow", "start"], {"HOME": "/home/runner"}, ids=(1001, 118))
    assert argv[:6] == ["sudo", "-n", "ip", "netns", "exec", "tow-a-1"]
    assert argv[6] == "setpriv"
    assert argv[7:10] == ["--reuid=1001", "--regid=118", "--init-groups"]
    assert argv[-5:] == ["env", "-i", "HOME=/home/runner", "/x/tow", "start"]
    # B reaches A by a private address: TOW's Host check lets it in, its peer is not loopback.
    import ipaddress

    for address in (smoke.ADDRESS_A, smoke.ADDRESS_B):
        assert ipaddress.ip_address(address).is_private
        assert not ipaddress.ip_address(address).is_loopback


def test_the_netns_smoke_refuses_8787_and_other_systems(monkeypatch, capsys):
    smoke = _script("netns-smoke.py")
    with pytest.raises(SystemExit):
        smoke.main(["--source", "x.tar.gz", "--port", "8787"])
    assert "never on 8787" in capsys.readouterr().err
    monkeypatch.setattr(smoke.sys, "platform", "win32")
    with pytest.raises(SystemExit):
        smoke.main(["--source", "x.tar.gz"])
    assert "need Linux" in capsys.readouterr().err
