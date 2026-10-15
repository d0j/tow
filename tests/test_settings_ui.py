import json
import re

import pytest
from fastapi.testclient import TestClient

from tow.web import app


@pytest.mark.parametrize("language", ["ru", "en"])
@pytest.mark.parametrize("custom_minutes", [None, 30])
def test_settings_explains_global_and_personal_timers(language, custom_minutes):
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
    # The label says what the number does; the hint, that a topic's own timer stays its own.
    if language == "ru":
        assert '<span class="pill mut">2 ч</span>' in interval
        assert '<label for="interval_min">Проверять раздачи каждые</label>' in interval
        assert "Раздачи с личным таймером проверяются по нему." in interval
        assert "Общий таймер" not in interval
    else:
        assert '<span class="pill mut">2 h</span>' in interval
        assert '<label for="interval_min">Check topics every</label>' in interval
        assert "Topics with a personal timer keep it." in interval
        assert "Global timer" not in interval


def test_settings_sections_follow_the_owners_order_and_language_holds_the_theme():
    """The sections TOW needs first come first; optional site access sits near the end. Language
    and theme are one section, and the older links to either still open it."""
    client = TestClient(app, headers={"Origin": "http://127.0.0.1"})
    page = client.get("/settings").text
    order = re.findall(r'<details class="settings-section settings-accordion[^"]*" id="(acc-[a-z]+)"', page)
    assert order == [
        "acc-clients",
        "acc-bots",
        "acc-intervals",
        "acc-access",
        "acc-transfer",
        "acc-updates",
        "acc-service",
        "acc-language",
        "acc-protection",
        "acc-log",
    ]
    appearance = page.split('id="acc-language"', 1)[1].split('id="acc-protection"', 1)[0]
    assert "Язык и тема · Language</span>" in appearance
    assert 'action="/settings/language"' in appearance
    assert 'action="/settings/theme"' in appearance
    assert 'id="acc-theme"' in appearance  # #acc-theme still lands here
    assert "<legend>Тема</legend>" in appearance  # each control keeps its own visible label
    assert '<label for="language-choice">Язык</label>' in appearance
    # Without scripts both are saved by a button: the language's own, the theme's in <noscript>.
    assert appearance.count('<button type="submit">') == 2
    for name in ("language", "theme"):
        opened = client.get(f"/settings?open={name}").text
        assert " open" in re.search(r'<details[^>]+id="acc-language"[^>]*>', opened).group()


def test_mobile_settings_anchor_keeps_clear_of_the_two_row_header():
    from pathlib import Path

    css = (Path(__file__).parents[1] / "src" / "tow" / "static" / "app.css").read_text(encoding="utf-8")
    mobile_blocks = re.findall(r"@media \(max-width: 720px\) \{(.*?)\n\}", css, re.DOTALL)
    assert any(
        "header.app" in block
        and re.search(
            r"\.settings-section(?:\s*,\s*\.help-page h2\[id\])?\s*\{\s*scroll-margin-top:\s*5\.5rem;\s*\}",
            block,
        )
        for block in mobile_blocks
    )


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
    assert len(accordion_tags) == 10  # protection is independently optional; language and theme share one
    assert all(" open" not in tag for tag in accordion_tags)
    assert "Проверяются сохранённые настройки." not in page  # Check is disabled while unsaved anyway
    assert "появится кнопка «Проверить»" not in page  # it described a button that was not there
    assert 'data-confirm="Перезапустить TOW?' in page
    assert 'aria-describedby="allow-lan-hint' in page  # plus allow-lan-locked while there is no password
    assert 'aria-describedby="lan-password-hint"' in page
    assert 'input[type="checkbox"], input[type="radio"] { width: auto; }' in css
    assert ".check-field" in css
    assert ".settings-page.settings-page-compact { width: 100%; max-width: none; }" in css
    assert "min-height: 0; padding: .38rem .62rem" in css
    assert 'action="/settings/client/add"' in page
    assert "Поддерживаются:" not in page  # the list below names them
    assert "пока не поддерживаются" not in page
    assert 'method="post" action="/settings/portable/export" data-native-submit' in page
    assert 'action="/settings/portable/import"' in page
    assert 'name="backup_file"' in page
    assert 'name="operation" value="check"' in page
    assert 'name="operation" value="restore"' in page
    assert "uv run --frozen tow export" not in page
    assert "uv run --frozen tow import" not in page
    assert "Сохранённых копий нет</span>" in page
    assert 'action="/settings/restore-points"' in page
    assert "Хранятся 10 последних. Восстановление не меняет доступ по сети." in page
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
    service = page.split('id="acc-service"', 1)[1].split('id="acc-language"', 1)[0]
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
    transfer = page.split('id="acc-transfer"', 1)[1].split('id="acc-updates"', 1)[0]

    assert "Сохранено: 1" in transfer
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
    access = page.split('id="acc-access"', 1)[1].split('id="acc-transfer"', 1)[0]

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
    access = lan.get("/settings").text.split('id="acc-access"', 1)[1].split('id="acc-transfer"', 1)[0]
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

    assert page.count('class="integration-card client-card"') == 2
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
    service = page.split('id="acc-service"', 1)[1].split('id="acc-language"', 1)[0]

    assert "<dt>Веб-страница работает с</dt>" in service
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


def test_the_diagnostics_row_says_what_diagnostics_shows():
    """Qa8: the Settings row promised "the running service", and Diagnostics shows the client,
    the sites and autostart: the row names those."""
    client = TestClient(app, headers={"Origin": "http://127.0.0.1"})
    settings = client.get("/settings").text
    assert "Связь с торрент-клиентом и сайтами, автозапуск" in settings
    assert "работающим сервисом" not in settings
    doctor = client.get("/doctor").text
    assert "Автозапуск" in doctor


def test_the_guide_calls_the_master_key_by_its_name():
    """Qa8: the in-app Guide called keys/master.key "the encryption key" ("ключ шифрования"), while
    Settings, the messages and the user guide say "master key" ("мастер-ключ")."""
    client = TestClient(app, headers={"Origin": "http://127.0.0.1"})
    ru = client.get("/settings/help").text
    assert "ключ шифрования" not in ru.lower()
    assert "мастер-ключ (<code>keys/master.key</code>)" in ru
    from tow.config import load_config, save_config

    cfg = load_config()
    cfg["language"] = "en"
    save_config(cfg)
    en = client.get("/settings/help").text
    assert "encryption key" not in en.lower()
    assert "the same master key (<code>keys/master.key</code>)" in en


def test_settings_fields_are_as_wide_as_what_they_hold():
    """At 1440 px a port was 470 px wide and a password, a token or a webhook 1354 px: every field
    filled its card. Each field has its width class; related fields share a row."""
    from tow.store import save_secrets

    save_secrets({"qbittorrent": {"host": "127.0.0.1", "port": 8080, "username": "admin", "password": "pw"}})
    page = TestClient(app, headers={"Origin": "http://127.0.0.1"}).get("/settings").text

    def classes(field_id: str) -> str:
        tag = re.search(rf'<(?:input|select)[^>]* id="{field_id}"[^>]*>|<(?:input|select) id="{field_id}"[^>]*>', page)
        assert tag, field_id
        found = re.search(r'class="([^"]*)"', tag.group())
        return found.group(1) if found else ""

    widths = {
        "q_host-client-0": "w-l",
        "q_port-client-0": "w-xs",
        "q_user-client-0": "w-m",
        "q_pass-client-0": "w-m",
        "nf-telegram-token": "w-l",
        "nf-telegram-chat_ids": "w-m",
        "nf-discord-webhook_url": "w-xl",
        "nf-whatsapp-phone": "w-s",
        "nf-whatsapp-apikey": "w-s",
        "nf-ntfy-topic": "w-m",
        "nf-ntfy-server": "w-l",
        "interval_min": "w-xs",
        "flash_ttl_min": "w-xs",
        "lan-password": "w-m",
        "lan-password2": "w-m",
        "lan-hint": "w-l",
        "backup-days": "w-xs",
        "backup-path-night": "w-xl",
        "language-choice": "w-l",
        "protection-timeout": "w-xs",
    }
    for field_id, width in widths.items():
        assert width in classes(field_id).split(), (field_id, classes(field_id))
    clients = page.split('id="acc-clients"', 1)[1].split('id="acc-bots"', 1)[0]
    assert 'class="settings-form form-grid"' in clients
    assert '<div class="field-row keep">' in clients  # the address and its port, on a phone too
    access = page.split('id="acc-access"', 1)[1].split('id="acc-transfer"', 1)[0]
    new_password = access.split('<div class="field-row">', 1)[1].split(
        '<div class="field">\n                  <label for="lan-hint"', 1
    )[0]
    assert 'id="lan-password"' in new_password
    assert 'id="lan-password2"' in new_password


def _tag(page: str, field_id: str) -> str:
    return re.search(rf'<input id="{field_id}"[^>]*>', page).group()


def test_a_secret_field_says_saved_only_when_a_secret_is_saved():
    """A client with no password saved said "Empty — keep as is": there was nothing to keep. A
    saved password or token is said by a persistent hint the field is described by; a field with
    nothing saved has none, and no field carries the instruction in its placeholder."""
    from tow.store import save_secrets

    save_secrets(
        {
            "qbittorrent": {"host": "127.0.0.1", "port": 8080, "username": "admin", "password": "pw"},
            "telegram": {"token": "123456:fake-token", "chat_ids": ["1001"]},
        }
    )
    client = TestClient(app, headers={"Origin": "http://127.0.0.1"})
    client.post("/settings/client/add", data={"kind": "transmission"})
    page = client.get("/settings").text
    assert "Пусто — не менять" not in page
    assert "Сохранён</span>" not in page  # no green "Saved" pill beside a label
    kept = "Сохранён · оставьте пустым, чтобы не менять"
    saved = _tag(page, "q_pass-client-0")
    assert 'aria-describedby="q_pass-client-0-saved"' in saved
    assert f'<p class="field-hint" id="q_pass-client-0-saved">{kept}</p>' in page
    new = _tag(page, "q_pass-client-1")  # Transmission, just added: nothing saved
    assert "aria-describedby" not in new
    assert "placeholder" not in new
    assert 'id="q_pass-client-1-saved"' not in page
    assert 'aria-describedby="nf-telegram-token-saved"' in _tag(page, "nf-telegram-token")
    assert "aria-describedby" not in _tag(page, "nf-discord-webhook_url")  # not connected
    assert 'placeholder="https://discord.com/api/webhooks/…"' in _tag(page, "nf-discord-webhook_url")


def test_secret_fields_have_an_eye_and_the_right_autofill():
    """A client's password is its own sign-in, a messenger's token, webhook or key is no password
    at all: neither offers to generate a new password (new-password did). TOW's own new password
    keeps new-password."""
    from tow.store import save_secrets

    save_secrets({"qbittorrent": {"host": "127.0.0.1", "port": 8080, "username": "admin", "password": "pw"}})
    page = TestClient(app, headers={"Origin": "http://127.0.0.1"}).get("/settings").text
    assert 'autocomplete="section-client-0 username"' in _tag(page, "q_user-client-0")
    assert 'autocomplete="section-client-0 current-password"' in _tag(page, "q_pass-client-0")
    for field_id in ("nf-telegram-token", "nf-discord-webhook_url", "nf-whatsapp-apikey"):
        tag = _tag(page, field_id)
        assert 'autocomplete="off"' in tag
        assert 'spellcheck="false"' in tag
        assert 'autocapitalize="off"' in tag
    assert 'autocomplete="new-password"' in _tag(page, "lan-password")
    assert "new-password" not in page.split('id="acc-access"', 1)[0]  # only TOW's own password
    for field_id in ("q_pass-client-0", "nf-telegram-token", "nf-discord-webhook_url", "lan-password", "lan-password2"):
        assert re.search(
            rf'<span class="secret"><input id="{field_id}"[^>]*><button type="button" class="ico reveal" '
            rf'data-reveal="{field_id}"[^>]* aria-controls="{field_id}" aria-pressed="false"',
            page,
        ), field_id


def test_settings_say_each_thing_once_and_the_guide_keeps_the_long_texts():
    """The Settings page carried 1239 words (S2, English): instructions repeated beside their
    buttons, architecture, absent events. The shortened texts keep what is needed; the long
    site-access explanation stays in the guide, word for word."""
    from markupsafe import escape

    from tow.config import load_config, save_config
    from tow.i18n import translate

    cfg = load_config()
    cfg["language"] = "en"
    save_config(cfg)
    client = TestClient(app, headers={"Origin": "http://127.0.0.1"})
    page = client.get("/settings").text
    assert "press “Save”, then “Check”" not in page  # the buttons are right below the steps
    assert "TOW runs as one service" not in page
    assert "has not been restarted from here yet" not in page
    assert "Installed: TOW" not in page.split('id="acc-updates"', 1)[1].split('id="acc-service"', 1)[0]
    assert "TOW tries ordinary mirrors first" not in page  # how it works: the guide
    # Kept, briefly: what a remote reader can see, and that the network needs the password.
    assert "Anyone who opens the sign-in page can read it — never include the password." in page
    assert "Turning it on takes effect after TOW restarts; turning it off works at once." in page
    guide = client.get("/settings/help").text
    for key in ("guide_hint", "sequence", "guide_step_install", "guide_step_address", "guide_step_enable"):
        assert str(escape(translate(f"protection_ui.{key}", "en"))) in guide, key
    assert "TOW does not install services for you." in guide
