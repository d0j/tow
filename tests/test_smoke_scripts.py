"""The smoke scripts the workflows run (scripts/*smoke*): what each kills, reads and replays.

They run on CI runners (`.github/workflows/smokes.yml`, `installers.yml`, `release.yml`); here
their pieces that need no real install: the crash smoke kills only processes of its own install,
the permissions smoke reads access rules by SID, the update smoke's entry-file layouts end with
the files this commit writes and its folder names are NFD or of an exact length when asked, and
the shell and PowerShell smokes parse.
"""

from __future__ import annotations

import importlib.util
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
