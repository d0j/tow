"""Settings must not turn TOW into a probe of the home network: ntfy servers, the client check."""

from __future__ import annotations

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from helpers import flash_of

from tow.config import load_config, save_config
from tow.i18n import t
from tow.store import load_secrets, save_secrets
from tow.web import app

TOPIC = "tow-abcdef123456"


@pytest.fixture(autouse=True)
def _throwaway_master_key(monkeypatch):
    monkeypatch.setenv("TOW_MASTER_KEY", Fernet.generate_key().decode("ascii"))


def _client() -> TestClient:
    return TestClient(app, headers={"Origin": "http://127.0.0.1"})


def _flash(response) -> str:
    return flash_of(response.headers["location"])


def _save_ntfy(server: str):
    return _client().post("/settings/notifier/ntfy", data={"topic": TOPIC, "server": server}, follow_redirects=False)


@pytest.mark.parametrize(
    "server",
    ["https://192.168.1.10", "https://127.0.0.1:8080", "https://[::1]/", "https://router.lan", "https://100.64.1.2"],
)
def test_an_ntfy_server_inside_the_home_network_is_refused(server):
    response = _save_ntfy(server)

    assert t("web.settings.notifier_private_host", "ru") in _flash(response)
    assert "ntfy" not in (load_secrets().get("notifiers") or {})


def test_a_name_that_resolves_to_the_home_network_is_refused_too(monkeypatch):
    monkeypatch.setattr(
        "tow.web.site_form._resolve_addresses", lambda name: ["192.168.1.5"] if name == "evil.example" else []
    )

    assert t("web.settings.notifier_private_host", "ru") in _flash(_save_ntfy("https://evil.example"))


def test_an_ntfy_server_that_does_not_resolve_is_saved_with_a_note(monkeypatch):
    monkeypatch.setattr("tow.web.site_form._resolve_addresses", lambda _name: [])

    message = _flash(_save_ntfy("https://ntfy.blocked.example"))

    assert t("web.settings.notifier_private_host", "ru") not in message
    assert "ntfy.blocked.example" in message
    assert load_secrets()["notifiers"]["ntfy"]["server"] == "https://ntfy.blocked.example"


def test_a_public_ntfy_server_is_saved(monkeypatch):
    monkeypatch.setattr("tow.web.site_form._resolve_addresses", lambda _name: ["93.184.216.34"])
    _save_ntfy("https://ntfy.example.org")

    assert load_secrets()["notifiers"]["ntfy"]["server"] == "https://ntfy.example.org"


def test_an_own_server_at_home_can_be_allowed():
    cfg = load_config()
    cfg["allow_private_notifier_hosts"] = True
    save_config(cfg)

    _save_ntfy("https://192.168.1.10")

    assert load_secrets()["notifiers"]["ntfy"]["server"] == "https://192.168.1.10"


def test_check_does_not_post_to_a_home_server_saved_by_an_older_version(monkeypatch):
    save_secrets({"notifiers": {"ntfy": {"topic": TOPIC, "server": "https://192.168.1.10"}}})
    monkeypatch.setattr("tow.notifiers.test", lambda *_a: pytest.fail("must not send"))

    response = _client().post("/settings/notifier/ntfy/test", follow_redirects=False)

    assert _flash(response) == t("web.settings.notifier_private_host", "ru")


class _LoginFailed(Exception):  # qbittorrentapi.LoginFailed by name
    pass


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (
            ConnectionError(
                "HTTPConnectionPool(host='192.168.1.5', port=22): Max retries exceeded "
                "(Caused by NewConnectionError: [WinError 10061] No connection could be made)"
            ),
            "web.settings.ping_unreachable",
        ),
        (TimeoutError("timed out connecting to 192.168.1.5:3389"), "web.settings.ping_unreachable"),
        (_LoginFailed("login failed for admin@192.168.1.5"), "web.settings.ping_login"),
    ],
)
def test_the_client_check_names_a_class_not_the_socket_text(monkeypatch, error, expected):
    class Broken:
        def ping(self):
            raise error

    monkeypatch.setattr("tow.clients.factory.from_secrets", lambda *_a: Broken())

    flash = _flash(_client().post("/settings/client/ping", data={"client_id": ""}, follow_redirects=False))

    assert flash == t("web.settings.ping_failed", "ru", error=t(expected, "ru"))
    assert "192.168.1.5" not in flash


def test_the_client_check_keeps_tows_own_plain_words(monkeypatch):
    from tow.clients.managed import ClientError

    class Broken:
        def ping(self):
            raise ClientError("Transmission: неверный логин или пароль")

    monkeypatch.setattr("tow.clients.factory.from_secrets", lambda *_a: Broken())

    flash = _flash(_client().post("/settings/client/ping", data={"client_id": ""}, follow_redirects=False))

    assert flash == t("web.settings.ping_failed", "ru", error="Transmission: неверный логин или пароль")
