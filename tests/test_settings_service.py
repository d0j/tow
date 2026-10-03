"""Settings → TOW service: autostart and restart of the one process (`tow run`)."""

from __future__ import annotations

from fastapi.testclient import TestClient
from helpers import shown

from tow.web import app


def _page(path: str = "/settings") -> str:
    return TestClient(app, headers={"Origin": "http://127.0.0.1"}).get(path).text


def _post(path: str, data: dict) -> str:
    response = TestClient(app, headers={"Origin": "http://127.0.0.1"}).post(path, data=data, follow_redirects=False)
    assert response.status_code == 303
    return shown(response.headers["location"])  # the address and the message it shows


def test_the_service_card_shows_one_process_and_its_autostart():
    page = _page()
    assert "TOW работает одним процессом" in page
    assert "Запускать TOW при входе в этот компьютер" in page
    # 1.21: the five Windows tasks and the switch from them are gone.
    assert 'action="/settings/service/migrate"' not in page
    assert "пятью задачами" not in page
    assert "задачами Windows" not in page
    assert "сам сторож не перезапускает TOW" in page


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
    assert "автозагрузка не подтверждена" in _post(
        "/settings/service/autostart", {"enabled": "1", "without_login": "1"}
    )
    assert calls == [(True, {"without_login": True})]

    hint = "loginctl enable-linger owner"
    monkeypatch.setattr("tow.web.services.set_autostart", lambda enabled, **kw: {"ok": True, "hint": hint})
    location = _post("/settings/service/autostart", {"enabled": "1", "without_login": "1"})
    assert "автозагрузка включена" in location
    assert hint in location


def test_a_refused_autostart_says_why(monkeypatch):
    monkeypatch.setattr("tow.web.services.set_autostart", lambda enabled, **kw: {"ok": False, "error": "чужая папка"})
    monkeypatch.setattr("tow.web.services.service_status", lambda: {"autostart": False})
    assert "автозагрузка не подтверждена: чужая папка" in _post("/settings/service/autostart", {"enabled": "1"})


def test_restart_without_tow_run_says_it_did_not_start():
    assert "перезапуск" in _post("/settings/service/restart", {}).lower()
