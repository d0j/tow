"""`tow permissions`: an install another account owns (created from an administrator terminal)
is given back to the owner once, in an administrator terminal, and closed as every start would.

Nothing here changes real permissions: the Windows backend runs against a fake icacls that keeps
each folder's owner and permissions in a dictionary."""

from __future__ import annotations

import os
import re
from pathlib import Path

import pytest

from tow import cli, permissions, platform
from tow.i18n import t
from tow.paths import launcher
from tow.platform import windows
from tow.platform.windows import WindowsBackend

OWNER = "S-1-5-21-1000000001-1000000002-1000000003-1001"  # the owner; autostart runs as it
ADMIN = "S-1-5-21-1000000001-1000000002-1000000003-1002"  # another administrator of the PC
NAMES = {OWNER: "PC\\owner", ADMIN: "PC\\admin", "S-1-5-32-544": "BUILTIN\\Administrators"}
# What C:\TOW made from an administrator terminal has: owner Administrators, inherited from C:\.
OPEN = "O:BAD:AI(A;OICIID;FA;;;BA)(A;OICIID;FA;;;SY)(A;OICIID;0x1200a9;;;BU)(A;OICIID;0x1301bf;;;AU)"


class FakeAcl:
    """Folders' owner and permissions; icacls /setowner and /inheritance:r /grant:r change
    them, and closing a folder passes its permissions on to what inherits inside it."""

    def __init__(self, folders: list[Path]) -> None:
        self.sddl = {str(folder): OPEN for folder in folders}
        self.calls: list[list[str]] = []

    def run(self, args: list[str], timeout: float = 20) -> str:
        self.calls.append(list(args))
        assert args[0].lower().endswith("\\system32\\icacls.exe")
        path = args[1]
        owner, dacl = re.fullmatch(r"O:(.+?)D:(.*)", self.sddl[path]).groups()
        if "/setowner" in args:
            owner = args[args.index("/setowner") + 1].lstrip("*")
        if "/inheritance:r" in args:
            sids = [grant.split(":")[0].lstrip("*") for grant in args if grant.startswith("*") and ":(" in grant]
            dacl = "PAI" + "".join(f"(A;OICI;FA;;;{sid})" for sid in sids)
            inherited = "AI" + "".join(f"(A;OICIID;FA;;;{sid})" for sid in sids)
            for other, text in self.sddl.items():
                if other != path and Path(other).is_relative_to(path) and "D:P" not in text:
                    self.sddl[other] = text.split("D:")[0] + "D:" + inherited
        self.sddl[path] = f"O:{owner}D:{dacl}"
        return "Successfully processed 1 files; Failed processing 0 files"

    def owner(self, path: Path) -> str:
        return self.sddl[str(path)].split("D:")[0][2:]


@pytest.fixture
def install(tmp_path, monkeypatch):
    root = tmp_path / "TOW"
    folders = [root, *(root / name for name in ("app", "runtime", "keys", "data"))]
    for folder in folders:
        folder.mkdir(parents=True, exist_ok=True)
    monkeypatch.setenv("TOW_ROOT", str(root))
    monkeypatch.setenv("TOW_HOME", str(root / "data"))
    acl = FakeAcl(folders)
    session = {"user": OWNER, "elevated": False, "task": "PC\\owner"}
    monkeypatch.setattr(windows, "_run", acl.run)
    monkeypatch.setattr(windows, "folder_security", lambda path: acl.sddl.get(str(path)))
    monkeypatch.setattr(windows, "user_sid", lambda: session["user"])
    # The account signed in to the Windows session: the terminal's own, unless a standard
    # account typed an administrator's password to open it ("console").
    monkeypatch.setattr(windows, "session_user_sid", lambda: session.get("console", session["user"]))
    monkeypatch.setattr(windows, "elevated", lambda: session["elevated"])
    monkeypatch.setattr(windows, "account_name", lambda sid: NAMES.get(sid, sid))
    monkeypatch.setattr(windows, "account_of", lambda name: {v: k for k, v in NAMES.items()}.get(name))
    monkeypatch.setattr(permissions, "_autostart_user", lambda: session["task"])
    with platform.use(WindowsBackend()):
        yield root, acl, session


def test_status_says_who_owns_the_open_folders_and_what_to_run(install, capsys):
    root, acl, _session = install

    assert cli.main(["permissions"]) == 0

    out = capsys.readouterr().out
    assert t("permissions.state_open") in out
    assert "BUILTIN\\Administrators" in out
    assert (
        t("permissions.needs_admin", owner="BUILTIN\\Administrators", command=permissions.fix_command(admin=True))
        in out
    )
    # The launcher of this install, quoted, inside its root (whatever separator the tests' OS uses).
    assert permissions.fix_command(admin=True) == f'"{root / launcher(windows=True)}" permissions fix'
    assert permissions.fix_command(admin=True).startswith(f'"{root}{os.sep}')
    assert launcher(windows=True).endswith("tow.cmd")
    assert acl.calls == []  # a status changes nothing, and the start-up repair is not run for it


def test_in_an_administrator_terminal_status_just_names_the_command(install, capsys):
    _root, _acl, session = install
    session.update(elevated=True)

    assert cli.main(["permissions"]) == 0

    out = capsys.readouterr().out
    assert t("permissions.run_fix", command=permissions.fix_command(admin=True)) in out
    assert "Run as administrator" not in out
    assert "Запуск от имени администратора" not in out


def test_without_an_administrator_terminal_fix_changes_nothing_and_prints_the_command(install, capsys):
    _root, acl, _session = install

    assert cli.main(["permissions", "fix"]) == 3

    assert all("/setowner" not in call for call in acl.calls)
    assert permissions.fix_command(admin=True) in capsys.readouterr().out


def test_an_administrator_terminal_gives_the_install_to_the_autostart_account_and_closes_it(install, capsys):
    root, acl, session = install
    session.update(user=ADMIN, elevated=True)  # another administrator opened the terminal

    assert cli.main(["permissions", "fix"]) == 0

    for folder in (root, root / "keys", root / "data"):
        assert acl.owner(folder) == OWNER  # never Administrators, never the terminal's account
    assert acl.sddl[str(root)] == f"O:{OWNER}D:PAI(A;OICI;FA;;;{OWNER})(A;OICI;FA;;;S-1-5-18)(A;OICI;FA;;;S-1-5-32-544)"
    assert "AU" not in acl.sddl[str(root / "app")]  # what the root holds inherits it
    assert {Path(call[1]) for call in acl.calls} <= {root, root / "keys", root / "data"}  # nothing outside
    out = capsys.readouterr().out
    assert t("permissions.owner_from_autostart", account="PC\\owner") in out
    assert t("permissions.fixed") in out

    # From now on the owner's own starts find nothing to warn about.
    session.update(user=OWNER, elevated=False)
    from tow.store import protect_install_folders

    assert protect_install_folders() == []
    assert capsys.readouterr().err == ""


def test_without_autostart_the_account_that_opened_the_terminal_gets_it(install, capsys):
    root, acl, session = install
    session.update(user=OWNER, elevated=True, task="")

    assert cli.main(["permissions", "fix"]) == 0

    assert acl.owner(root) == OWNER
    assert t("permissions.owner_from_terminal", account="PC\\owner") in capsys.readouterr().out


@pytest.mark.parametrize("terminal", ["S-1-5-32-544", "S-1-5-18"])
def test_administrators_or_system_never_become_the_owner(install, capsys, terminal):
    root, acl, session = install
    session.update(user=terminal, elevated=True, task="")

    assert cli.main(["permissions", "fix"]) == 3

    assert acl.calls == []
    assert acl.owner(root) == "BA"
    command = f"{permissions.fix_command(admin=False)} --owner"
    assert t("permissions.no_account", command=command) in capsys.readouterr().out


def test_an_administrator_password_typed_from_a_standard_account_names_no_owner(install, capsys):
    """Round-3 audit: a standard account that opened the terminal with an administrator's
    password made that administrator the owner, and the standard account - the one using TOW -
    lost its own install. TOW refuses and asks for the owner."""
    root, acl, session = install
    session.update(user=ADMIN, console=OWNER, elevated=True, task="")

    assert cli.main(["permissions", "fix"]) == 3

    assert acl.calls == []
    assert acl.owner(root) == "BA"
    out = capsys.readouterr().out
    command = f"{permissions.fix_command(admin=False)} --owner"
    assert t("permissions.owner_ambiguous", terminal="PC\\admin", session="PC\\owner", command=command) in out


def test_the_owner_can_be_named(install, capsys):
    root, acl, session = install
    session.update(user=ADMIN, console=OWNER, elevated=True, task="")

    assert cli.main(["permissions", "fix", "--owner", "PC\\owner"]) == 0

    for folder in (root, root / "keys", root / "data"):
        assert acl.owner(folder) == OWNER
    out = capsys.readouterr().out
    assert t("permissions.owner_from_option", account="PC\\owner") in out
    assert t("permissions.fixed") in out


def test_a_named_owner_without_an_administrator_terminal_changes_nothing_and_says_why(install, capsys):
    # Round-5 QA: `--owner` in an ordinary terminal was ignored without a word.
    _root, acl, session = install
    session.update(user=OWNER, elevated=False)

    assert cli.main(["permissions", "fix", "--owner", "PC\\owner"]) == 3

    assert acl.calls == []
    assert t("permissions.owner_needs_admin", command=permissions.fix_command(admin=True)) in capsys.readouterr().out


@pytest.mark.parametrize("name", ["BUILTIN\\Administrators", "PC\\nobody"])
def test_a_named_owner_must_be_a_person(install, capsys, name):
    _root, acl, session = install
    session.update(elevated=True)

    assert cli.main(["permissions", "fix", "--owner", name]) == 3

    assert acl.calls == []
    assert t("permissions.owner_not_person", name=name) in capsys.readouterr().out


def test_an_install_a_person_owns_stays_theirs(install, capsys):
    """Its folders are open, but the TOW folder already belongs to a person: another
    administrator's terminal closes them for that person, never takes them over."""
    root, acl, session = install
    acl.sddl[str(root)] = acl.sddl[str(root)].replace("O:BA", f"O:{OWNER}")
    session.update(user=ADMIN, elevated=True, task="")

    assert cli.main(["permissions", "fix"]) == 0

    for folder in (root, root / "keys", root / "data"):
        assert acl.owner(folder) == OWNER
    assert t("permissions.owner_from_owner", account="PC\\owner") in capsys.readouterr().out


def test_a_data_folder_outside_the_install_is_reported_without_the_fix_advice(install, tmp_path, monkeypatch, capsys):
    """Round-3 audit: TOW_HOME elsewhere was reported open with "run tow permissions fix",
    which never changes a folder outside the install."""
    _root, acl, session = install
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    acl.sddl[str(elsewhere)] = OPEN
    monkeypatch.setenv("TOW_HOME", str(elsewhere))
    session.update(elevated=True)
    assert cli.main(["permissions", "fix"]) == 0
    capsys.readouterr()

    assert cli.main(["permissions"]) == 0

    out = capsys.readouterr().out
    assert t("permissions.outside", folder=t("permissions.folder_data"), path=str(elsewhere)) in out
    assert permissions.fix_command(admin=True) not in out  # no advice to run what changes nothing
    assert t("permissions.all_closed") not in out
    assert acl.sddl[str(elsewhere)] == OPEN  # never changed
    assert permissions.status()["ok"] is True


def test_a_drive_root_is_refused(install, monkeypatch, capsys):
    root, acl, session = install
    session.update(elevated=True)
    monkeypatch.setenv("TOW_ROOT", root.anchor)

    assert cli.main(["permissions", "fix"]) == 3

    assert acl.calls == []
    assert t("permissions.refused_root", path=root.anchor) in capsys.readouterr().out


def test_a_closed_install_needs_nothing(install, capsys):
    _root, acl, session = install
    session.update(elevated=True)
    assert cli.main(["permissions", "fix"]) == 0
    acl.calls.clear()

    assert cli.main(["permissions", "fix"]) == 0  # once is enough: the second run changes nothing
    assert acl.calls == []
    assert cli.main(["permissions"]) == 0
    assert t("permissions.all_closed") in capsys.readouterr().out


@pytest.mark.skipif(platform.this_os() != "windows", reason="ctypes.wintypes")
def test_the_session_account_is_the_one_signed_in(monkeypatch):
    freed = []

    class Wts:
        def WTSQuerySessionInformationW(self, server, session, info, buffer, size):
            buffer._obj.value = {7: "PC", 5: "owner"}[info]
            return 1

        def WTSFreeMemory(self, buffer):
            freed.append(buffer.value)

    monkeypatch.setattr(windows, "_dll", lambda name: Wts())
    monkeypatch.setattr(windows, "account_of", lambda name: {"PC\\owner": OWNER}.get(name))
    assert windows.session_user_sid() == OWNER
    assert freed == ["PC", "owner"]

    class NoAnswer(Wts):
        def WTSQuerySessionInformationW(self, *_args):
            return 0

    monkeypatch.setattr(windows, "_dll", lambda name: NoAnswer())
    assert windows.session_user_sid() is None


def test_the_warnings_point_to_the_repair_command():
    for key in ("cli.root_shared", "cli.folder_shared"):
        for lang in ("en", "ru"):
            assert "tow permissions fix" in t(key, lang, path="X")
    for key in ("doctor_report.open_root", "doctor_report.open_folders"):
        for lang in ("en", "ru"):
            assert "tow permissions fix" in t(key, lang, value="X")


def test_the_autostart_task_says_which_account_it_runs_as():
    from tow.autostart.windows import parse_task

    xml = (
        '<Task xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">'
        "<Triggers><LogonTrigger><UserId>PC\\trigger</UserId></LogonTrigger></Triggers>"
        "<Principals><Principal id='Author'><UserId>PC\\owner</UserId></Principal></Principals>"
        "</Task>"
    )
    assert parse_task(xml)["user"] == "PC\\owner"


@pytest.mark.skipif(platform.this_os() == "windows", reason="POSIX permissions")
def test_posix_hand_over_of_an_own_folder_makes_it_0700(tmp_path):
    from tow.platform.posix import PosixBackend

    folder = tmp_path / "TOW"
    folder.mkdir()
    folder.chmod(0o777)
    backend = PosixBackend("linux")
    me = backend.current_account()
    assert me is not None
    assert backend.hand_over(folder, me) is (me != "0")
