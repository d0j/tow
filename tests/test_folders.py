import os

import pytest

from tow import platform
from tow.folders import (
    is_protected_folder,
    paths_equal,
    recent_save_roots,
    remember_save_root,
    save_root,
    seen_from_here,
)
from tow.platform.posix import PosixBackend
from tow.platform.windows import WindowsBackend


def test_save_root_top_level_only():
    assert save_root(r"M:\TV\Show\S01") == r"M:\TV"
    assert save_root(r"M:\anime") == r"M:\anime"
    assert save_root(r"m:/films/x") == r"m:\films"
    assert save_root("") == ""


def test_remember_last_10_newest_first():
    st: dict = {}
    remember_save_root(st, r"M:\TV\a")
    remember_save_root(st, r"D:\films\b")
    remember_save_root(st, r"M:\TV\c")
    assert st["save_roots"] == [r"M:\TV", r"D:\films"]
    for i in range(12):
        remember_save_root(st, rf"E:\p{i}\x")
    assert len(st["save_roots"]) == 10
    assert st["save_roots"][0] == r"E:\p11"


def test_empty_path_uses_last_root():
    from tow.folders import resolve_save_path

    st = {"save_roots": [r"M:\TV", r"M:\anime"]}
    assert resolve_save_path("", st) == r"M:\TV"
    assert resolve_save_path(r"P:\video", st) == r"P:\video"
    assert resolve_save_path("", {}) == ""


def test_recent_from_topics_if_empty():
    st = {
        "topics": [
            {"save_path": r"M:\old\show"},
            {"save_path": r"M:\TV\x"},
        ]
    }
    assert recent_save_roots(st) == [r"M:\TV", r"M:\old"]


def test_posix_save_root_keeps_the_first_two_folders():
    assert save_root("/srv/media/Show/S01") == "/srv/media"
    assert save_root("/downloads") == "/downloads"
    assert save_root("//nas/media/x") == r"\\nas\media"  # a share, however it is typed


@pytest.mark.parametrize(
    ("a", "b", "same"),
    [
        ("D:\\Media\\", "d:/media", True),  # a Windows client: case and slashes do not matter
        ("/downloads/TV/", "/downloads/TV", True),
        ("/downloads/tv", "/downloads/TV", False),  # a Linux client: they do
        ("/downloads/a/../b", "/downloads/b", True),
        ("", "", False),
        ("D:\\Media", "/Media", False),
    ],
)
def test_client_paths_compare_the_way_the_clients_system_does(a, b, same):
    assert paths_equal(a, b) is same


@pytest.mark.parametrize("name", ["windows", "linux", "macos"])
def test_every_system_refuses_the_folders_of_every_system(monkeypatch, name):
    for variable in ("SystemRoot", "ProgramFiles", "ProgramData", "APPDATA", "LOCALAPPDATA", "USERPROFILE"):
        monkeypatch.delenv(variable, raising=False)
    # A home folder that does not exist here: on macOS /home resolves into /System/Volumes/Data.
    backend = PosixBackend(name, home="/no-such-home-for-tow/x") if name != "windows" else WindowsBackend()
    with platform.use(backend):
        for path in (r"C:\Windows\System32\x", r"C:\Program Files\x", "/etc/cron.d", "/usr/local/bin/x"):
            assert is_protected_folder(path), path
        assert is_protected_folder("/no-such-home-for-tow/x/.ssh/keys") is (name != "windows")
        for path in (r"D:\Media\Show", "/downloads/tv", "/var/lib/transmission-daemon/downloads"):
            assert not is_protected_folder(path), path


@pytest.mark.parametrize(("name", "merged"), [("linux", False), ("macos", True), ("windows", False)])
def test_remembered_roots_ignore_case_only_where_the_paths_system_does(name, merged):
    backend = PosixBackend(name, home="/no-such-home-for-tow/x") if name != "windows" else WindowsBackend()
    with platform.use(backend):
        state: dict = {}
        remember_save_root(state, "/srv/Media/Show")
        remember_save_root(state, "/srv/media/Other")
        assert state["save_roots"] == (["/srv/media"] if merged else ["/srv/media", "/srv/Media"])
        remember_save_root(state, r"D:\Serials\a")
        remember_save_root(state, r"d:\serials\b")  # a Windows path: one folder on every system
        assert [root for root in state["save_roots"] if ":" in root] == [r"d:\serials"]
        topics = {"topics": [{"save_path": "/srv/Media/a"}, {"save_path": "/srv/media/b"}]}
        assert len(recent_save_roots(topics)) == (1 if merged else 2)


@pytest.mark.parametrize(("name", "folded"), [("linux", False), ("macos", True), ("windows", True)])
def test_posix_system_folders_ignore_case_except_on_linux(monkeypatch, name, folded):
    backend = PosixBackend(name, home="/no-such-home-for-tow/x") if name != "windows" else WindowsBackend()
    with platform.use(backend):
        assert is_protected_folder("/System/Library/x")
        assert is_protected_folder("/system/library/x") is folded  # a download folder on Linux


@pytest.mark.parametrize("name", ["windows", "linux", "macos"])
def test_a_windows_clients_appdata_is_protected_whatever_tow_runs_on(monkeypatch, name):
    for variable in ("SystemRoot", "ProgramFiles", "ProgramData", "APPDATA", "LOCALAPPDATA", "USERPROFILE"):
        monkeypatch.delenv(variable, raising=False)
    backend = PosixBackend(name, home="/no-such-home-for-tow/x") if name != "windows" else WindowsBackend()
    with platform.use(backend):
        startup = r"C:\Users\bob\AppData\Roaming\Microsoft\Windows\Start Menu\Programs\Startup"
        for path in (startup, r"e:\users\Alice\appdata", "C:/Users/bob/AppData/Local/x", r"\\pc\c$\Users\bob\AppData"):
            assert is_protected_folder(path), path
        for path in (r"C:\Users\bob\Videos", r"D:\Users\bob\AppDataBackup", r"D:\Media\Users\bob\AppData"):
            assert not is_protected_folder(path), path


def test_seen_from_here_needs_the_drive_or_top_folder_of_this_machine(tmp_path):
    assert seen_from_here(str(tmp_path / "Show" / "S01"))
    assert not seen_from_here("/no-such-top-folder-for-tow/tv")  # a client in a container
    assert not seen_from_here("")
    assert not seen_from_here("relative/folder")
    if os.name != "nt":
        assert not seen_from_here(r"D:\Media")  # a Windows client seen from Linux or macOS


def test_macos_data_volume_paths_are_the_folders_they_stand_for(monkeypatch):
    import posixpath
    from types import SimpleNamespace

    from tow import folders

    macos_path = SimpleNamespace(
        isabs=posixpath.isabs,
        normpath=posixpath.normpath,
        dirname=posixpath.dirname,
        basename=posixpath.basename,
        join=posixpath.join,
        exists=lambda _p: True,
        realpath=lambda p: "/System/Volumes/Data" + p,  # /home is a firmlink into the data volume
    )
    monkeypatch.setattr(folders, "os", SimpleNamespace(path=macos_path))
    assert folders._real_path("/home/user/Media") == "/home/user/Media"
