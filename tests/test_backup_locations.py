"""Backup folders chosen in Settings, night copy status, and the watchdog watching them."""

from __future__ import annotations

import errno
import re
import sys
from datetime import UTC, datetime
from pathlib import Path

import httpx
import pytest
from fastapi.testclient import TestClient
from helpers import flash_of

from tow.config import load_config, save_config
from tow.i18n import t
from tow.locations import MANUAL, NIGHT, check_writable, problem, resolve
from tow.paths import config_path


def _client(**kwargs) -> TestClient:
    from tow.web import app

    return TestClient(app, headers={"Origin": "http://127.0.0.1"}, **kwargs)


def _flash(response: httpx.Response) -> str:
    return flash_of(response.headers["location"])


@pytest.fixture(autouse=True)
def _master_key(monkeypatch):
    """Night copies are signed with a key derived from the master key: a throwaway one."""
    from cryptography.fernet import Fernet

    monkeypatch.setenv("TOW_MASTER_KEY", Fernet.generate_key().decode("ascii"))


@pytest.fixture
def night(tmp_path):
    folder = tmp_path.parent / f"{tmp_path.name}-night"
    cfg = load_config()
    cfg["backup_dir"] = str(folder)
    save_config(cfg)
    return folder


def test_defaults_are_inside_the_install():
    assert resolve("", NIGHT) == config_path().resolve().parent / "backup" / "night"
    assert resolve("", MANUAL).name == "restore-points"
    assert resolve("copies", NIGHT) == config_path().resolve().parent / "copies"  # relative = moves with TOW
    if sys.platform == "win32":  # a share is a path only on Windows (problem() refuses it elsewhere)
        assert resolve(r"\\nas\share\TOW", NIGHT) == __import__("pathlib").Path(r"\\nas\share\TOW")


@pytest.mark.parametrize(
    ("value", "reason"),
    [
        (r"C:\Windows\TOW", "Windows"),  # protected on Windows, another system's path elsewhere
        ("..\\up", ".."),
        ("a\x01b", "недопустимые"),
    ],
)
def test_bad_folders_are_refused(value, reason, monkeypatch):
    monkeypatch.setenv("SystemRoot", r"C:\Windows")
    found = problem(value, NIGHT)
    assert found is not None
    assert reason in found


def test_a_folder_of_another_system_in_config_is_refused_never_created(tmp_path):
    """A config carried from Windows to Linux (backup_dir: D:\\Backups) - or the other way
    round - must not become a folder named "D:\\Backups" inside the install."""
    from tow.locations import _of_another_system
    from tow.restore_points import RestorePointError, create_restore_point, list_restore_points
    from tow.snapshots import SnapshotError, create_snapshot

    other = next(value for value in (r"D:\Backups", "/mnt/backups") if _of_another_system(value))
    cfg = load_config()
    cfg["backup_dir"] = other
    save_config(cfg)

    with pytest.raises(SnapshotError) as night:
        create_snapshot()
    assert str(night.value) == t("locations.other_system_config", title=NIGHT.title, key="backup_dir", value=other)

    cfg = load_config()
    cfg.pop("backup_dir")
    cfg["restore_points_dir"] = other
    save_config(cfg)
    with pytest.raises(RestorePointError) as manual:
        create_restore_point()
    assert "restore_points_dir" in str(manual.value)
    with pytest.raises(RestorePointError):
        list_restore_points()
    with pytest.raises(SnapshotError):  # a night copy carries the restore points: said, not skipped
        create_snapshot()
    assert not any(  # nothing created inside the install
        "Backups" in p.name or p.name == "mnt" for p in tmp_path.rglob("*")
    )


@pytest.mark.parametrize("key", ["backup_dir", "restore_points_dir"])
def test_configured_backup_folder_cannot_escape_install_with_dotdot(key, tmp_path):
    from tow.restore_points import RestorePointError, restore_points_dir
    from tow.snapshots import SnapshotError, backup_root

    cfg = load_config()
    cfg[key] = "../outside"
    save_config(cfg)
    error = SnapshotError if key == "backup_dir" else RestorePointError
    with pytest.raises(error):
        backup_root() if key == "backup_dir" else restore_points_dir()
    assert not (tmp_path.parent / "outside").exists()


@pytest.mark.parametrize("key", ["backup_dir", "restore_points_dir"])
def test_configured_backup_folder_rechecks_protected_roots(key, monkeypatch):
    from tow import platform
    from tow.restore_points import RestorePointError, restore_points_dir
    from tow.snapshots import SnapshotError, backup_root

    monkeypatch.setenv("SystemRoot", r"C:\Windows")
    path = r"C:\Windows\TOW" if platform.is_windows() else "/etc/tow"
    location = NIGHT if key == "backup_dir" else MANUAL
    reason = problem(path, location)
    assert reason is not None
    cfg = load_config()
    cfg[key] = path
    save_config(cfg)
    error = SnapshotError if key == "backup_dir" else RestorePointError
    with pytest.raises(error, match=re.escape(reason)):
        backup_root() if key == "backup_dir" else restore_points_dir()


def test_night_copies_never_inside_the_data_folder():
    from tow.paths import data_dir

    assert problem(str(data_dir() / "x"), NIGHT) == "ночные копии должны лежать вне папки данных TOW"
    assert problem(str(data_dir() / "x"), MANUAL) is None


def test_write_check(tmp_path):
    assert check_writable(tmp_path / "new" / "deeper") is None
    assert (tmp_path / "new" / "deeper").is_dir()
    assert not list((tmp_path / "new" / "deeper").iterdir())  # the probe file is gone
    blocker = tmp_path / "file"
    blocker.write_text("x", encoding="utf-8")
    assert check_writable(blocker / "sub") is not None


@pytest.mark.parametrize(
    ("code", "key"),
    [
        (errno.EINVAL, "locations.bad_name"),  # Windows: < > : " | ? * in a folder name
        (errno.ENAMETOOLONG, "locations.name_too_long"),
        (errno.ENOSPC, "locations.no_space"),
        (errno.EIO, "locations.folder_unavailable"),
    ],
)
def test_a_folder_the_system_refuses_is_named_in_the_pages_words(monkeypatch, tmp_path, code, key):
    def refuse(*_a, **_k):
        raise OSError(code, "The filename, directory name, or volume label syntax is incorrect")

    monkeypatch.setattr(Path, "mkdir", refuse)

    reason = check_writable(tmp_path / "bad")

    assert reason == t(key, "ru")
    assert "syntax" not in reason


def test_save_check_and_reset_a_folder_from_settings(tmp_path):
    target = tmp_path.parent / f"{tmp_path.name}-copies"
    c = _client()
    checked = c.post(
        "/settings/backup/location",
        data={"kind": "night", "path": str(target), "action": "check"},
        follow_redirects=False,
    )
    assert _flash(checked).startswith(f"{target}: запись работает")
    assert "backup_dir" not in load_config()  # a check saves nothing

    saved = c.post(
        "/settings/backup/location",
        data={"kind": "night", "path": str(target), "action": "save"},
        follow_redirects=False,
    )
    assert _flash(saved).startswith(f"папка ночных копий: {target}")
    assert load_config()["backup_dir"] == str(target)
    page = c.get("/settings").text
    assert f'value="{target}"' in page

    reset = c.post("/settings/backup/location", data={"kind": "night", "action": "default"}, follow_redirects=False)
    assert "по умолчанию" in _flash(reset)
    assert f"прежние копии остались в {target}" in _flash(reset)
    assert "backup_dir" not in load_config()


def test_unknown_or_bad_folder_kind(monkeypatch):
    monkeypatch.setenv("SystemRoot", r"C:\Windows")
    c = _client()
    assert (
        _flash(c.post("/settings/backup/location", data={"kind": "x"}, follow_redirects=False)) == "неизвестная папка"
    )
    bad = c.post("/settings/backup/location", data={"kind": "night", "path": r"C:\Windows\x"}, follow_redirects=False)
    assert "не подходит" in _flash(bad)


def test_backup_now_and_its_status(night):
    from tow.snapshots import status

    response = _client().post("/settings/backup/now", follow_redirects=False)
    assert _flash(response).startswith("копия сделана: ")
    st = status()
    assert st["last_snapshot"].startswith("tow-")
    assert st["last_error"] == ""
    page = _client().get("/settings").text
    assert "Ночная копия " in page
    assert 'action="/settings/backup/night/' in page  # this PC can restore it


def test_failed_copy_is_recorded_and_shown(tmp_path):
    from tow.snapshots import SnapshotError, create_snapshot, status

    blocker = tmp_path.parent / f"{tmp_path.name}-blocker"
    blocker.write_text("x", encoding="utf-8")
    cfg = load_config()
    cfg["backup_dir"] = str(blocker / "night")
    save_config(cfg)
    with pytest.raises(SnapshotError):
        create_snapshot()
    assert status()["last_error"].startswith(t("backup.snapshot.cannot_write", reason=""))
    page = _client().get("/settings").text
    assert ") не удалась: " in page


def test_a_signed_in_device_restores_a_night_copy_too(night, monkeypatch):
    # Owner's decision (01.10.2026): a device signed in from the network is the owner.
    from tow.auth import issue_session
    from tow.clock import format_ui_timestamp
    from tow.snapshots import create_snapshot, list_snapshots

    name = __import__("pathlib").Path(create_snapshot()["snapshot"]).name
    monkeypatch.setenv("TOW_LAN_AUTH_TOKEN", "t" * 32)
    cfg = load_config()
    cfg["allow_lan"] = True
    save_config(cfg)
    lan = _client(client=("192.168.1.7", 50000), cookies={"tow_session": issue_session("t" * 32)})
    done = lan.post(f"/settings/backup/night/{name}/restore", follow_redirects=False)
    # The copy's date as the list shows it, not TOW's folder name (QA 1.24.1).
    at = format_ui_timestamp(list_snapshots()[0]["created_at"])
    assert _flash(done).startswith(f"восстановлено из ночной копии от {at}")
    assert name not in _flash(done).split(";")[0]
    bad = _client().post("/settings/backup/night/..%5Cevil/restore", follow_redirects=False)
    assert bad.status_code in (303, 404)


def test_a_night_restore_says_what_happens_to_network_access(night):
    """QA 1.24.1: the restore changed nothing visible about network access and said nothing; the
    owner could not tell whether devices on the network still reach TOW."""
    from tow.auth import lan_password_record
    from tow.snapshots import create_snapshot
    from tow.store import load_secrets, save_secrets

    secrets = load_secrets()
    secrets["lan_auth"] = lan_password_record("a-long-password")
    save_secrets(secrets)
    cfg = load_config()
    cfg.update(allow_lan=True, bind="0.0.0.0")
    save_config(cfg)
    name = Path(create_snapshot()["snapshot"]).name
    page = _client().get("/settings").text
    assert "Доступ по сети и его пароль не изменятся." in page  # in the restore confirmation
    done = _client().post(f"/settings/backup/night/{name}/restore", follow_redirects=False)
    assert load_config()["allow_lan"] is True
    assert _flash(done).endswith("; доступ по сети остался как был")


def test_a_night_restore_says_when_network_access_was_turned_off(night, monkeypatch):
    """The settings in force could not be read: the restore makes TOW local-only, and says so."""
    from tow.snapshots import create_snapshot

    cfg = load_config()
    cfg.update(allow_lan=True, bind="0.0.0.0")
    save_config(cfg)
    name = Path(create_snapshot()["snapshot"]).name
    real = __import__("tow.snapshots", fromlist=["restore_snapshot"]).restore_snapshot

    def restore_as_if_unreadable(path, *, apply=False):
        result = real(path, apply=apply)
        after = load_config()
        after.update(allow_lan=False, bind="127.0.0.1")  # what _keep_local_access does then
        save_config(after)
        return result

    monkeypatch.setattr("tow.web.services.restore_snapshot", restore_as_if_unreadable)
    done = _client().post(f"/settings/backup/night/{name}/restore", follow_redirects=False)
    assert "доступ по сети выключен" in _flash(done)
    assert "/settings" in done.headers["location"]


def test_regular_copies_follow_their_folder(tmp_path):
    from tow.restore_points import create_restore_point, list_restore_points

    folder = tmp_path.parent / f"{tmp_path.name}-manual"
    cfg = load_config()
    cfg["restore_points_dir"] = str(folder)
    save_config(cfg)
    point = create_restore_point()
    assert (folder / f"{point['id']}.towx").is_file()
    assert [p["id"] for p in list_restore_points()] == [point["id"]]


# --- the watchdog -----------------------------------------------------------------------------


def test_watchdog_reports_a_failed_or_missing_night_copy(night):
    from tow.snapshots import create_snapshot, record_failure
    from tow.store import save_state
    from tow.watchdog import BACKUP_STALE_SEC, run_watchdog

    now = {"t": datetime.now(UTC).timestamp()}
    save_state({"topics": [], "health": {"auto_at_ts": int(now["t"])}})
    sent: list[str] = []

    def run():
        save_state({"topics": [], "health": {"auto_at_ts": int(now["t"])}})
        return run_watchdog(
            is_healthy=lambda _p: True,
            deploy_running=lambda: False,
            send=lambda text: sent.append(text) or True,
            now=lambda: now["t"],
            sleep=lambda s: now.__setitem__("t", now["t"] + s),
        )

    assert run()["backup_ok"] is True  # watching starts now
    now["t"] += BACKUP_STALE_SEC + 60
    assert run()["backup_ok"] is False
    assert (
        sent[-1].splitlines()[-1]
        == "TOW: ночная копия ещё ни разу не делалась — проверьте папку копий в Настройках → Резервные копии"
    )

    create_snapshot()
    now["t"] = datetime.now(UTC).timestamp() + 60
    assert run()["backup_ok"] is True
    assert sent[-1].splitlines()[-1] == "TOW: ночные копии снова делаются"

    record_failure("cannot write snapshot: диск отключён")
    now["t"] += 600
    assert run()["backup_ok"] is False
    assert sent[-1].splitlines()[-1].startswith("TOW: ночная копия не удалась: cannot write snapshot: диск отключён")


@pytest.mark.parametrize("value", [r"D:\TOW-backup", r"\\nas\share\TOW", "/mnt/backup/tow"])
def test_a_path_of_another_system_is_refused_not_made_relative(value, monkeypatch):
    import os

    from tow.i18n import t

    # No real lookup of the server name: realpath of \\nas\... asks the network on Windows (~3 s).
    real = os.path.realpath
    monkeypatch.setattr(os.path, "realpath", lambda p, **kw: str(p) if str(p).startswith("\\\\") else real(p, **kw))
    absolute_here = Path(value).is_absolute()
    found = problem(value, NIGHT)
    if absolute_here:
        assert found != t("locations.other_system")
    else:  # D:\... on Linux would become <install>/D:\TOW-backup; /mnt/... on Windows the current drive
        assert found == t("locations.other_system")


def test_system_folders_are_named_the_way_this_system_has_them():
    from tow.i18n import t

    expected = t("locations.other_system") if sys.platform == "win32" else t("locations.protected_system")
    assert problem("/etc/tow", NIGHT) == expected


def test_a_network_share_is_named_only_on_this_computer(monkeypatch):
    # From a network session even the check would make Windows sign in to that server (NTLM)
    # with the owner's account, and night copies would go there.
    from tow import locations, platform
    from tow.auth import issue_session
    from tow.platform.windows import WindowsBackend

    share = r"\\files\share\tow"
    touched: list[object] = []
    monkeypatch.setattr(locations, "problem", lambda *_args: None)  # the share's syntax is fine everywhere
    monkeypatch.setattr("tow.web.services.folder_write_problem", lambda path: touched.append(path))
    monkeypatch.setattr("tow.web.services.free_bytes", lambda _path, state=None: None)
    monkeypatch.setenv("TOW_LAN_AUTH_TOKEN", "t" * 32)
    cfg = load_config()
    cfg["allow_lan"] = True
    save_config(cfg)
    lan = _client(client=("192.168.1.7", 50000), cookies={"tow_session": issue_session("t" * 32)})
    with platform.use(WindowsBackend()):
        for action in ("check", "save"):
            for path in (share, share.replace("\\", "/")):
                refused = lan.post(
                    "/settings/backup/location",
                    data={"kind": "night", "path": path, "action": action},
                    follow_redirects=False,
                )
                assert _flash(refused) == t("web.backup.share_local_only", "ru")
        assert touched == []
        assert "backup_dir" not in load_config()

        saved = _client().post(
            "/settings/backup/location", data={"kind": "night", "path": share, "action": "save"}, follow_redirects=False
        )
    assert _flash(saved).startswith("папка ночных копий: ")
    assert load_config()["backup_dir"] == share
    assert len(touched) == 1


def test_a_share_named_from_the_network_is_never_opened(monkeypatch):
    """The refusal comes before the folder rules: problem() resolves the folder of night copies
    (os.path.realpath), and on Windows that already opens the share and signs in to it."""
    import os

    from tow import platform
    from tow.auth import issue_session
    from tow.platform.windows import WindowsBackend

    share = r"\\files\share\tow"
    opened: list[str] = []
    real = os.path.realpath

    def realpath(path, *args, **kwargs):
        if str(path).replace("/", "\\").startswith("\\\\"):
            opened.append(str(path))
            return str(path)  # never the real call: it would reach the network
        return real(path, *args, **kwargs)

    monkeypatch.setattr(os.path, "realpath", realpath)
    monkeypatch.setenv("TOW_LAN_AUTH_TOKEN", "t" * 32)
    cfg = load_config()
    cfg["allow_lan"] = True
    save_config(cfg)
    lan = _client(client=("192.168.1.7", 50000), cookies={"tow_session": issue_session("t" * 32)})
    with platform.use(WindowsBackend()):
        for action in ("check", "save"):
            for path in (share, share.replace("\\", "/")):
                refused = lan.post(
                    "/settings/backup/location",
                    data={"kind": "night", "path": path, "action": action},
                    follow_redirects=False,
                )
                assert _flash(refused) == t("web.backup.share_local_only", "ru")
    assert opened == []
    assert "backup_dir" not in load_config()


def test_only_a_windows_share_counts_as_one():
    from tow import platform
    from tow.locations import is_network_share
    from tow.platform.posix import PosixBackend
    from tow.platform.windows import WindowsBackend

    with platform.use(WindowsBackend()):
        assert all(map(is_network_share, [r"\\nas\share", "//nas/share", '"\\\\nas\\x"']))
        assert not any(map(is_network_share, [r"D:\backup", "backup/night", ""]))
    with platform.use(PosixBackend("linux")):
        assert not is_network_share("//srv/backup")


# --- the volume a backup folder was recorded on (an unmounted disk is never written to) ------------


def _disk(monkeypatch, identity):
    """The volume every folder is on now (None: it cannot be told)."""
    from tow import platform

    current = {"id": identity}
    monkeypatch.setattr(
        platform, "volume_of", lambda path: platform.Volume(current["id"], "local") if current["id"] else None
    )
    return current


def _volumes():
    from tow.store import load_state

    return load_state().get("backup_volumes") or {}


def test_a_verified_night_copy_records_its_folders_volume(night, monkeypatch):
    from tow.snapshots import create_snapshot

    _disk(monkeypatch, "usb-disk")
    create_snapshot()
    assert _volumes()["night"] == {"path": str(resolve(str(night), NIGHT)), "id": "usb-disk"}


def test_a_night_copy_is_never_written_to_another_volume(night, monkeypatch):
    from tow.snapshots import SnapshotError, create_snapshot, list_snapshots, status

    disk = _disk(monkeypatch, "usb-disk")
    create_snapshot()
    before = sorted(path.name for path in night.iterdir())
    disk["id"] = "system-disk"  # unmounted: the folder is on the disk below now
    with pytest.raises(SnapshotError) as refused:
        create_snapshot()
    assert str(refused.value) == t("locations.volume_missing", "ru", title=NIGHT.title)
    assert sorted(path.name for path in night.iterdir()) == before  # nothing written, nothing created
    assert status()["last_error"] == str(refused.value)
    assert isinstance(_volumes()["night"]["unseen_at"], int)
    assert _volumes()["night"]["id"] == "usb-disk"  # never rebound by itself
    disk["id"] = None  # the volume cannot be told: refused as well
    with pytest.raises(SnapshotError):
        create_snapshot()
    disk["id"] = "usb-disk"  # back: copies go on, the note goes
    create_snapshot()
    assert "unseen_at" not in _volumes()["night"]
    assert len(list_snapshots()) == 2


def test_a_restore_point_is_never_written_to_another_volume(tmp_path, monkeypatch):
    from tow.restore_points import RestorePointError, create_restore_point

    folder = tmp_path.parent / f"{tmp_path.name}-points"
    cfg = load_config()
    cfg["restore_points_dir"] = str(folder)
    save_config(cfg)
    disk = _disk(monkeypatch, "usb-disk")
    create_restore_point()
    assert _volumes()["manual"]["id"] == "usb-disk"
    disk["id"] = "system-disk"
    for path in folder.iterdir():
        path.unlink()
    folder.rmdir()  # the unmounted disk took its folder along
    with pytest.raises(RestorePointError, match=re.escape(t("locations.volume_missing", "ru", title=MANUAL.title))):
        create_restore_point()
    assert not folder.exists()  # not created on the disk below
    assert isinstance(_volumes()["manual"]["unseen_at"], int)


def test_free_space_of_a_backup_folder_off_its_volume_is_unknown(night, monkeypatch):
    from tow.locations import free_bytes
    from tow.snapshots import create_snapshot

    disk = _disk(monkeypatch, "usb-disk")
    path = resolve(str(night), NIGHT)
    assert isinstance(free_bytes(path), int)  # no record yet: as before
    create_snapshot()
    assert isinstance(free_bytes(path), int)
    disk["id"] = "system-disk"
    assert free_bytes(path) is None  # never the disk below's space
    disk["id"] = None
    assert free_bytes(path) is None


def test_a_moved_backup_folder_records_anew_and_a_damaged_record_matches_nothing(night, monkeypatch, tmp_path):
    from tow.locations import recorded_volume, volume_unseen
    from tow.store import load_state, save_state

    _disk(monkeypatch, "usb-disk")
    state = load_state()
    state["backup_volumes"] = {"night": {"path": str(tmp_path / "elsewhere"), "id": "old"}}
    path = resolve(str(night), NIGHT)
    assert recorded_volume(state, NIGHT, path) is None  # a record of another folder: none
    state["backup_volumes"] = {"night": {"path": str(path), "id": 7}}
    assert recorded_volume(state, NIGHT, path) == ""
    assert volume_unseen(state, NIGHT, path)  # damaged: nothing matches it
    save_state(state)


def test_an_import_keeps_no_backup_volumes_of_the_exporting_computer():
    from tow.bundle.importer import _forget_volumes

    state = {
        "topics": [{"id": "1", "volume_unseen": {"folder": "/srv/tv", "at": 1}}, {"id": "2"}],
        "backup_volumes": {"night": {"path": "/mnt/b", "id": "ext4 /mnt/b"}},
    }
    _forget_volumes(state)
    assert state == {"topics": [{"id": "1"}, {"id": "2"}]}
