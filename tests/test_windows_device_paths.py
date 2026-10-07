"""Windows namespaces must not reach filesystem probes or torrent clients."""

from itertools import product
from pathlib import PureWindowsPath

import pytest
from fastapi.testclient import TestClient
from helpers import flash_of

from tow import folders, locations
from tow.config import load_config, save_config
from tow.i18n import t
from tow.paths import config_path
from tow.store import load_state, save_state

PREFIXES = [f"{a}{b}{marker}{c}" for marker in "?." for a, b, c in product("/\\", repeat=3)]


@pytest.mark.parametrize("prefix", PREFIXES)
@pytest.mark.parametrize("tail", ["C:/Windows/tow-fixture", "UNC/server/share/tow-fixture"])
def test_windows_parses_every_separator_variant_as_a_namespace(prefix, tail):
    assert str(PureWindowsPath(prefix + tail)).startswith(("\\\\?\\", "\\\\.\\"))


@pytest.mark.parametrize("prefix", PREFIXES)
@pytest.mark.parametrize("allow_unc", [False, True])
def test_client_path_syntax_rejects_namespace_even_when_shares_are_allowed(prefix, allow_unc):
    assert folders.save_path_problem(prefix + "C:/Media", allow_unc=allow_unc) == t("folders.device_paths")


@pytest.mark.parametrize("prefix", PREFIXES)
def test_standalone_folder_policy_rejects_namespace_before_filesystem_lookup(prefix, monkeypatch):
    monkeypatch.setattr(folders, "_real_path", lambda _p: pytest.fail("namespace filesystem lookup"))
    path = prefix + "C:/Windows/tow-fixture"
    assert folders.protected_kind(path) == "windows"
    assert folders.save_path_policy_problem(path) is not None


@pytest.mark.parametrize("prefix", PREFIXES)
@pytest.mark.parametrize("location", [locations.NIGHT, locations.MANUAL])
def test_backup_folder_rejects_namespace_before_path_resolution(prefix, location, monkeypatch):
    monkeypatch.setattr(locations, "resolve", lambda *_a: pytest.fail("namespace path resolution"))
    raw = prefix + "C:/Windows/tow-fixture"
    assert locations.problem(raw, location) == t("locations.bad_chars")
    assert locations.problem(f'  "{raw}"  ', location) == t("locations.bad_chars")
    with pytest.raises(locations.LocationError):
        locations.resolve_checked(raw, location)


@pytest.mark.parametrize("prefix", PREFIXES)
@pytest.mark.parametrize("kind", ["night", "manual"])
@pytest.mark.parametrize("action", ["check", "save"])
def test_web_backup_rejects_namespace_without_probe_or_config_write(prefix, kind, action, monkeypatch):
    from tow.web import app

    monkeypatch.setattr("tow.web.services.folder_write_problem", lambda *_a: pytest.fail("namespace write probe"))
    before = config_path().read_bytes()
    response = TestClient(app, headers={"Origin": "http://127.0.0.1"}).post(
        "/settings/backup/location",
        data={"kind": kind, "path": prefix + "C:/Windows/tow-fixture", "action": action},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert t("locations.bad_chars") in flash_of(response.headers["location"])
    assert config_path().read_bytes() == before
    assert locations.LOCATIONS[kind].key not in load_config()


@pytest.mark.parametrize("prefix", PREFIXES)
@pytest.mark.parametrize("action", ["add", "edit"])
def test_web_topic_rejects_namespace_before_client_or_state_mutation(prefix, action, monkeypatch):
    from tow.web import app, services

    cfg = load_config()
    cfg["allow_unc_save_paths"] = True
    save_config(cfg)
    topic = {"id": "fixture", "title": "Show A", "url": "http://rutor.info/torrent/1234567/show"}
    # Even keeping an already stored device path must not bypass the syntax check.
    path = prefix + "C:/Media"
    if action == "edit":
        save_state({"topics": [{**topic, "save_path": path}]})
    before = load_state()
    monkeypatch.setattr(services, "run_check", lambda **_kw: pytest.fail("client check attempted"))
    monkeypatch.setattr(services, "save_state", lambda *_a: pytest.fail("topic state mutation attempted"))
    route = "/topics/add" if action == "add" else "/topics/fixture/edit"
    client = TestClient(app, headers={"Origin": "http://127.0.0.1"})
    response = client.post(
        route,
        data={"url": topic["url"], "title": topic["title"], "save_path": path},
        follow_redirects=False,
    )
    assert response.status_code == 303
    message = (
        client.get(response.headers["location"]).text if action == "add" else flash_of(response.headers["location"])
    )
    assert t("folders.device_paths") in message
    assert load_state() == before


@pytest.mark.parametrize("path", ["C:/Media", r"D:\Media", "/downloads/tv", r"\\nas\media", "//nas/media"])
def test_ordinary_client_paths_remain_accepted(path):
    assert folders.save_path_problem(path, allow_unc=True) is None


@pytest.mark.parametrize(
    ("raw", "kind"),
    [
        ("NUL", "device"),
        ("CON.txt", "device"),
        (r"backup\COM1.log", "device"),
        ("backup/LPT1.", "device"),
        ("COM¹", "device"),
        ("conin$", "device"),
        ("night:ads", "stream"),
        (r"D:\copies\night::$INDEX_ALLOCATION", "stream"),
        (r"\\localhost\C$\Windows\Temp", "admin_share"),
        ("//127.0.0.1/c$/Users/x", "admin_share"),
        (r"\\server\ADMIN$\x", "admin_share"),
    ],
)
@pytest.mark.parametrize("location", [locations.NIGHT, locations.MANUAL])
def test_backup_folder_refuses_windows_devices_streams_and_admin_shares(raw, kind, location, monkeypatch):
    from tow import platform
    from tow.platform.windows import WindowsBackend

    monkeypatch.setattr(locations, "check_writable", lambda *_a: pytest.fail("write probe of a device"))
    with platform.use(WindowsBackend()):
        assert locations.problem(raw, location) == t(f"locations.windows_{kind}")
        with pytest.raises(locations.LocationError):
            locations.resolve_checked(raw, location)


@pytest.mark.parametrize(
    "raw", ["copies", r"D:\Backups\TOW", r"\\nas\share\tow", r"\\nas\backups$\tow", "console", "com10", "nul-copies"]
)
def test_plain_backup_folders_stay_accepted_on_windows(raw):
    from tow import platform
    from tow.platform.windows import WindowsBackend

    assert folders.windows_name_problem(raw) is None
    with platform.use(WindowsBackend()):
        assert locations.problem(raw, locations.MANUAL) in (None, t("locations.other_system"))


def test_relative_backup_folder_remains_relative_to_install():
    assert locations.problem("copies", locations.MANUAL) is None
    assert locations.resolve_checked("copies", locations.MANUAL) == locations.install_dir() / "copies"
