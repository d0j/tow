"""Settings → TOW service: autostart and restart through the supervisor (`tow run`)."""

from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from helpers import shown

from tow.web import app


def _page(path: str = "/settings") -> str:
    return TestClient(app, headers={"Origin": "http://127.0.0.1"}).get(path).text


def _post(path: str, data: dict) -> str:
    response = TestClient(app, headers={"Origin": "http://127.0.0.1"}).post(path, data=data, follow_redirects=False)
    assert response.status_code == 303
    return shown(response.headers["location"])  # the address and the message it shows


def _service_card(page: str) -> str:
    return page.split('id="acc-service"', 1)[1].split('id="acc-language"', 1)[0]


def test_the_service_card_shows_one_service_and_its_autostart():
    page = _page()
    card = _service_card(page)
    # 1.32: the card names the service and offers its autostart; what the service runs (the
    # page, checks, copies, watchdog) is architecture, not a setting, and was taken out.
    assert ">Сервис TOW<" in card
    assert 'action="/settings/service/autostart"' in card
    assert ">Запускать TOW при входе в систему<" in card
    # 1.21: the five Windows tasks and the switch from them are gone.
    assert 'action="/settings/service/migrate"' not in page
    assert "пятью задачами" not in page
    assert "задачами Windows" not in page
    # The watchdog only reports; nothing but the service restarts TOW.
    assert "Сторож сообщает о простое в мессенджеры; сам TOW он не перезапускает." in card


@pytest.mark.parametrize(
    ("language", "title", "removed", "incorrect"),
    [
        ("en", "TOW service", "TOW runs as one service", "TOW runs as one process"),
        ("ru", "Сервис TOW", "TOW работает как единый сервис", "TOW работает одним процессом"),
    ],
)
def test_service_card_names_the_service_not_a_single_process(language, title, removed, incorrect):
    from tow.config import load_config, save_config

    config = load_config()
    config["language"] = language
    save_config(config)
    page = _page()
    assert f">{title}<" in _service_card(page)
    assert removed not in page  # the architecture sentence is gone (Settings say each thing once)
    assert incorrect not in page


def test_the_switch_route_is_gone():
    response = TestClient(app, headers={"Origin": "http://127.0.0.1"}).post(
        "/settings/service/migrate", data={"action": "apply"}, follow_redirects=False
    )
    assert response.status_code in (404, 405)


def test_start_without_signing_in_is_offered_where_it_exists(monkeypatch):
    def status():
        return {
            "pid": 1,
            "autostart": True,
            "without_login": False,
            "supports_without_login": True,
            "autostart_detail": {"where": "TOW"},
            "restart": None,
        }

    monkeypatch.setattr("tow.web.services.service_status", status)
    page = _page()
    assert 'id="service-without-login"' in page
    assert "Запускать без входа в систему" in page


def test_without_signing_in_counts_only_when_read_back(monkeypatch):
    calls = []
    monkeypatch.setattr(
        "tow.web.services.set_autostart", lambda enabled, **kw: calls.append((enabled, kw)) or {"ok": True}
    )
    monkeypatch.setattr(
        "tow.web.services.service_status",
        lambda: {"autostart": True, "without_login": False, "supports_without_login": True},
    )
    assert "автозапуск не подтверждён" in _post("/settings/service/autostart", {"enabled": "1", "without_login": "1"})
    assert calls == [(True, {"without_login": True})]

    hint = "loginctl enable-linger owner"
    monkeypatch.setattr("tow.web.services.set_autostart", lambda enabled, **kw: {"ok": True, "hint": hint})
    location = _post("/settings/service/autostart", {"enabled": "1", "without_login": "1"})
    assert "автозапуск включён" in location
    assert hint in location


def test_a_refused_autostart_says_why(monkeypatch):
    monkeypatch.setattr("tow.web.services.set_autostart", lambda enabled, **kw: {"ok": False, "error": "чужая папка"})
    monkeypatch.setattr("tow.web.services.service_status", lambda: {"autostart": False})
    assert "автозапуск не подтверждён: чужая папка" in _post("/settings/service/autostart", {"enabled": "1"})


def test_restart_without_tow_run_says_it_did_not_start():
    assert "перезапуск" in _post("/settings/service/restart", {}).lower()


@pytest.mark.parametrize(("language", "expected"), [("en", "TOW restart requested"), ("ru", "Запрошен перезапуск TOW")])
def test_accepted_restart_log_describes_a_request_not_completed_work(monkeypatch, language, expected):
    from tow.log import format_event, read_events

    monkeypatch.setattr("tow.web.services.request_restart", lambda: {"ok": True, "operation_id": "restart-test"})
    _post("/settings/service/restart", {})
    records = [row for row in read_events(limit=10) if row["kind"] == "settings_service_restart"]
    assert len(records) == 1
    assert records[0]["operation_id"] == "restart-test"
    monkeypatch.setattr("tow.log.owner_language", lambda: language)
    assert format_event(records[0])["label"] == expected
