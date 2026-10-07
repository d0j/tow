"""keys/ and data/ only for this account: Windows ACLs (an install in a drive root inherits
"Authenticated Users: modify"), POSIX 0700; repaired at start, reported by `tow doctor`.

Nothing here changes real permissions: icacls is faked, backends are injected."""

from __future__ import annotations

from pathlib import Path

import pytest

from tow import platform
from tow.platform import windows

USER = "S-1-5-21-1000000001-1000000002-1000000003-1001"
OTHER = "S-1-5-21-1000000001-1000000002-1000000003-1002"
# What a folder in C:\ (C:\TOW) inherits from the drive root.
DRIVE_ROOT = (
    f"O:{USER}D:AI(A;OICIID;FA;;;BA)(A;OICIID;FA;;;SY)(A;OICIID;0x1200a9;;;BU)"
    "(A;ID;0x1301bf;;;AU)(A;OICIIOID;SDGXGWGR;;;AU)(A;OICIIOID;GA;;;CO)"
)
PRIVATE = f"O:{USER}D:PAI(A;OICI;FA;;;BA)(A;OICI;FA;;;SY)(A;OICI;FA;;;{USER})"


@pytest.mark.parametrize(
    ("sddl", "others"),
    [
        (DRIVE_ROOT, True),
        (PRIVATE, False),
        (f"O:{USER}D:PAI(A;OICI;FA;;;{USER})(A;OICI;FA;;;{OTHER})", True),
        (f"O:{USER}D:PAI(D;OICI;FA;;;WD)(A;OICI;FA;;;{USER})(A;OICIIO;GA;;;CO)", False),  # deny is no access
        (f"O:{USER}D:NO_ACCESS_CONTROL", True),  # no permissions: everyone
        ("O:BAD:PAI(A;OICI;FA;;;SY)(XA;OICI;FA;;;WD;(Member_of {SID(BA)}))", True),
        ("garbage", None),
        (f"O:{USER}D:P(A;OICI;FA;;;SY", None),
    ],
)
def test_who_besides_this_account_may_open_a_folder(sddl, others):
    assert windows.sddl_others(sddl, USER) is others


def test_the_owner_of_a_folder_is_read_from_its_permissions():
    assert windows.sddl_owner(DRIVE_ROOT) == USER
    assert windows.sddl_owner("O:BAD:PAI(A;;FA;;;SY)") == "S-1-5-32-544"
    assert windows.sddl_owner("garbage") is None


BUILTIN_ADMIN = "S-1-5-21-1000000001-1000000002-1000000003-500"
# What Windows writes for a folder only the built-in Administrator (RID 500) may open.
ALIASED = "O:LAD:PAI(A;OICI;FA;;;LA)(A;OICI;FA;;;SY)(A;OICI;FA;;;BA)"


def test_an_account_written_as_an_alias_is_the_same_account(monkeypatch):
    monkeypatch.setattr(windows, "_alias_sid", {"LA": BUILTIN_ADMIN}.get)
    assert windows.sddl_owner(ALIASED) == BUILTIN_ADMIN
    assert windows.sddl_others(ALIASED, BUILTIN_ADMIN) is False
    assert windows.sddl_others(ALIASED, USER) is True
    assert windows.sddl_others(ALIASED.replace("(A;OICI;FA;;;SY)", "(A;OICI;FR;;;LG)"), BUILTIN_ADMIN) is True
    assert windows.sddl_others(f"O:{USER}D:PAI(A;OICI;FA;;;{USER.lower()})(A;;FA;;;S-1-5-18)", USER) is False


def test_a_private_folder_of_an_aliased_account_is_left_alone(fake_windows, monkeypatch):
    folder, acl, calls = fake_windows
    monkeypatch.setattr(windows, "user_sid", lambda: BUILTIN_ADMIN)
    monkeypatch.setattr(windows, "_alias_sid", {"LA": BUILTIN_ADMIN}.get)
    acl["sddl"] = ALIASED
    assert windows.folder_shared(folder) is False
    acl["sddl"] = "O:LAD:AI(A;OICIID;FA;;;BA)(A;OICIID;FA;;;SY)(A;ID;0x1301bf;;;AU)"
    monkeypatch.setattr(windows, "_run", lambda args, timeout=20: calls.append(args) or acl.update(sddl=ALIASED) or "")
    assert windows.make_private(folder) is True  # its own folder: the owner matches, read back private
    assert len(calls) == 1


@pytest.mark.skipif(platform.this_os() != "windows", reason="Windows security descriptors")
def test_windows_writes_the_builtin_administrator_as_an_alias_and_it_still_matches():
    import ctypes
    from ctypes import wintypes

    machine = windows.sid_text("LA").rsplit("-", 1)[0]
    assert machine.startswith("S-1-5-21-")
    advapi32 = ctypes.WinDLL("advapi32")
    for rid in (500, 501, 1001):
        sid = f"{machine}-{rid}"
        descriptor, size = ctypes.c_void_p(), wintypes.ULONG()
        source = f"O:{sid}D:PAI(A;OICI;FA;;;{sid})(A;OICI;FA;;;SY)(A;OICI;FA;;;BA)"
        assert advapi32.ConvertStringSecurityDescriptorToSecurityDescriptorW(
            source, 1, ctypes.byref(descriptor), ctypes.byref(size)
        )
        text = wintypes.LPWSTR()
        assert advapi32.ConvertSecurityDescriptorToStringSecurityDescriptorW(
            descriptor, 1, 0x1 | 0x4, ctypes.byref(text), None
        )
        written = text.value
        ctypes.windll.kernel32.LocalFree(text)
        ctypes.windll.kernel32.LocalFree(descriptor)
        assert windows.sddl_owner(written) == sid, written
        assert windows.sddl_others(written, sid) is False, written


@pytest.fixture
def fake_windows(monkeypatch, tmp_path):
    """A folder whose permissions icacls changes - in a dictionary, never on disk."""
    folder = tmp_path / "keys"
    folder.mkdir()
    acl = {"sddl": DRIVE_ROOT}
    calls: list[list[str]] = []

    def icacls(args, timeout=20):
        calls.append(list(args))
        acl["sddl"] = PRIVATE
        return ""

    monkeypatch.setattr(windows, "user_sid", lambda: USER)
    monkeypatch.setattr(windows, "folder_security", lambda _path: acl["sddl"])
    monkeypatch.setattr(windows, "_run", icacls)
    return folder, acl, calls


def test_a_folder_of_this_account_loses_what_it_inherits_and_is_read_back(fake_windows):
    folder, _acl, calls = fake_windows
    assert windows.folder_shared(folder) is True

    assert windows.make_private(folder) is True

    [argv] = calls
    assert argv[0].lower().endswith("\\system32\\icacls.exe")  # never an icacls.exe of the current folder
    assert argv[1:] == [
        str(folder),
        "/inheritance:r",
        "/grant:r",
        f"*{USER}:(OI)(CI)F",
        "*S-1-5-18:(OI)(CI)F",
        "*S-1-5-32-544:(OI)(CI)F",
        "/Q",
    ]
    assert windows.folder_shared(folder) is False


def test_a_folder_of_another_account_is_never_changed(fake_windows):
    folder, acl, calls = fake_windows
    acl["sddl"] = DRIVE_ROOT.replace(f"O:{USER}", f"O:{OTHER}")
    assert windows.make_private(folder) is False
    assert calls == []


@pytest.mark.parametrize(("created", "changed"), [(True, True), (False, False)])
def test_a_folder_owned_by_administrators_is_changed_only_when_this_process_created_it(fake_windows, created, changed):
    # An elevated session creates folders owned by Administrators, not by the account itself;
    # such a folder may also be another account's, made by its own elevated session: changing
    # it would leave only this account (and Administrators) in.
    folder, acl, calls = fake_windows
    acl["sddl"] = DRIVE_ROOT.replace(f"O:{USER}", "O:BA")
    assert windows.make_private(folder, created=created) is changed
    assert bool(calls) is changed
    acl["sddl"] = DRIVE_ROOT.replace(f"O:{USER}", f"O:{OTHER}")
    assert windows.make_private(folder, created=True) is False  # another account's, never


def test_a_change_that_does_not_read_back_private_is_a_failure(fake_windows, monkeypatch):
    folder, _acl, calls = fake_windows
    monkeypatch.setattr(windows, "_run", lambda args, timeout=20: calls.append(list(args)) or "")
    assert windows.make_private(folder) is False
    assert len(calls) == 1


def test_unknown_permissions_are_not_reported_as_open(monkeypatch, tmp_path):
    monkeypatch.setattr(windows, "folder_security", lambda _path: None)
    assert windows.folder_shared(tmp_path) is None


class FolderBackend:
    """Folders other accounts can open until made private (``fixable`` ones only)."""

    name = "windows"

    def __init__(self, shared: set[Path], fixable: set[Path]) -> None:
        self.shared = set(shared)
        self.fixable = set(fixable)
        self.repaired: list[Path] = []
        self.created: list[Path] = []

    def folder_shared(self, path: Path) -> bool | None:
        return path in self.shared

    def make_private(self, path: Path, *, created: bool = False) -> bool:
        self.repaired.append(path)
        if created:
            self.created.append(path)
        if path in self.fixable:
            self.shared.discard(path)
            return True
        return False


def test_private_folders_repairs_ours_and_reports_what_stays_open(tmp_path):
    ours, foreign, private = tmp_path / "ours", tmp_path / "foreign", tmp_path / "private"
    for folder in (ours, foreign, private):
        folder.mkdir()
    backend = FolderBackend(shared={ours, foreign}, fixable={ours})
    with platform.use(backend):  # type: ignore[arg-type]
        assert platform.private_folders([ours, foreign, private, tmp_path / "missing"]) == [foreign]
        assert platform.private_folders([ours, foreign]) == [foreign]  # idempotent
    assert backend.repaired == [ours, foreign, foreign]


def test_doctor_asks_without_changing_anything(tmp_path):
    folder = tmp_path / "data"
    folder.mkdir()
    backend = FolderBackend(shared={folder}, fixable={folder})
    with platform.use(backend):  # type: ignore[arg-type]
        assert platform.private_folders([folder], repair=False) == [folder]
    assert backend.repaired == []


def test_a_backend_without_the_question_is_unknown(tmp_path):
    class Old:
        name = "windows"

    with platform.use(Old()):  # type: ignore[arg-type]
        assert platform.private_folders([tmp_path]) == []


def test_every_start_closes_keys_and_data_and_warns_about_what_stays_open(tmp_path, capsys):
    from tow import cli
    from tow.paths import data_dir, keys_dir

    keys_dir().mkdir()
    keys, data = keys_dir(), data_dir()
    backend = FolderBackend(shared={keys, data}, fixable={keys})
    with platform.use(backend):  # type: ignore[arg-type]
        assert cli.main(["version"]) == 0
    assert backend.repaired == [keys, data]
    err = capsys.readouterr().err
    assert str(data) in err
    assert str(keys) not in err


def test_the_web_app_start_closes_them_too(tmp_path):
    from fastapi.testclient import TestClient

    from tow.paths import data_dir
    from tow.web import create_app

    data = data_dir()
    backend = FolderBackend(shared={data}, fixable={data})
    with platform.use(backend), TestClient(create_app()):  # type: ignore[arg-type]
        pass
    assert backend.repaired == [data]


def test_a_new_key_folder_is_made_private(tmp_path):
    from tow.store import generate_master_key

    key = tmp_path / "elsewhere" / "master.key"
    backend = FolderBackend(shared={key.parent}, fixable={key.parent})
    with platform.use(backend):  # type: ignore[arg-type]
        generate_master_key(key)
    assert backend.repaired == backend.created == [key.parent]  # made by this process
    assert key.is_file()


def test_doctor_warns_when_other_accounts_can_open_the_folders(tmp_path):
    from tow.doctor import doctor_report, doctor_text
    from tow.i18n import t
    from tow.paths import data_dir

    data = data_dir()
    with platform.use(FolderBackend(shared={data}, fixable=set())):  # type: ignore[arg-type]
        report = doctor_report(probe=False)
    assert report["open_folders"] == [data.name]
    assert t("doctor_report.open_folders", "ru", value=data.name) in doctor_text(report)
    with platform.use(FolderBackend(shared=set(), fixable=set())):  # type: ignore[arg-type]
        assert doctor_report(probe=False)["open_folders"] == []


@pytest.mark.skipif(platform.this_os() == "windows", reason="POSIX permissions")
def test_posix_folder_of_this_account_becomes_0700(tmp_path):
    from tow.platform.posix import PosixBackend

    folder = tmp_path / "data"
    folder.mkdir(mode=0o755)
    folder.chmod(0o755)
    backend = PosixBackend("linux")
    assert backend.folder_shared(folder) is True
    assert backend.make_private(folder) is True
    assert folder.stat().st_mode & 0o777 == 0o700
