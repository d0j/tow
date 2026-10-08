"""Closing a folder and handing it to its owner count only when Windows reads them back so.

``make_private`` (every start) and ``hand_over`` (``tow permissions fix``) change real
permissions with icacls; here icacls, the account and the folder's security descriptor (SDDL)
are fakes, so nothing on disk changes. An unknown account or owner, a group or SYSTEM as the
new owner, and a change that does not read back must each be a failure - never "closed" or
"handed over".
"""

from __future__ import annotations

import re
from pathlib import Path

import pytest

from tow import platform
from tow.platform import windows
from tow.platform.windows import WindowsBackend

USER = "S-1-5-21-1000000001-1000000002-1000000003-1001"
OTHER = "S-1-5-21-1000000001-1000000002-1000000003-1002"
MS_ACCOUNT = "S-1-12-1-1000000001-1000000002-1000000003-1000000004"
# A folder made from an administrator terminal: owner Administrators, inherited from the drive.
OPEN = "O:BAD:AI(A;OICIID;FA;;;BA)(A;OICIID;FA;;;SY)(A;OICIID;0x1200a9;;;BU)(A;OICIID;0x1301bf;;;AU)"


def _closed(owner: str, account: str) -> str:
    return f"O:{owner}D:PAI(A;OICI;FA;;;{account})(A;OICI;FA;;;SY)(A;OICI;FA;;;BA)"


class Icacls:
    """icacls on one folder: /setowner and /inheritance:r /grant:r change ``sddl`` unless the
    test turned that part off (a change Windows did not make, without an error)."""

    def __init__(self, sddl: str | None, *, sets_owner: bool = True, closes: bool = True) -> None:
        self.sddl = sddl
        self.sets_owner = sets_owner
        self.closes = closes
        self.calls: list[list[str]] = []

    def run(self, args: list[str], timeout: float = 20) -> str:
        self.calls.append(list(args))
        if self.sddl is None:
            return ""
        owner, dacl = re.fullmatch(r"O:(.+?)D:(.*)", self.sddl).groups()  # type: ignore[union-attr]
        if "/setowner" in args and self.sets_owner:
            owner = args[args.index("/setowner") + 1].lstrip("*")
        if "/inheritance:r" in args and self.closes:
            sids = [grant.split(":")[0].lstrip("*") for grant in args if grant.startswith("*") and ":(" in grant]
            dacl = "PAI" + "".join(f"(A;OICI;FA;;;{sid})" for sid in sids)
        self.sddl = f"O:{owner}D:{dacl}"
        return ""


@pytest.fixture
def folder(tmp_path):
    path = tmp_path / "TOW"
    path.mkdir()
    return path


def _fake(monkeypatch, icacls: Icacls, *, user: str | None = USER) -> Icacls:
    monkeypatch.setattr(windows, "user_sid", lambda: user)
    monkeypatch.setattr(windows, "folder_security", lambda _path: icacls.sddl)
    monkeypatch.setattr(windows, "_run", icacls.run)
    return icacls


# --- make_private ----------------------------------------------------------------------------


def test_without_knowing_this_account_nothing_is_closed_or_reported_closed(monkeypatch, folder):
    icacls = _fake(monkeypatch, Icacls(OPEN.replace("O:BA", f"O:{USER}")), user=None)
    assert windows.make_private(folder) is False
    assert windows.make_private(folder, created=True) is False
    assert icacls.calls == []


@pytest.mark.parametrize(
    "sddl", [None, "", "garbage", "D:PAI(A;OICI;FA;;;SY)"], ids=["unreadable", "empty", "garbage", "no-owner"]
)
def test_without_knowing_the_owner_nothing_is_closed_or_reported_closed(monkeypatch, folder, sddl):
    icacls = _fake(monkeypatch, Icacls(sddl))
    assert windows.make_private(folder) is False
    assert windows.make_private(folder, created=True) is False
    assert icacls.calls == []


def test_a_folder_administrators_own_is_left_alone_unless_this_process_created_it(monkeypatch, folder):
    icacls = _fake(monkeypatch, Icacls(OPEN))
    assert windows.make_private(folder) is False  # by default: not created by this process
    assert icacls.calls == []
    assert WindowsBackend().make_private(folder) is False
    assert WindowsBackend().make_private(folder, created=True) is True
    assert len(icacls.calls) == 1


def test_the_backend_reports_what_the_folder_reads_back(monkeypatch, folder):
    icacls = _fake(monkeypatch, Icacls(OPEN.replace("O:BA", f"O:{USER}")))
    backend = WindowsBackend()
    assert backend.folder_shared(folder) is True
    assert backend.root_shared(folder) is True
    assert backend.make_private(folder) is True
    assert backend.folder_shared(folder) is False
    assert backend.folder_owner(folder) == USER
    icacls.sddl = None
    assert backend.folder_shared(folder) is None
    monkeypatch.setattr(windows, "user_sid", lambda: None)
    icacls.sddl = OPEN
    assert backend.folder_shared(folder) is None


# --- hand_over (tow permissions fix) -----------------------------------------------------------


@pytest.mark.parametrize(
    "account",
    ["S-1-5-32-544", "BA", "S-1-5-18", "SY", "S-1-5-19", "S-1-5-20", "S-1-1-0", "S-1-5-11", "S-1-5-32-545"],
    ids=[
        "administrators",
        "administrators-alias",
        "system",
        "system-alias",
        "local-service",
        "network-service",
        "everyone",
        "authenticated-users",
        "users",
    ],
)
def test_an_install_is_never_handed_to_a_group_system_or_a_service(monkeypatch, folder, account):
    icacls = _fake(monkeypatch, Icacls(OPEN))
    with platform.use(WindowsBackend()):
        assert platform.current().personal_account(account) is False
        assert platform.current().hand_over(folder, account) is False
    assert icacls.calls == []
    assert icacls.sddl == OPEN


@pytest.mark.parametrize("account", [USER, MS_ACCOUNT, USER.lower()], ids=["local", "microsoft", "lowercase"])
def test_a_persons_account_gets_the_folder_and_it_is_read_back_closed(monkeypatch, folder, account):
    icacls = _fake(monkeypatch, Icacls(OPEN))
    assert windows.hand_over(folder, account) is True
    owner = account.upper()
    assert icacls.sddl == f"O:{owner}D:PAI(A;OICI;FA;;;{owner})(A;OICI;FA;;;S-1-5-18)(A;OICI;FA;;;S-1-5-32-544)"
    assert [call[2] for call in icacls.calls] == ["/setowner", "/inheritance:r"]
    assert icacls.calls[0][3] == f"*{owner}"


@pytest.mark.parametrize(
    ("sets_owner", "closes"),
    [(False, True), (True, False), (False, False)],
    ids=["owner-kept", "still-open", "nothing"],
)
def test_a_hand_over_windows_did_not_make_is_a_failure(monkeypatch, folder, sets_owner, closes):
    _fake(monkeypatch, Icacls(OPEN, sets_owner=sets_owner, closes=closes))
    assert windows.hand_over(folder, USER) is False


def test_a_hand_over_to_another_owner_than_asked_is_a_failure(monkeypatch, folder):
    icacls = _fake(monkeypatch, Icacls(OPEN))
    original = icacls.run

    def wrong_owner(args, timeout=20):
        original(args, timeout)
        icacls.sddl = icacls.sddl.replace(f"O:{USER}", f"O:{OTHER}")  # type: ignore[union-attr]
        return ""

    monkeypatch.setattr(windows, "_run", wrong_owner)
    assert windows.hand_over(folder, USER) is False


@pytest.mark.parametrize(
    "after", [None, "garbage", f"O:{USER}D:NO_ACCESS_CONTROL"], ids=["unreadable", "garbage", "no-acl"]
)
def test_a_hand_over_whose_result_cannot_be_read_back_closed_is_a_failure(monkeypatch, folder, after):
    icacls = _fake(monkeypatch, Icacls(OPEN))
    monkeypatch.setattr(windows, "_run", lambda args, timeout=20: icacls.calls.append(list(args)) or "")
    icacls.sddl = after
    assert windows.hand_over(folder, USER) is False
    assert len(icacls.calls) == 2


def test_the_owner_of_a_folder_is_unknown_when_its_permissions_are(monkeypatch, folder):
    _fake(monkeypatch, Icacls(None))
    assert windows.folder_owner(folder) is None
    _fake(monkeypatch, Icacls(_closed("BA", USER)))
    assert windows.folder_owner(folder) == "S-1-5-32-544"
    assert Path(folder).is_dir()  # nothing on disk was touched


# --- the SDDL parser and the account names, without Windows (mutation survivors) -------------


@pytest.fixture
def no_win32(monkeypatch):
    """Any Win32 call fails the way a missing DLL would: these helpers must not need one."""

    def no_dll(name):
        raise OSError(f"no {name} here")

    windows._alias_sid.cache_clear()
    monkeypatch.setattr(windows, "_dll", no_dll)
    yield
    windows._alias_sid.cache_clear()


def test_the_ace_parser_reads_each_ace_and_refuses_malformed_text():
    aces = windows._aces("(A;OICI;FA;;;SY)(D;;FA;;;S-1-5-21-1-2-3-1001)")
    assert aces == [["A", "OICI", "FA", "", "", "SY"], ["D", "", "FA", "", "", "S-1-5-21-1-2-3-1001"]]
    assert windows._aces("") == []
    assert windows._aces("(A;;FA;;;SY;(@User.x == 1))") == [["A", "", "FA", "", "", "SY", "(@User.x == 1)"]]
    assert windows._aces("(A;;FA;;;SY))") is None  # one parenthesis closed twice
    assert windows._aces(")(A;;FA;;;SY)") is None
    assert windows._aces("(A;;FA;;;SY") is None  # never closed
    assert windows._aces("(A;;FA;;SY)") is None  # five fields: no account


def test_others_in_the_permissions_are_found_by_account_however_written(no_win32):
    assert windows.sddl_others(_closed(USER, USER), USER) is False
    assert windows.sddl_others(_closed(USER, USER), USER.lower()) is False
    assert windows.sddl_others(_closed(USER, OTHER), USER) is True
    assert windows.sddl_others(OPEN, USER) is True  # Users and Authenticated Users
    assert windows.sddl_others(f"O:{USER}D:P(D;;FA;;;WD)(A;;FA;;;SY)", USER) is False  # a deny lets nobody in
    assert windows.sddl_others(f"O:{USER}D:NO_ACCESS_CONTROL", USER) is True
    # A resource-attribute ACE carries a seventh field: the account is still the sixth.
    assert windows.sddl_others(f"O:{USER}D:P(A;;FA;;;SY;(x))(A;;FA;;;{USER})", USER) is False
    assert windows.sddl_others(f"O:{USER}D:P(A;;FA;;;SY))", USER) is None  # malformed: unknown
    assert windows.sddl_others(f"O:{USER}D:P(A;;FA;;SY)", USER) is None
    assert windows.sddl_others("not sddl", USER) is None
    assert windows.sddl_owner(_closed("BA", USER)) == "S-1-5-32-544"
    assert windows.sddl_owner(_closed(USER.lower(), USER)) == USER
    assert windows.sddl_owner("not sddl") is None


def test_an_account_written_as_a_sid_is_passed_through_without_windows(no_win32):
    assert windows.account_of(USER) == USER
    assert windows.account_of(USER.lower()) == USER
    assert windows.account_of("S-1-5-18") == "S-1-5-18"
    assert windows.account_of("PC\\owner") is None  # a name needs Windows: unknown here
    assert windows.sid_text("SY") == "S-1-5-18"
    assert windows.sid_text("ZZ") == "ZZ"  # an alias Windows cannot resolve stays itself
