"""Settings shares connection waiting times and cannot acknowledge stale credentials."""

from copy import deepcopy
from types import SimpleNamespace

import pytest
from fastapi.testclient import TestClient
from helpers import flash_of

from tow import client_access
from tow.config import load_config
from tow.errors import TowError
from tow.store import load_secrets, load_state, save_secrets, save_state
from tow.web import app, services


@pytest.fixture
def connection(monkeypatch):
    save_secrets({"qbittorrent": {"host": "127.0.0.1", "port": 8080, "username": "owner", "password": "old"}})
    save_state({"topics": [], "health": {"qbit_ok": False}})
    calls = []
    monkeypatch.setattr(services, "client_answers", lambda _client: calls.append("socket") or True)
    return TestClient(app, headers={"Origin": "http://127.0.0.1"}), calls


def observe(code, **params):
    import time

    ctx = client_access.context(load_config(), load_secrets())
    state = load_state()
    client_access.observe(state, ctx, TowError("client.managed.http_error", code=code, **params), now=time.time())
    save_state(state)
    return ctx


def test_settings_test_during_server_pause_makes_no_request_or_false_manual_failure(connection, monkeypatch):
    client, calls = connection
    ctx = observe(429, retry_after=3600)
    before = deepcopy(load_state())
    monkeypatch.setattr(services, "client_from_secrets", lambda *_args: pytest.fail("no adapter during server pause"))
    result = client.post("/settings/client/ping", follow_redirects=False)
    assert result.status_code == 303
    assert "Клиент запросил паузу" in flash_of(result.headers["location"])
    after = load_state()
    assert calls == []
    assert after["health"] == before["health"]
    assert after["client_access"] == before["client_access"]
    assert client_access.capture(after, ctx) == ""


def test_a_real_manual_rate_limit_is_saved_and_the_next_click_waits(connection, monkeypatch):
    client, calls = connection

    def ping():
        calls.append("ping")
        raise TowError("client.managed.http_error", code=429, retry_after=3600)

    monkeypatch.setattr(services, "client_from_secrets", lambda *_args: SimpleNamespace(ping=ping))
    assert client.post("/settings/client/ping", follow_redirects=False).status_code == 303
    before = deepcopy(load_state()["client_access"])
    assert calls == ["socket", "ping"]
    assert client.post("/settings/client/ping", follow_redirects=False).status_code == 303
    assert calls == ["socket", "ping"]
    assert load_state()["client_access"] == before


def test_password_only_save_clears_old_read_refusal_without_rotating_command_authority(connection):
    client, _calls = connection
    old_context = observe(403)
    state = load_state()
    receipt = {"since": "2026-11-03T00:00:00+00:00", "resume_attempted": True, "blocked": "client.managed.http_error"}
    state["topics"] = [{"id": "a", "file_observation_revision": "same-generation", "verification_pending": receipt}]
    save_state(state)
    result = client.post(
        "/settings/client",
        data={"host": "127.0.0.1", "port": "8080", "username": "owner", "password": "new"},
        follow_redirects=False,
    )
    assert flash_of(result.headers["location"]) == "сохранено"
    assert load_secrets()["qbittorrent"]["password"] == "new"
    after = load_state()
    assert client_access.capture(after, old_context) == ""
    assert after["topics"][0]["file_observation_revision"] == "same-generation"
    assert after["topics"][0]["verification_pending"] == receipt


@pytest.mark.parametrize("fails", [False, True])
def test_a_late_test_from_old_credentials_cannot_replace_the_new_refusal(connection, monkeypatch, fails):
    client, _calls = connection
    observe(403)
    current = {}
    events = []
    monkeypatch.setattr(services, "log_event", lambda kind, **_fields: events.append(kind))

    def ping():
        secrets = load_secrets()
        secrets["qbittorrent"]["password"] = "new"
        save_secrets(secrets)
        current["ctx"] = observe(403)
        current["state"] = deepcopy(load_state())
        if fails:
            raise TowError("client.managed.http_error", code=403)
        return "synthetic"

    monkeypatch.setattr(services, "client_from_secrets", lambda *_args: SimpleNamespace(ping=ping))
    response = client.post("/settings/client/ping", follow_redirects=False)
    assert "Подключение изменилось" in flash_of(response.headers["location"])
    after = load_state()
    assert after["health"] == current["state"]["health"]
    assert after["client_access"] == current["state"]["client_access"]
    assert client_access.capture(after, current["ctx"])
    assert events == []


def test_successful_manual_check_releases_only_the_captured_read_refusal(connection, monkeypatch):
    client, calls = connection
    ctx = observe(403)
    state = load_state()
    state["topics"] = [{"id": "a", "repair_pending": {"resume_dispatched": True, "deadline": 1800000000}}]
    save_state(state)
    before = deepcopy(state["topics"])
    monkeypatch.setattr(services, "client_from_secrets", lambda *_args: SimpleNamespace(ping=lambda: "synthetic"))
    client.post("/settings/client/ping", follow_redirects=False)
    after = load_state()
    assert after["health"]["qbit_ok"] is True
    assert client_access.capture(after, ctx) == ""
    assert after["topics"] == before
    assert calls == ["socket"]


def test_a_removed_client_does_not_make_its_old_test_form_fail_the_server(connection):
    client, calls = connection
    response = client.post("/settings/client/ping", data={"client_id": "removed"}, follow_redirects=False)
    assert response.status_code == 303
    assert "Подключение изменилось" in flash_of(response.headers["location"])
    assert calls == []


@pytest.mark.parametrize("code", [429, 503])
@pytest.mark.parametrize("new_endpoint", [False, True])
def test_a_late_server_pause_applies_only_to_the_same_endpoint_after_password_change(
    connection, monkeypatch, code, new_endpoint
):
    import time

    client, _calls = connection
    current = {}

    def ping():
        secrets = load_secrets()
        secrets["qbittorrent"]["password"] = "new"
        if new_endpoint:
            secrets["qbittorrent"]["port"] = 9090
        save_secrets(secrets)
        current["ctx"] = observe(403)
        current["state"] = deepcopy(load_state())
        raise TowError("client.managed.http_error", code=code, retry_after=3600)

    monkeypatch.setattr(services, "client_from_secrets", lambda *_args: SimpleNamespace(ping=ping))
    client.post("/settings/client/ping", follow_redirects=False)
    after = load_state()
    assert after["health"] == current["state"]["health"]
    assert client_access.capture(after, current["ctx"]) == client_access.capture(current["state"], current["ctx"])
    pause = client_access.server_wait(after, current["ctx"], now=time.time())
    assert bool(pause) is not new_endpoint
    if pause:
        assert pause["not_before"] - time.time() > 3500
