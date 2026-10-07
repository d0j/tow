"""Signed-in devices: logout survives a restart, "Sign out everywhere", nothing grows without end."""

from __future__ import annotations

import json

import pytest
from cryptography.fernet import Fernet
from fastapi.testclient import TestClient
from helpers import shown

from tow import auth
from tow.auth import (
    SESSION_TTL_SEC,
    clear_sessions,
    issue_session,
    lan_password_record,
    lan_password_session_key,
    revoke_session,
    session_is_valid,
    sign_out_everywhere,
)
from tow.config import load_config, save_config
from tow.i18n import t
from tow.paths import data_dir
from tow.store import load_secrets, save_secrets
from tow.web import app

TOKEN = "t" * 32
LAN = ("192.168.1.7", 50000)
ORIGIN = {"Origin": "http://127.0.0.1"}
HTML = {"Accept": "text/html"}


@pytest.fixture(autouse=True)
def _throwaway_master_key(monkeypatch):
    monkeypatch.setenv("TOW_MASTER_KEY", Fernet.generate_key().decode("ascii"))


@pytest.fixture
def password():
    record = lan_password_record("old-horse-battery")
    save_secrets({"lan_auth": record})
    cfg = load_config()
    cfg.update(allow_lan=True, bind="0.0.0.0")
    save_config(cfg)
    return record


def _device(record) -> TestClient:
    return TestClient(
        app, client=LAN, headers=ORIGIN, cookies={"tow_session": issue_session(lan_password_session_key(record))}
    )


def test_cookies_from_before_this_version_stay_valid():
    cookie = issue_session(TOKEN, now=100.0)  # no sessions.json yet: epoch 0, the old signature
    assert not (data_dir() / "sessions.json").exists()
    assert session_is_valid(cookie, TOKEN, now=200.0)


def test_a_logout_survives_a_restart():
    cookie = issue_session(TOKEN)
    revoke_session(cookie, TOKEN)
    clear_sessions()  # a restarted TOW (or another TOW process) knows it too

    assert not session_is_valid(cookie, TOKEN)
    assert session_is_valid(issue_session(TOKEN), TOKEN)
    stored = json.loads((data_dir() / "sessions.json").read_text(encoding="utf-8"))
    assert cookie.split(".")[0] not in json.dumps(stored)  # only a hash of the id is kept


def test_signed_out_sessions_are_forgotten_once_they_would_have_expired():
    first = issue_session(TOKEN, now=1000.0)
    revoke_session(first, TOKEN, now=1000.0)
    later = 1000.0 + SESSION_TTL_SEC + 120
    revoke_session(issue_session(TOKEN, now=later), TOKEN, now=later)

    stored = json.loads((data_dir() / "sessions.json").read_text(encoding="utf-8"))
    assert len(stored["revoked"]) == 1


def test_too_many_sign_outs_sign_everyone_out_instead_of_growing(monkeypatch):
    monkeypatch.setattr(auth, "_MAX_REVOKED", 3)
    keeper = issue_session(TOKEN)
    for _ in range(4):
        revoke_session(issue_session(TOKEN), TOKEN)

    stored = json.loads((data_dir() / "sessions.json").read_text(encoding="utf-8"))
    assert stored == {"epoch": 1, "revoked": {}}
    assert not session_is_valid(keeper, TOKEN)


def test_made_up_cookies_are_never_remembered_and_cannot_sign_everyone_out(monkeypatch):
    monkeypatch.setattr(auth, "_MAX_REVOKED", 3)
    keeper = issue_session(TOKEN)
    session_id, expires, _signature = issue_session(TOKEN).split(".")
    forged = [f"{session_id[:-2]}{n:02d}.{expires}.{'A' * 43}" for n in range(10)]
    forged += [issue_session("x" * 32), issue_session(TOKEN, now=1.0)]  # another key; long expired

    for cookie in forged:
        revoke_session(cookie, TOKEN)

    assert not (data_dir() / "sessions.json").exists()
    assert session_is_valid(keeper, TOKEN)


def test_a_forged_logout_from_this_computer_changes_nothing(password):
    device = issue_session(lan_password_session_key(password))
    local = TestClient(app, headers=ORIGIN)
    session_id, expires, _signature = device.split(".")

    for n in range(5):
        local.cookies.set("tow_session", f"{session_id[:-2]}{n:02d}.{expires}.{'A' * 43}")
        assert local.post("/logout", follow_redirects=False).status_code == 303

    assert not (data_dir() / "sessions.json").exists()
    assert session_is_valid(device, lan_password_session_key(password))
    phone = _device(password)
    assert phone.post("/logout", follow_redirects=False).status_code == 303  # a real one still signs out
    assert len(json.loads((data_dir() / "sessions.json").read_text(encoding="utf-8"))["revoked"]) == 1


def test_sign_out_everywhere_invalidates_every_session_but_not_new_ones():
    before = issue_session(TOKEN)
    sign_out_everywhere()
    clear_sessions()

    assert not session_is_valid(before, TOKEN)
    assert session_is_valid(issue_session(TOKEN), TOKEN)


def test_an_unreadable_sessions_file_fails_closed():
    sign_out_everywhere()
    cookie = issue_session(TOKEN)
    (data_dir() / "sessions.json").write_text("{broken", encoding="utf-8")

    assert not session_is_valid(cookie, TOKEN)


def test_a_sign_in_after_a_damaged_sessions_file_holds(password):
    """Round-3 audit: with data/sessions.json damaged every sign-in answered 303 with a cookie
    signed for epoch 0 that the next page refused (epoch unknown): a silent loop that locked
    out every device on the network. The sign-in rewrites the file once and says so."""
    from tow.log import read_events

    old = issue_session(lan_password_session_key(password))
    (data_dir() / "sessions.json").write_text("{broken", encoding="utf-8")
    device = TestClient(app, client=LAN, headers=ORIGIN)

    response = device.post("/login", data={"password": "old-horse-battery"}, follow_redirects=False)

    assert response.status_code == 303
    assert device.get("/settings", headers=HTML, follow_redirects=False).status_code == 200
    stored = json.loads((data_dir() / "sessions.json").read_text(encoding="utf-8"))
    assert stored["epoch"] > 1  # one no earlier session was signed with
    assert not session_is_valid(old, lan_password_session_key(password))  # still closed for the old ones
    second = TestClient(app, client=("192.168.1.8", 50000), headers=ORIGIN)
    assert second.post("/login", data={"password": "old-horse-battery"}, follow_redirects=False).status_code == 303
    assert device.get("/settings", headers=HTML, follow_redirects=False).status_code == 200  # not reset again
    assert [event["kind"] for event in read_events(limit=20)].count("sessions_reset") == 1


def test_a_sessions_file_that_cannot_be_read_refuses_the_sign_in_with_a_reason(password, monkeypatch):
    from pathlib import Path

    (data_dir() / "sessions.json").write_text('{"epoch": 2, "revoked": {}}\n', encoding="utf-8")
    clear_sessions()
    real = Path.read_bytes

    def denied(self):
        if self.name == "sessions.json":
            raise PermissionError(13, "Access is denied")
        return real(self)

    monkeypatch.setattr(Path, "read_bytes", denied)
    device = TestClient(app, client=LAN, headers=ORIGIN)

    response = device.post("/login", data={"password": "old-horse-battery"}, follow_redirects=False)

    assert response.status_code == 503
    assert "tow_session" not in response.cookies
    assert t("auth.sessions_unreadable", "ru") in response.text


def test_the_session_cookie_format_is_pinned():
    """A cookie a device got from an earlier TOW must keep working after an update: id, expiry
    and an HMAC-SHA256 of both (base64url, no padding) keyed by the password's digest, and for
    epoch N > 0 by that key + "\\x00tow-session-epoch-N"."""
    payload = "Q" * 43 + ".1900000000"
    now = 1_900_000_000 - 1000
    epoch_0 = f"{payload}.AwuFW0qNwP5LvNPjHytjFOemS9jq5UMB9BUXgyXm6DQ"
    epoch_3 = f"{payload}.U0CkIBCNE9N6togk86pb7Vi9OlZh1jAGdWbN1_AxU78"
    assert session_is_valid(epoch_0, TOKEN, now=now)
    assert not session_is_valid(epoch_3, TOKEN, now=now)
    (data_dir() / "sessions.json").write_text('{"epoch": 3, "revoked": {}}\n', encoding="utf-8")
    assert session_is_valid(epoch_3, TOKEN, now=now)
    assert not session_is_valid(epoch_0, TOKEN, now=now)
    session_id, expires, signature = issue_session(TOKEN, now=now).split(".")
    assert (len(session_id), int(expires), len(signature)) == (43, now + SESSION_TTL_SEC, 43)


def test_a_file_held_for_a_moment_keeps_the_last_known_state(monkeypatch):
    from pathlib import Path

    sign_out_everywhere()
    cookie = issue_session(TOKEN)
    assert session_is_valid(cookie, TOKEN)
    (data_dir() / "sessions.json").write_text('{"epoch": 1, "revoked": {}}\n', encoding="utf-8")  # changed on disk
    real = Path.read_bytes

    def held(self):
        if self.name == "sessions.json":
            raise PermissionError(13, "The process cannot access the file")
        return real(self)

    monkeypatch.setattr(Path, "read_bytes", held)
    assert session_is_valid(cookie, TOKEN)


def test_the_settings_button_signs_out_every_other_device(password):
    phone, laptop = _device(password), _device(password)
    assert phone.get("/settings", headers=HTML, follow_redirects=False).status_code == 200
    page = phone.get("/settings").text
    assert 'action="/settings/sessions/sign-out"' in page
    assert t("settings.access.sign_out_all", "ru") in page

    response = phone.post("/settings/sessions/sign-out", follow_redirects=False)

    assert t("web.password.signed_out", "ru") in shown(response.headers["location"])
    assert phone.get("/settings", headers=HTML, follow_redirects=False).status_code == 200  # this one stays
    assert laptop.get("/settings", headers=HTML, follow_redirects=False).headers["location"] == "/login"
    assert load_secrets()["lan_auth"] == password  # the password is the same


def test_the_button_is_shown_only_with_a_password():
    page = TestClient(app).get("/settings").text
    assert 'action="/settings/sessions/sign-out"' not in page


def test_an_anonymous_device_cannot_sign_everyone_out(password):
    owner = _device(password)
    stranger = TestClient(app, client=("192.168.1.8", 50000), headers=ORIGIN)

    assert stranger.post("/settings/sessions/sign-out", follow_redirects=False).status_code == 401
    assert owner.get("/settings", headers=HTML, follow_redirects=False).status_code == 200


def test_undoing_a_password_change_does_not_bring_old_sessions_back(password):
    old_device = issue_session(lan_password_session_key(password))
    local = TestClient(app, headers=ORIGIN)
    local.post("/settings/password", data={"lan_password": "new-cat-garden", "lan_password2": "new-cat-garden"})
    local.post("/undo")

    assert load_secrets()["lan_auth"]["digest"] == password["digest"]  # the old password is back ...
    assert not session_is_valid(old_device, lan_password_session_key(password))  # ... its sessions are not
