"""Permissions smoke (TC-09): who may open a real install's folders after its install and start.

    python scripts/permissions-smoke.py --root <install> [--legs]

Read independently of TOW's own code (which reports and repairs them):

- Linux and macOS: the mode bits of the root (no write for every account), ``data/``, ``keys/``
  (0700: no group or other bit) and ``keys/master.key`` (no group or other bit), by ``os.stat``.
- Windows: the effective access rules of the root, ``data/``, ``keys/`` and ``keys/master.key`` as
  security identifiers (``Get-Acl`` with ``SecurityIdentifier``, inherited rules included), never by
  account names (a runner may be in another language): only this account, SYSTEM
  (``S-1-5-18``), Administrators (``S-1-5-32-544``), CREATOR OWNER and OWNER RIGHTS may be
  allowed in (whether a folder still inherits is recorded: TOW closes only a folder that was open).

With ``--legs`` two more cases run on the install, kept apart:

1. **repair**: a folder of this account that other accounts may open - on POSIX 0775; on Windows
   through what it inherits (an allow rule for Users ``S-1-5-32-545`` on the folder above the
   install, as a folder in a drive root inherits "Authenticated Users") - is closed again by the
   next TOW command (``tow status``), which says nothing about it;
2. **failure**: a folder TOW must not change - owned by another account (``sudo chown root`` on
   POSIX; on Windows owned by Administrators, with an explicit rule for Users, which TOW never
   removes) - stays open, and ``tow permissions status`` reports it open and not this account's; then
   it is given back and repaired.

The failure leg needs ``sudo -n`` (POSIX) or an elevated runner (Windows); without it the leg
fails under ``--strict`` and is reported as not run otherwise. Standard library only; writes
only inside the install and its parent folder (the throwaway folder that holds it).
"""

from __future__ import annotations

import argparse
import contextlib
import json
import os
import re
import stat
import subprocess
import sys
from pathlib import Path

WINDOWS = os.name == "nt"
SYSTEM_SID = "S-1-5-18"
ADMINISTRATORS_SID = "S-1-5-32-544"
USERS_SID = "S-1-5-32-545"
# CREATOR OWNER and OWNER RIGHTS: they stand for the owner itself, never another account.
PRIVATE_SIDS = frozenset({SYSTEM_SID, ADMINISTRATORS_SID, "S-1-3-0", "S-1-3-4"})
# The effective rules of one path as SIDs (Get-Acl reads them; GetAccessRules resolves no name).
ACL_SCRIPT = r"""
$ErrorActionPreference = 'Stop'
[Console]::OutputEncoding = [Text.Encoding]::UTF8
$acl = Get-Acl -LiteralPath $env:TOW_SMOKE_ACL_PATH
$rules = @($acl.GetAccessRules($true, $true, [Security.Principal.SecurityIdentifier]) | ForEach-Object {
  [ordered]@{ sid = $_.IdentityReference.Value; type = [string]$_.AccessControlType;
    rights = [string]$_.FileSystemRights; inherited = [bool]$_.IsInherited } })
$owner = (New-Object Security.Principal.NTAccount($acl.Owner)).Translate([Security.Principal.SecurityIdentifier]).Value
$value = [ordered]@{ owner = $owner; protected = [bool]$acl.AreAccessRulesProtected; rules = $rules }
$value | ConvertTo-Json -Depth 4 -Compress
"""


class PermissionsError(RuntimeError):
    pass


def say(text: str) -> None:
    print(f"permissions-smoke: {text}", flush=True)


def _run(argv: list[str], *, env: dict[str, str] | None = None, timeout: float = 120) -> subprocess.CompletedProcess:
    return subprocess.run(
        argv, capture_output=True, text=True, encoding="utf-8", errors="replace", env=env, timeout=timeout, check=False
    )


def user_sid() -> str:
    done = _run(["whoami", "/user", "/fo", "csv", "/nh"])
    found = re.search(r"S-1-[0-9-]+", done.stdout)
    if done.returncode != 0 or found is None:
        raise PermissionsError(f"whoami /user gave no SID: {done.stdout!r} {done.stderr!r}")
    return found.group(0)


def windows_acl(path: Path) -> dict:
    env = {**os.environ, "TOW_SMOKE_ACL_PATH": str(path)}
    done = _run(["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", ACL_SCRIPT], env=env)
    if done.returncode != 0:
        raise PermissionsError(f"Get-Acl {path}: {done.stderr.strip() or done.stdout.strip()}")
    value = json.loads(done.stdout)
    rules = value.get("rules") or []
    value["rules"] = [rules] if isinstance(rules, dict) else rules
    return value


def windows_others(acl: dict, user: str) -> list[str]:
    """The SIDs other than this account and the private ones an allow rule lets in."""
    allowed = PRIVATE_SIDS | {user}
    return sorted({rule["sid"] for rule in acl["rules"] if rule["type"] == "Allow" and rule["sid"] not in allowed})


def posix_mode(path: Path) -> int:
    return stat.S_IMODE(path.stat().st_mode)


def check(root: Path) -> list[str]:
    """What is wrong with the install's permissions (empty: private as TOW keeps them)."""
    data, keys, key = root / "data", root / "keys", root / "keys" / "master.key"
    problems = [f"{path} is missing" for path in (root, data, keys, key) if not path.exists()]
    if problems:
        return problems
    if WINDOWS:
        user = user_sid()
        for path in (root, data, keys, key):
            acl = windows_acl(path)
            others = windows_others(acl, user)
            say(f"{path.name or path}: owner {acl['owner']}, protected {acl['protected']}, others {others or 'none'}")
            if others:
                problems.append(f"{path}: allowed to {', '.join(others)}")
        return problems
    rules = ((root, 0o002, "no write for every account"), (data, 0o077, "0700"), (keys, 0o077, "0700"))
    for path, mask, wanted in (*rules, (key, 0o077, "0600")):
        mode = posix_mode(path)
        say(f"{path.name}: {mode:04o}")
        if mode & mask:
            problems.append(f"{path} is {mode:04o}, not {wanted}")
    return problems


# --- the repair and failure legs --------------------------------------------------------------


class Install:
    def __init__(self, root: Path):
        self.root = root
        self.launcher = root / "app" / "scripts" / ("tow.cmd" if WINDOWS else "tow")
        self.env = {k: v for k, v in os.environ.items() if not k.upper().startswith(("TOW_", "UV_"))}
        self.env.update(TOW_NO_BROWSER="1", UV_OFFLINE="1", PYTHONIOENCODING="utf-8")

    def tow(self, *args: str) -> subprocess.CompletedProcess:
        argv = [str(self.launcher), *args]
        if WINDOWS:
            argv = ["cmd.exe", "/d", "/c", "call", *argv]
        done = _run(argv, env=self.env, timeout=300)
        if done.returncode != 0:
            raise PermissionsError(
                f"tow {args[0]} exited with {done.returncode}: {done.stdout[-400:]}{done.stderr[-400:]}"
            )
        return done

    def status(self) -> subprocess.CompletedProcess:
        """A command that repairs the folders first (every command but `tow permissions`)."""
        return self.tow("status", "--json")

    def report(self, folder: str) -> dict:
        """`tow permissions status --json`: its row for ``folder`` (root, keys, data)."""
        value = json.loads(self.tow("permissions", "status", "--json").stdout)
        rows = [row for row in value.get("folders", []) if row.get("folder") == folder]
        if len(rows) != 1:
            raise PermissionsError(f"`tow permissions status` has no single {folder} row: {value}")
        return rows[0]


def _icacls(*args: str) -> None:
    done = _run(["icacls", *args, "/Q"])
    if done.returncode != 0:
        raise PermissionsError(f"icacls {' '.join(args)}: {done.stdout.strip()} {done.stderr.strip()}")


def _is_open(path: Path) -> bool:
    if WINDOWS:
        return USERS_SID in windows_others(windows_acl(path), user_sid())
    return bool(posix_mode(path) & 0o077)


def own(path: Path, *, recursive: bool = False) -> None:
    """This account owns ``path`` (an elevated runner creates what it unpacks as Administrators;
    an owner's own unpacked folder is theirs)."""
    if WINDOWS:
        _icacls(str(path), "/setowner", f"*{user_sid()}", *(["/T", "/C"] if recursive else []))


def leg_repair(install: Install) -> None:
    root, data = install.root, install.root / "data"
    if WINDOWS:
        base = root.parent
        _icacls(str(base), "/grant", f"*{USERS_SID}:(OI)(CI)RX")
        try:
            for path in (root, data):
                own(path)
                _icacls(str(path), "/inheritance:e")
            opened = [path for path in (root, data) if _is_open(path)]
            if opened != [root, data]:
                raise PermissionsError(
                    f"repair: the inherited rule did not open {root} and {data}: the leg tests nothing"
                )
            done = install.status()
        finally:
            _icacls(str(base), "/remove:g", f"*{USERS_SID}")
        closed = [root, data]
    else:
        data.chmod(0o775)
        if not _is_open(data):
            raise PermissionsError(f"{data} did not open: the leg tests nothing")
        done = install.status()
        closed = [data]
    still = [str(path) for path in closed if _is_open(path)]
    if still:
        raise PermissionsError(f"repair: still open after `tow status`: {still}")
    del done
    reported = [path.name for path in closed if install.report("root" if path == root else "data").get("open")]
    if reported:
        raise PermissionsError(f"repair: `tow permissions` still calls {reported} open")
    names = ", ".join(path.name for path in closed)
    say(f"1. repair: {names} of this account, open to others, closed by the next command")


def _give_away(path: Path) -> bool:
    """``path`` owned by another account (Administrators / root) and open to others; False when this
    account cannot do that."""
    if WINDOWS:
        if _run(["icacls", str(path), "/setowner", f"*{ADMINISTRATORS_SID}", "/Q"]).returncode != 0:
            return False
        _icacls(str(path), "/grant", f"*{USERS_SID}:(OI)(CI)RX")
        return True
    if _run(["sudo", "-n", "chown", "0", str(path)]).returncode != 0:
        return False
    _run(["sudo", "-n", "chmod", "0775", str(path)])
    return True


def _take_back(path: Path) -> None:
    if WINDOWS:
        _icacls(str(path), "/remove:g", f"*{USERS_SID}")
        own(path)
    else:
        _run(["sudo", "-n", "chown", str(os.getuid()), str(path)])  # type: ignore[attr-defined,unused-ignore]


def leg_failure(install: Install, *, strict: bool) -> None:
    data = install.root / "data"
    try:
        if not _give_away(data):
            text = "failure: this account cannot give a folder to another one (no sudo -n / not elevated)"
            if strict:
                raise PermissionsError(text)
            say(f"2. not run: {text}")
            return
        if not _is_open(data):
            raise PermissionsError(f"failure: {data} did not open: the leg tests nothing")
        install.status()
        if not _is_open(data):
            raise PermissionsError(f"failure: TOW changed {data}, which another account owns")
        row = install.report("data")
        if row.get("open") is not True or row.get("mine") is not False:
            raise PermissionsError(f"failure: `tow permissions` does not report data open and not ours: {row}")
        say("2. failure: data of another account stays open, and `tow permissions` reports it so")
    finally:
        with contextlib.suppress(OSError, subprocess.SubprocessError, PermissionsError):
            _take_back(data)
    install.status()
    if _is_open(data):
        raise PermissionsError(f"{data} given back to this account is still open after `tow status`")


def _utf8_output() -> None:
    for stream in (sys.stdout, sys.stderr):
        with contextlib.suppress(AttributeError, ValueError):
            stream.reconfigure(encoding="utf-8", errors="replace")


def main(argv: list[str] | None = None) -> int:
    _utf8_output()
    parser = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    parser.add_argument("--root", type=Path, required=True, help="the install's folder")
    parser.add_argument("--legs", action="store_true", help="also the repair and the failure legs")
    parser.add_argument("--strict", action="store_true", help="the failure leg must run")
    args = parser.parse_args(argv)
    root = args.root.absolute()  # as TOW names it (not resolved: macOS /var is a link)
    try:
        problems = check(root)
        if problems:
            raise PermissionsError("; ".join(problems))
        say("the root, data/, keys/ and keys/master.key are private")
        if args.legs:
            install = Install(root)
            leg_repair(install)
            leg_failure(install, strict=args.strict)
            problems = check(root)
            if problems:
                raise PermissionsError("after the legs: " + "; ".join(problems))
    except (PermissionsError, OSError, ValueError, subprocess.SubprocessError) as exc:
        say(f"FAILED: {exc}")
        return 1
    say("passed")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
