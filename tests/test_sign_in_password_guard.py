"""TOW's own sign-in password never lands in another secret field (1.33.1 QA-01).

A browser that saved TOW's network sign-in filled it, unasked, into the torrent client's password,
the messengers' tokens, webhooks and keys and the sites' passwords; a Save of the client card then
stored it as the client's password. Every secret field but TOW's own sign-in therefore asks for a
new password (a browser fills no saved login there), and every form that stores a secret refuses
TOW's sign-in password on the server too."""

from __future__ import annotations

import re
from pathlib import Path

import pytest
from bs4 import BeautifulSoup
from fastapi.testclient import TestClient
from helpers import flash_kind, flash_of

from tow import access
from tow.auth import lan_password_record
from tow.config import load_config, save_config
from tow.store import load_secrets, save_secrets, save_state
from tow.web import app

TEMPLATES = Path(__file__).resolve().parents[1] / "src" / "tow" / "templates"
SIGN_IN = "horse-battery-staple-9"
REFUSED = "это пароль для входа в TOW"
# The only fields a browser may fill with TOW's saved sign-in: the sign-in page and Settings'
# "current password" (which is TOW's password, asked to change it).
TOW_SIGN_IN_FIELDS = {("login.html", "lan-password"), ("settings.html", "current-password")}


@pytest.fixture
def signed_in_tow(monkeypatch):
    monkeypatch.setattr("tow.web.services.log_event", lambda *args, **kwargs: None)
    secrets = load_secrets()
    secrets["lan_auth"] = lan_password_record(SIGN_IN)
    secrets["trackers"] = {"rutor": {"username": "site-user", "password": "site-secret"}}
    secrets["telegram"] = {"token": "123456:saved-token", "chat_ids": ["1001"]}
    save_secrets(secrets)
    cfg = load_config()
    cfg["trackers"] = {
        "rutor": {"title": "rutor", "url_regex": r"rutor/(\d+)"},
        "club": {"title": "Club", "url_regex": r"https://club\.example/t(\d+)", "login_path": "/login"},
    }
    save_config(cfg)
    save_state({"topics": [{"id": "t1", "title": "Show", "url": "https://club.example/t1"}], "mirrors": {}})
    return TestClient(app, headers={"Origin": "http://127.0.0.1"})


def _refused(response) -> None:
    assert response.status_code == 303
    location = response.headers["location"]
    assert REFUSED in flash_of(location), flash_of(location)
    assert flash_kind(location) == "err"
    assert SIGN_IN not in location


def test_the_sign_in_password_is_recognised_through_its_hash_only():
    record = lan_password_record(SIGN_IN)
    secrets = {"lan_auth": record}
    assert access.is_sign_in_secret(secrets, SIGN_IN)
    assert access.is_sign_in_secret(secrets, "", f" {SIGN_IN} ")  # a site's password is stored stripped
    assert not access.is_sign_in_secret(secrets, "another-password", "")
    assert not access.is_sign_in_secret({}, SIGN_IN)  # no sign-in at all: nothing to compare with
    assert not access.is_sign_in_secret(None, SIGN_IN)  # secrets that cannot be read
    assert SIGN_IN not in str(record)


def test_the_external_access_key_counts_as_the_sign_in_too(monkeypatch):
    monkeypatch.setenv("TOW_LAN_AUTH_TOKEN", "an-external-access-key-0123456789")
    assert access.is_sign_in_secret({}, "an-external-access-key-0123456789")
    assert not access.is_sign_in_secret({}, "an-external-access-key-012345678")


def test_a_client_save_with_the_sign_in_password_is_refused(signed_in_tow):
    before = load_secrets()
    response = signed_in_tow.post(
        "/settings/client",
        data={"kind": "qbittorrent", "host": "127.0.0.1", "port": "8080", "username": "admin", "password": SIGN_IN},
        follow_redirects=False,
    )
    _refused(response)
    assert load_secrets() == before


def test_a_messenger_save_with_the_sign_in_password_is_refused(signed_in_tow):
    before = load_secrets()
    response = signed_in_tow.post(
        "/settings/notifier/telegram", data={"token": SIGN_IN, "chat_ids": "1001"}, follow_redirects=False
    )
    _refused(response)
    assert load_secrets() == before
    for kind, field in (("discord", "webhook_url"), ("whatsapp", "apikey")):
        _refused(signed_in_tow.post(f"/settings/notifier/{kind}", data={field: SIGN_IN}, follow_redirects=False))
    assert load_secrets() == before


def test_site_saves_with_the_sign_in_password_are_refused(signed_in_tow):
    before = load_secrets()
    added = signed_in_tow.post(
        "/sites/new",
        data={"name": "other", "url_regex": r"other/(\d+)", "username": "u", "password": SIGN_IN},
        follow_redirects=False,
    )
    assert added.status_code == 303
    assert SIGN_IN not in added.headers["location"]
    form = signed_in_tow.get(added.headers["location"]).text  # the add form again, with the reason
    assert REFUSED in form
    assert SIGN_IN not in form
    _refused(signed_in_tow.post("/sites/rutor", data={"username": "u", "password": SIGN_IN}, follow_redirects=False))
    _refused(
        signed_in_tow.post("/sites/rutor/login", data={"username": "u", "password": SIGN_IN}, follow_redirects=False)
    )
    _refused(
        signed_in_tow.post(
            "/topics/t1/tracker-login", data={"username": "u", "password": SIGN_IN}, follow_redirects=False
        )
    )
    assert load_secrets() == before
    assert "other" not in (load_config().get("trackers") or {})


def test_another_password_is_still_saved(signed_in_tow):
    response = signed_in_tow.post(
        "/sites/rutor/login", data={"username": "u", "password": "a-site-password"}, follow_redirects=False
    )
    assert response.status_code == 303
    assert REFUSED not in flash_of(response.headers["location"])
    assert load_secrets()["trackers"]["rutor"]["password"] == "a-site-password"


def _password_inputs() -> list[tuple[str, str, str]]:
    found = []
    for path in sorted(TEMPLATES.glob("*.html")):
        source = re.sub(r"\{#.*?#\}", "", path.read_text(encoding="utf-8"), flags=re.DOTALL)  # comments
        for tag in re.findall(r"<input\b[^>]*>", source):
            if 'type="password"' not in tag:
                continue
            field_id = re.search(r'id="([^"]*)"', tag)
            autocomplete = re.search(r'autocomplete="([^"]*)"', tag)
            found.append(
                (path.name, field_id.group(1) if field_id else "", autocomplete.group(1) if autocomplete else "")
            )
    return found


def test_only_tows_own_sign_in_fields_may_be_filled_with_a_saved_login():
    """Every other secret field (a client, a messenger, a site, a sign-in on Home) asks for a new
    password: browsers do not fill a saved login there ("off" is ignored for a password field)."""
    fields = _password_inputs()
    assert len(fields) >= 9, fields
    for template, field_id, autocomplete in fields:
        if (template, field_id) in TOW_SIGN_IN_FIELDS:
            assert autocomplete == "current-password", (template, field_id)
        else:
            assert autocomplete == "new-password", (template, field_id, autocomplete)


def test_the_rendered_secret_fields_ask_for_a_new_password(signed_in_tow):
    settings = BeautifulSoup(signed_in_tow.get("/settings").text, "html.parser")
    sites = BeautifulSoup(signed_in_tow.get("/sites?open=rutor").text, "html.parser")
    for page in (settings, sites):
        for field in page.select('input[type="password"]'):
            if field["id"] == "current-password":
                assert field["autocomplete"] == "current-password"
            else:
                assert field["autocomplete"] == "new-password", field["id"]
        for field in page.select('input[name="username"]'):  # no login pairs with TOW's own sign-in
            assert field["autocomplete"] == "off", field["id"]
    assert settings.select_one("#nf-telegram-token")["autocomplete"] == "new-password"
