import json
import re

import pytest
from fastapi.testclient import TestClient

from tow.web import app


@pytest.mark.parametrize("language", ["ru", "en"])
@pytest.mark.parametrize("custom_minutes", [None, 30])
def test_settings_explains_global_and_individual_timers(language, custom_minutes):
    from tow.config import load_config, save_config
    from tow.store import load_state, save_state

    cfg = load_config()
    cfg.update(language=language, interval_sec=7200)
    save_config(cfg)
    state = load_state()
    state["topics"] = [
        {
            "id": "fixture",
            "url": "https://tracker.example/topic/1234567",
            "save_path": "D:/TV",
            "check_interval_min": custom_minutes,
        }
    ]
    save_state(state)

    page = TestClient(app, headers={"Origin": "http://127.0.0.1"}).get("/settings").text
    interval = page.split('id="acc-intervals"', 1)[1].split('id="acc-access"', 1)[0]
    if language == "ru":
        assert '<label for="interval_min">Общий таймер</label>' in interval
        assert "Активные раздачи без своего таймера проверяются раз в 2 ч." in interval
        assert "Свой таймер задаётся при добавлении или редактировании раздачи." in interval
        assert "Каждая раздача проверяется" not in interval
    else:
        assert '<label for="interval_min">Global timer</label>' in interval
        assert "Active topics without their own timer are checked every 2 h." in interval
        assert "Set an individual timer when adding or editing a topic." in interval
        assert "Every watched topic is checked" not in interval


def test_settings_ui_is_sectioned_and_explains_actions():
    client = TestClient(app, headers={"Origin": "http://127.0.0.1"})

    page = client.get("/settings").text
    css_response = client.get("/static/app.css")
    css = css_response.text

    assert '<h1 id="page-title">Настройки</h1>' in page
    assert 'href="/static/app.css?v=' in page
    assert 'src="/static/app.js?v=' in page
    assert "20260924-settings" not in page
    assert css_response.headers["cache-control"] == "no-cache"
    assert client.get("/settings").headers["referrer-policy"] == "same-origin"
    assert 'class="settings-section settings-accordion acc" id="acc-clients"' in page
    assert 'class="settings-section settings-accordion acc" id="acc-bots"' in page
    assert 'class="settings-section settings-accordion acc" id="acc-access"' in page
    assert 'class="settings-section settings-accordion acc" id="acc-service"' in page
    assert 'class="settings-section settings-accordion acc settings-log" id="acc-log"' in page
    accordion_tags = re.findall(r'<details class="settings-section settings-accordion[^>]+>', page)
    assert len(accordion_tags) == 8  # language, clients, notifications, checks, access, service, backups, log
    assert all(" open" not in tag for tag in accordion_tags)
    assert "Проверяются сохранённые настройки." in page
    assert "После «Сохранить» появится кнопка «Проверить» — она отправит пробное сообщение." in page
    assert 'data-confirm="Перезапустить TOW?' in page
    assert 'aria-describedby="allow-lan-hint' in page  # plus allow-lan-locked while there is no password
    assert 'aria-describedby="lan-password-hint"' in page
    assert 'input[type="checkbox"], input[type="radio"] { width: auto; }' in css
    assert ".check-field" in css
    assert ".settings-page.settings-page-compact { width: 100%; max-width: none; }" in css
    assert "min-height: 0; padding: .38rem .62rem" in css
    assert 'action="/settings/client/add"' in page
    assert "Поддерживаются: qBittorrent, Transmission, Deluge." in page
    assert "пока не поддерживаются" not in page
    assert 'method="post" action="/settings/portable/export" data-native-submit' in page
    assert 'action="/settings/portable/import"' in page
    assert 'name="backup_file"' in page
    assert 'name="operation" value="check"' in page
    assert 'name="operation" value="restore"' in page
    assert "uv run --frozen tow export" not in page
    assert "uv run --frozen tow import" not in page
    assert "Сохранённых копий нет." in page
    assert 'action="/settings/restore-points"' in page
    assert "Перед восстановлением TOW проверит копию" in page
    assert ">Сохранить файл</button>" in page
    assert ">Проверить</button>" in page
    assert ">Восстановить</button>" in page
    app_js = client.get("/static/app.js").text
    assert "details.settings-accordion" in app_js
    assert 'form.dataset.submitting !== "1"' in app_js
    assert "form.dataset.returnTarget" in app_js
    assert "navigator.clipboard?.writeText" in app_js
    assert 'querySelectorAll("[data-auto-submit]")' in app_js
    assert "new FormData(form, submitter)" in app_js
    assert 'submitter?.getAttribute("formaction") || form.getAttribute("action")' in app_js
    assert "submitter?.formAction" not in app_js
    assert '"X-TOW-Fetch": "1"' in app_js  # D6: JSON redirect, the next page renders once
    assert 'if (!nextUrl) throw new Error(t("js.submit.no_next_page"))' in app_js
    texts = re.search(r'id="tow-i18n">(.*?)</script>', client.get("/settings").text, re.DOTALL)
    assert texts
    assert json.loads(texts.group(1))["js.submit.no_next_page"] == "сервер не указал следующую страницу"
    assert "form.dataset.nativeSubmit !== undefined" in app_js
    assert app_js.index("const preparedBody =") < app_js.rindex("b.disabled = true")
    # One button/field height everywhere (the owner asked for a single look).
    assert "--btn-h: 1.625rem;" in css  # the owner wants small 26 px buttons everywhere
    assert "min-height: 2.25rem" not in css
    assert "height: var(--field-h)" in css
    assert 'grid-template-areas: "nav nav right" "sites services clock"' in css
    assert 'input[type="file"]::file-selector-button' in css
    service = page.split('id="acc-service"', 1)[1].split('id="acc-transfer"', 1)[0]
    assert "data-auto-submit-control" in service
    assert 'type="hidden" name="enabled" value="0"' in service
    assert ">Сохранить</button>" not in service
    assert ">Перезапустить</button>" in service


def test_settings_renders_saved_restore_points(monkeypatch):
    monkeypatch.setattr(
        "tow.web.services.restore_point_cleanup_status",
        lambda **_kwargs: {"pending": False, "read_error": False, "location": "synthetic-folder"},
    )
    monkeypatch.setattr(
        "tow.web.services.list_restore_points",
        lambda: [
            {
                "id": "20260924T091524Z-deadbeef",
                "created_at": "2026-09-24T09:15:24+00:00",
                "bytes": 4096,
            }
        ],
    )

    page = TestClient(app, headers={"Origin": "http://127.0.0.1"}).get("/settings").text
    transfer = page.split('id="acc-transfer"', 1)[1].split('id="acc-log"', 1)[0]

    assert "1 сохранено" in transfer
    assert "24.09.2026" in transfer
    assert "4,0 КБ" in transfer
    assert 'action="/settings/restore-points/20260924T091524Z-deadbeef/restore"' in transfer
    assert ">Восстановить</button>" in transfer


def test_settings_access_without_a_password_says_the_network_is_closed(monkeypatch):
    cfg = {
        "trackers": {},
        "client": {"kind": "qbittorrent"},
        "interval_sec": 3600,
        "bind": "0.0.0.0",
        "allow_lan": True,
        "language": "ru",  # this config replaces the test one, which pins Russian
    }
    monkeypatch.setattr("tow.web.services.load_config", lambda: cfg)
    monkeypatch.setattr("tow.web.services.load_secrets", dict)

    page = TestClient(app, headers={"Origin": "http://127.0.0.1"}).get("/settings").text
    access = page.split('id="acc-access"', 1)[1].split('id="acc-service"', 1)[0]

    assert '<span class="pill warn">Сеть закрыта: нет пароля</span>' in access
    assert "<b>Пароль не задан</b> — с других устройств TOW сейчас недоступен." in access
    assert 'id="lan-auth"' not in access  # the network can no longer be opened without a password
    assert 'action="/settings/access"' in access  # this PC may change it


def test_settings_access_cannot_be_changed_from_another_device(monkeypatch):
    from tow.auth import issue_session

    cfg = {
        "trackers": {},
        "client": {"kind": "qbittorrent"},
        "interval_sec": 3600,
        "bind": "0.0.0.0",
        "allow_lan": True,
        "language": "ru",  # this config replaces the test one, which pins Russian
    }
    monkeypatch.setattr("tow.web.services.load_config", lambda: cfg)
    monkeypatch.setenv("TOW_LAN_AUTH_TOKEN", "t" * 32)
    lan = TestClient(
        app,
        client=("192.168.1.7", 50000),
        headers={"Origin": "http://127.0.0.1"},
        cookies={"tow_session": issue_session("t" * 32)},
    )
    access = lan.get("/settings").text.split('id="acc-access"', 1)[1].split('id="acc-service"', 1)[0]
    assert 'action="/settings/access"' not in access
    assert "Доступ с других устройств включается и выключается только на компьютере с TOW" in access
    assert 'action="/settings/password"' not in access  # no password yet: the first one is set on this PC


def test_settings_multiple_clients_render_as_independent_cards(monkeypatch):
    cfg = {
        "trackers": {},
        "clients": [
            {"id": "main", "kind": "qbittorrent", "default": True},
            {"id": "backup", "kind": "qbittorrent"},
        ],
        "interval_sec": 3600,
    }
    secrets = {
        "clients": {
            "main": {"host": "main-host"},
            "backup": {"host": "backup-host"},
        }
    }
    monkeypatch.setattr("tow.web.services.load_config", lambda: cfg)
    monkeypatch.setattr("tow.web.services.load_secrets", lambda: secrets)

    page = TestClient(app, headers={"Origin": "http://127.0.0.1"}).get("/settings").text

    assert page.count('class="integration-card"') == 2
    assert page.count('class="integration-card notifier-card"') == 4
    assert page.count('action="/settings/client"') == 2
    assert "main-host" in page
    assert "backup-host" in page
    assert "<dt>ID</dt>" in page


def test_settings_translates_restart_marker_to_human_status(monkeypatch):
    monkeypatch.setattr(
        "tow.web.services.service_status",
        lambda: {
            "pid": 4321,
            "autostart": True,
            "task": {"status": "Ready", "last_run_result": "0"},
            "supervisor": {"server": {"up_since": "2026-10-02T19:59:53+00:00"}},
            "restart": {
                "status": "ready",
                "operation_id": "restart-test",
                "created_at": "2026-09-24T09:15:24+00:00",
                "reason": "settings",
            },
        },
    )

    page = TestClient(app, headers={"Origin": "http://127.0.0.1"}).get("/settings").text
    service = page.split('id="acc-service"', 1)[1].split('id="acc-transfer"', 1)[0]

    assert "Веб-страница работает с:" in service
    assert "02.10" in service
    assert "Последний перезапуск по запросу:" in service
    assert "из настроек" in service
    assert "выполнен" in service
    assert "Последний перезапуск по запросу: <b>ready</b>" not in service


@pytest.mark.parametrize(
    ("form", "accepted"),
    [
        ({"host": "http://qbit.lan", "port": "8080", "username": "admin", "password": ""}, True),
        ({"host": "http://typo.lan", "port": "8080", "username": "admin", "password": ""}, False),
        ({"host": "http://qbit.lan", "port": "9090", "username": "admin", "password": ""}, False),
        ({"host": "http://typo.lan", "port": "8080", "username": "admin", "password": "new-secret"}, True),
    ],
)
def test_client_password_is_not_kept_for_a_changed_address(monkeypatch, form, accepted):
    import base64

    from tow.store import load_secrets, save_secrets

    monkeypatch.setenv("TOW_MASTER_KEY", base64.urlsafe_b64encode(b"k" * 32).decode())
    save_secrets({"qbittorrent": {"host": "http://qbit.lan", "port": 8080, "username": "admin", "password": "stored"}})
    client = TestClient(app, headers={"Origin": "http://127.0.0.1"})

    response = client.post("/settings/client", data=form, follow_redirects=False)

    assert response.status_code == 303
    saved = load_secrets()["qbittorrent"]
    if accepted:
        assert saved["host"] == form["host"]
        assert saved["password"] == (form["password"] or "stored")
    else:
        assert "flash=" in response.headers["location"]
        assert saved == {"host": "http://qbit.lan", "port": 8080, "username": "admin", "password": "stored"}
