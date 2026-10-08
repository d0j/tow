"""tow.access: one way to set the password, sign in and out, and keep actions to this computer.

The external access key (TOW_LAN_AUTH_TOKEN / _FILE) is an explicit alternative for an install
with no password record; it never stands in for a damaged password record or unreadable secrets.
"""

from __future__ import annotations

import pytest
from fastapi.routing import iter_route_contexts
from fastapi.testclient import TestClient
from helpers import flash_of, open_network, raises_code

from tow import access
from tow.auth import SESSION_COOKIE, _sessions_state, issue_session, lan_password_record, lan_password_session_key
from tow.config import load_config
from tow.i18n import t
from tow.store import SecretStoreError, load_secrets, save_secrets
from tow.web import app

LAN = ("192.168.1.7", 50000)
ORIGIN = {"Origin": "http://127.0.0.1"}
KEY = "k" * 40
DAMAGED = {"scheme": "pbkdf2-sha256", "iterations": 5, "salt": "x", "digest": "y"}


def _lan(**kwargs) -> TestClient:
    return TestClient(app, client=LAN, headers=ORIGIN, **kwargs)


# --------------------------------------------------------------------------- the credential


def test_the_access_key_counts_only_while_no_password_is_set(monkeypatch):
    monkeypatch.setenv("TOW_LAN_AUTH_TOKEN", KEY)

    assert access.credential({}).kind == "token"
    record = lan_password_record("a-long-password")
    chosen = access.credential({"lan_auth": record})
    assert (chosen.kind, chosen.session_key) == ("password", lan_password_session_key(record))
    assert not chosen.matches(KEY)


def test_a_damaged_password_never_falls_back_to_the_key(monkeypatch):
    monkeypatch.setenv("TOW_LAN_AUTH_TOKEN", KEY)

    with raises_code("auth.record_invalid"):
        access.credential({"lan_auth": DAMAGED})
    with raises_code("auth.record_invalid"):
        access.credential({"lan_auth": None})


def test_unreadable_secrets_fail_closed(monkeypatch):
    monkeypatch.setenv("TOW_LAN_AUTH_TOKEN", KEY)

    with raises_code("auth.store_unavailable"):
        access.credential(None)


def test_no_password_and_no_key_is_no_credential():
    with raises_code("auth.token_missing"):
        access.credential({})


# --------------------------------------------------------------------------- over HTTP


def test_signing_in_with_the_access_key_when_no_password_is_set(monkeypatch):
    open_network(monkeypatch, token=KEY)
    lan = _lan()

    assert "Ключ доступа" in lan.get("/login").text
    signed_in = lan.post("/login", data={"password": KEY}, follow_redirects=False)

    assert signed_in.status_code == 303
    assert SESSION_COOKIE in signed_in.cookies
    assert lan.get("/settings").status_code == 200


def test_with_a_password_set_the_access_key_is_refused(monkeypatch):
    open_network(monkeypatch, token=KEY)
    save_secrets({"lan_auth": lan_password_record("a-long-password")})
    lan = _lan()

    assert lan.post("/login", data={"password": KEY}, follow_redirects=False).status_code == 401
    assert lan.post("/login", data={"password": "a-long-password"}, follow_redirects=False).status_code == 303


def test_a_damaged_password_refuses_every_device_and_says_why(monkeypatch):
    open_network(monkeypatch, token=KEY)
    save_secrets({"lan_auth": DAMAGED})
    lan = _lan()

    assert lan.get("/settings").status_code == 503
    page = lan.get("/", headers={"Accept": "text/html"})  # a browser is shown the sign-in page
    assert page.url.path == "/login"
    assert t("auth.record_invalid", "ru") in page.text
    refused = lan.post("/login", data={"password": KEY}, follow_redirects=False)
    assert refused.status_code == 503
    assert SESSION_COOKIE not in refused.cookies
    old_key_session = issue_session(KEY)
    assert _lan(cookies={SESSION_COOKIE: old_key_session}).get("/settings").status_code == 503
    # This computer still opens, and sets a new password without the old one.
    local = TestClient(app, headers=ORIGIN)
    assert local.get("/settings").status_code == 200
    local.post("/settings/password", data={"lan_password": "new-password-1", "lan_password2": "new-password-1"})
    assert access.password_is_set(load_secrets())


def test_unreadable_secrets_refuse_the_network_with_their_own_reason(monkeypatch):
    open_network(monkeypatch, token=KEY)

    def broken():
        raise SecretStoreError("cannot decrypt TOW secrets")

    monkeypatch.setattr("tow.web.services.load_secrets", broken)
    lan = _lan(cookies={SESSION_COOKIE: issue_session(KEY)})

    assert lan.get("/settings").status_code == 503
    assert t("auth.store_unavailable", "ru") in lan.get("/login").text


# --------------------------------------------------------------------------- this computer only


def _local_only_routes() -> set[tuple[str, str]]:
    found = set()
    for route in iter_route_contexts(app.routes):  # the routers' routes, flattened
        dependant = getattr(route, "dependant", None)
        calls = [dependency.call for dependency in getattr(dependant, "dependencies", [])]
        if any(getattr(call, "__name__", "") == "only_this_computer" for call in calls):
            found.update((method, route.path) for method in route.methods)
    return found


def test_the_local_only_routes_declare_it():
    """Turning network access on/off and the first-start page: the dependency, not an if."""
    assert _local_only_routes() == {("GET", "/setup"), ("POST", "/setup"), ("POST", "/settings/access")}


def test_a_signed_in_device_cannot_use_a_local_only_route():
    open_network()
    record = lan_password_record("a-long-password")
    save_secrets({"lan_auth": record})
    lan = _lan(cookies={SESSION_COOKIE: issue_session(lan_password_session_key(record))})

    closed = lan.post("/settings/access", data={"allow_lan": ""}, follow_redirects=False)
    setup = lan.post("/setup", data={"action": "skip"}, follow_redirects=False)

    assert flash_of(closed.headers["location"]) == t("web.settings.access_local_only", "ru")
    assert load_config()["allow_lan"] is True  # still open
    assert (setup.status_code, setup.headers["location"]) == (303, "/")
    assert lan.get("/setup", follow_redirects=False).headers["location"] == "/"


# --------------------------------------------------------------------------- one way to set it


def test_every_way_to_set_the_password_signs_every_device_out(monkeypatch):
    from tow import cli

    epoch = _sessions_state()[0]
    answers = iter(["cli-pass-word", "cli-pass-word"])
    monkeypatch.setattr("getpass.getpass", lambda _prompt="": next(answers))
    monkeypatch.setattr("builtins.input", lambda _prompt="": "")
    assert cli.main(["password"]) == 0
    assert _sessions_state()[0] == epoch + 1

    local = TestClient(app, headers=ORIGIN)
    local.post("/settings/password", data={"lan_password": "web-pass-word", "lan_password2": "web-pass-word"})
    assert _sessions_state()[0] == epoch + 2
    assert load_config()["setup_done"] is True


def test_set_password_checks_both_fields():
    with raises_code("web.password.differ"):
        access.set_password("one-password", "another-one")
    assert "lan_auth" not in load_secrets()


@pytest.mark.parametrize("scheme", ["http", "https"])
def test_the_session_cookie_is_issued_the_same_way_everywhere(monkeypatch, scheme):
    open_network(monkeypatch, token=KEY)
    client = TestClient(
        app, client=LAN, base_url=f"{scheme}://192.168.1.2:8787", headers={"Origin": f"{scheme}://192.168.1.2:8787"}
    )

    cookie = client.post("/login", data={"password": KEY}, follow_redirects=False).headers["set-cookie"].lower()

    assert "httponly" in cookie
    assert "samesite=lax" in cookie
    assert "path=/" in cookie
    assert ("secure" in cookie.split("; ")) is (scheme == "https")


# --------------------------------------------------------------------------- signing out


def _signed_in_lan() -> tuple[TestClient, str]:
    open_network()
    record = lan_password_record("a-long-password")
    save_secrets({"lan_auth": record})
    cookie = issue_session(lan_password_session_key(record))
    return _lan(cookies={SESSION_COOKIE: cookie}), cookie


def test_signing_out_revokes_the_session_not_only_the_cookie():
    lan, cookie = _signed_in_lan()
    assert lan.get("/settings").status_code == 200

    assert lan.post("/logout", follow_redirects=False).status_code == 303

    replayed = _lan(cookies={SESSION_COOKIE: cookie})  # the old cookie, kept by someone
    assert replayed.get("/settings", follow_redirects=False).status_code != 200


def test_the_device_that_signs_out_everywhere_stays_signed_in():
    lan, cookie = _signed_in_lan()
    other = _lan(cookies={SESSION_COOKIE: issue_session(lan_password_session_key(load_secrets()["lan_auth"]))})
    assert other.get("/settings").status_code == 200

    answer = lan.post("/settings/sessions/sign-out", follow_redirects=False)

    assert answer.status_code == 303
    assert answer.cookies.get(SESSION_COOKIE) not in (None, cookie)  # a new session for this device
    assert lan.get("/settings", follow_redirects=False).status_code == 200
    assert other.get("/settings", follow_redirects=False).status_code != 200  # every other one signs in again
    assert _lan(cookies={SESSION_COOKIE: cookie}).get("/settings", follow_redirects=False).status_code != 200


def test_a_damaged_password_record_is_not_a_password():
    assert access.password_record({"lan_auth": DAMAGED}) is None
    assert access.password_is_set({"lan_auth": DAMAGED}) is False
    assert access.password_record({"lan_auth": {**DAMAGED, "iterations": 600_000}}) is None
    record = lan_password_record("a-long-password")
    assert access.password_record({"lan_auth": record}) == record
    assert access.password_record(None) is None
