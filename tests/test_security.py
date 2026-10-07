"""Security decisions of 01.10.2026: network access, hosts, save paths, headers."""

from __future__ import annotations

import json
import socket

import pytest
from fastapi.testclient import TestClient
from helpers import flash_kind, flash_of

from tow.folders import save_path_policy_problem
from tow.web import app

LAN = ("192.168.1.7", 50000)


@pytest.fixture
def lan_config(monkeypatch):
    monkeypatch.setattr(
        "tow.web.services.load_config",
        lambda: {"bind": "0.0.0.0", "allow_lan": True, "language": "ru"},
    )
    monkeypatch.setenv("TOW_LAN_AUTH_TOKEN", "t" * 32)


@pytest.mark.parametrize(
    ("host", "allowed"),
    [
        ("127.0.0.1:8787", True),
        ("localhost:8787", True),
        ("192.168.1.2:8787", True),
        ("192.168.1.5:8787", True),
        ("100.64.1.1:8787", True),  # Tailscale (CGNAT range)
        ("[fe80::1]:8787", True),
        ("85.64.1.2:8787", False),  # a public address: the request came from the internet
        ("8.8.8.8", False),
        ("evil.example:8787", False),  # DNS rebinding
    ],
)
def test_only_local_network_host_names_are_accepted(host, allowed):
    response = TestClient(app).get("/healthz", headers={"Host": host})
    assert (response.status_code == 200) is allowed, response.status_code


@pytest.mark.parametrize(
    ("peer", "allowed"),
    [
        ("192.168.1.7", True),
        ("192.168.1.5", True),
        ("100.64.1.1", True),  # Tailscale (CGNAT range) is not the internet
        ("fd7a:115c:a1e0::1", True),  # Tailscale over IPv6 (ULA)
        ("fe80::1", True),
        ("85.64.1.2", False),  # an internet client with a home-network Host header
        ("8.8.8.8", False),
        ("2a00:1450:4001::1", False),
        ("::ffff:85.64.1.2", False),  # the same IPv4 client on a dual-stack socket
    ],
)
def test_internet_peers_are_refused_whatever_the_host_header_says(lan_config, peer, allowed):
    response = TestClient(app, client=(peer, 50000)).get("/healthz", headers={"Host": "192.168.1.2:8787"})
    assert (response.status_code == 200) is allowed, (response.status_code, response.text)
    if not allowed:
        assert response.text == "untrusted host"


def test_healthz_names_the_install_only_to_this_computer(lan_config):
    from tow.supervisor.layout import install_id

    local = TestClient(app, client=("127.0.0.1", 50000)).get("/healthz").json()
    assert local == {"ok": True, "version": local["version"], "install": install_id()}
    remote = TestClient(app, client=("192.168.1.7", 50000)).get("/healthz", headers={"Host": "192.168.1.2:8787"})
    assert "install" not in remote.json()  # nothing about this computer's folders on the network


@pytest.fixture
def lan_auto_language(monkeypatch):
    from tow.config import load_config, save_config

    cfg = load_config()
    cfg.update(allow_lan=True, bind="0.0.0.0", language="auto")
    save_config(cfg)
    monkeypatch.setenv("TOW_LAN_AUTH_TOKEN", "t" * 32)


_RUSSIAN_BROWSER = {"Accept-Language": "ru-RU,ru;q=0.9", "Accept": "text/html", "Host": "192.168.1.2:8787"}


def _remembered() -> str | None:
    from tow.paths import data_dir

    path = data_dir() / "ui-language.json"
    return json.loads(path.read_text(encoding="utf-8"))["language"] if path.is_file() else None


@pytest.mark.parametrize(
    ("peer", "cookies"),
    [
        (LAN, {}),  # not signed in: sent to the sign-in page
        (LAN, {"tow_session": "forged.123.abcdefghijklmnopqrstuvwxyz"}),
        (("85.64.1.2", 50000), {}),  # refused: an internet peer
    ],
)
def test_a_refused_or_anonymous_request_does_not_change_the_owners_language(lan_auto_language, peer, cookies):
    response = TestClient(app, client=peer, cookies=cookies).get("/", headers=_RUSSIAN_BROWSER, follow_redirects=False)
    assert response.status_code in (303, 403)
    assert _remembered() is None


def test_the_login_page_from_the_network_does_not_change_it_either(lan_auto_language):
    response = TestClient(app, client=LAN).get("/login", headers=_RUSSIAN_BROWSER)
    assert response.status_code == 200
    assert '<html lang="ru">' in response.text  # the page itself is in the visitor's language
    assert _remembered() is None


def test_a_signed_in_device_and_this_pc_set_the_owners_language(lan_auto_language):
    from tow.auth import issue_session

    signed_in = TestClient(app, client=LAN, cookies={"tow_session": issue_session("t" * 32)})
    assert signed_in.get("/", headers=_RUSSIAN_BROWSER).status_code == 200
    assert _remembered() == "ru"
    TestClient(app).get("/", headers={"Accept-Language": "en", "Accept": "text/html"})
    assert _remembered() == "en"


def test_this_computers_own_name_is_accepted():
    name = socket.gethostname().lower()
    assert TestClient(app).get("/healthz", headers={"Host": f"{name}:8787"}).status_code == 200
    assert TestClient(app).get("/healthz", headers={"Host": f"{name}.local:8787"}).status_code == 200


def test_access_settings_change_only_on_this_computer(lan_config, monkeypatch):
    from tow.auth import issue_session

    session = issue_session("t" * 32)
    lan = TestClient(app, client=LAN, headers={"Origin": "http://127.0.0.1"}, cookies={"tow_session": session})
    response = lan.post("/settings/access", data={"allow_lan": "0"}, follow_redirects=False)
    assert response.status_code == 303
    assert "только на компьютере с TOW" in flash_of(response.headers["location"])
    assert flash_kind(response.headers["location"]) == "err"


def test_security_headers():
    headers = TestClient(app).get("/healthz").headers
    csp = headers["content-security-policy"]
    for directive in ("form-action 'self'", "base-uri 'none'", "object-src 'none'", "frame-ancestors 'none'"):
        assert directive in csp
    assert headers["x-frame-options"] == "DENY"


@pytest.mark.parametrize(
    "path",
    [
        r"C:\Windows\Temp\x",
        r"C:\Program Files\x",
        r"C:\ProgramData\Microsoft\Windows\Start Menu\Programs\StartUp",
        r"C:\Users\user\AppData\Roaming\Microsoft\Windows\Start Menu\Programs\Startup",
    ],
)
def test_system_folders_are_never_a_download_target(path, monkeypatch):
    monkeypatch.setenv("SystemRoot", r"C:\Windows")
    monkeypatch.setenv("ProgramFiles", r"C:\Program Files")
    monkeypatch.setenv("ProgramData", r"C:\ProgramData")
    monkeypatch.setenv("USERPROFILE", r"C:\Users\user")
    monkeypatch.setenv("APPDATA", r"C:\Users\user\AppData\Roaming")
    problem = save_path_policy_problem(path)
    assert problem is not None
    assert "AppData" in problem


@pytest.fixture
def windows_profile(monkeypatch):
    monkeypatch.setenv("SystemRoot", r"C:\Windows")
    monkeypatch.setenv("ProgramFiles", r"C:\Program Files")
    monkeypatch.setenv("ProgramData", r"C:\ProgramData")
    monkeypatch.setenv("USERPROFILE", r"C:\Users\user")
    monkeypatch.setenv("APPDATA", r"C:\Users\user\AppData\Roaming")


@pytest.mark.parametrize(
    "path",
    [
        r"C:\Users\user\AppData.\Roaming\Microsoft\Windows\Start Menu\Programs\Startup",
        r"C:\Users\user\AppData \Roaming\Microsoft\Windows\Start Menu\Programs\Startup",
        r"C:\Users\user\AppData\Roaming.\Microsoft",
        r"C:\Users\user\APPDAT~1\Roaming\Microsoft\Windows\Start Menu\Programs\Startup",
        r"C:\PROGRA~1\Evil",
        r"C:\Windows...\Temp",
    ],
)
def test_names_windows_reads_as_another_folder_are_refused(path, windows_profile):
    problem = save_path_policy_problem(path)
    assert problem is not None
    assert problem.startswith("имя папки «")


@pytest.mark.parametrize("path", [r"C:\Users\user\AppData.\Roaming\TOW", r"C:\WINDOWS\Backups", r"C:\PROGRA~1\TOW"])
def test_backup_folders_follow_the_same_rule(path, windows_profile):
    from tow.locations import NIGHT, problem

    assert problem(path, NIGHT) is not None  # judged before anything is created there


@pytest.mark.parametrize("path", [r"D:\Media\Show", r"M:\TV\Show S01", r"D:\Фильмы\2024.10 Season", "/downloads/tv"])
def test_ordinary_folders_still_pass(path, windows_profile):
    assert save_path_policy_problem(path) is None


def test_a_link_into_appdata_is_judged_by_where_it_really_leads(tmp_path, monkeypatch):
    import os
    import sys

    profile = tmp_path / "profile"
    roaming = profile / "AppData" / "Roaming"
    roaming.mkdir(parents=True)
    monkeypatch.setenv("USERPROFILE", str(profile))
    monkeypatch.setenv("APPDATA", str(roaming))
    monkeypatch.setenv("HOME", str(profile))
    link = tmp_path / "innocent"
    if sys.platform == "win32":
        import _winapi

        _winapi.CreateJunction(str(profile / "AppData"), str(link))
    else:
        os.symlink(profile / "AppData", link)

    problem = save_path_policy_problem(str(link / "Roaming" / "Startup"))

    assert problem is not None
    assert "AppData" in problem
    assert save_path_policy_problem(str(tmp_path / "media" / "Show")) is None


# "/ETC/x" is /etc only where case is ignored (macOS, and from Windows): tests/test_folders.py.
@pytest.mark.parametrize("path", ["/etc/cron.d", "/usr/local/bin/x", "/System/Library/x"])
def test_posix_system_folders_are_never_a_download_target(path):
    problem = save_path_policy_problem(path)
    assert problem is not None
    assert "/etc" in problem


@pytest.mark.parametrize("path", [r"M:\TV\New", r"E:\other", "/srv/media/new"])
def test_history_is_not_a_download_folder_allowlist(path):
    assert save_path_policy_problem(path) is None


def test_adding_a_topic_from_the_network_can_pick_a_new_folder(monkeypatch):
    from tow.auth import issue_session
    from tow.config import load_config, save_config
    from tow.store import load_state, save_state

    cfg = load_config()
    cfg.update(bind="0.0.0.0", allow_lan=True)
    save_config(cfg)
    monkeypatch.setenv("TOW_LAN_AUTH_TOKEN", "t" * 32)
    monkeypatch.setattr("tow.web.services.run_check", lambda **kw: {"qbit": "ok", "results": []})
    monkeypatch.setattr("tow.web.services.guess_topic_title", lambda *args, **kw: "Synthetic series")

    save_state({"topics": [{"id": "a", "url": "http://rutor.info/torrent/1/x", "save_path": r"M:\TV"}]})
    lan = TestClient(
        app, client=LAN, headers={"Origin": "http://127.0.0.1"}, cookies={"tow_session": issue_session("t" * 32)}
    )
    response = lan.post(
        "/topics/add",
        data={"url": "http://rutor.info/torrent/2/y", "title": "Y", "save_path": r"E:\Startup-ish"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert len(load_state()["topics"]) == 2
    assert load_state()["topics"][-1]["save_path"] == r"E:\Startup-ish"
    assert load_state()["save_roots"][0] == r"E:\Startup-ish"


def test_names_resolving_to_internal_addresses_count_as_internal(monkeypatch):
    from tow.web import site_form

    monkeypatch.setattr(
        site_form,
        "_resolve_addresses",
        lambda name: {"127.0.0.1.nip.io": ["127.0.0.1"], "rutor.info": ["104.21.1.1"]}.get(name, []),
    )
    assert site_form.internal_host("127.0.0.1.nip.io") is True
    assert site_form.internal_host("rutor.info") is False
    assert site_form.internal_host("router.home.arpa") is True
    assert site_form.internal_host("100.64.1.1") is True
