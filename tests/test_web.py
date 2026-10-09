import copy
import json
import os
import re
from urllib.parse import parse_qs, urlparse

import pytest
from fastapi.testclient import TestClient
from helpers import flash_of, open_network, shown, wait_for_check_job

from tow.check import reconcile as check_reconcile
from tow.check import rows as check_rows
from tow.check import run as check_run
from tow.clock import iso_now
from tow.log import log_event, read_events
from tow.store import (
    load_download_history,
    load_state,
    save_download_history,
    save_state,
)
from tow.web import app

NNM_HTTP_SPEC = {
    "title": "NNM-Club",
    "url_regex": r"^https?://(?:www\.)?nnmclub\.to/forum/viewtopic\.php\?t=(\d+)",
    "login_hosts": ["https://nnmclub.to"],
    "fetch_hosts": ["https://nnmclub.to"],
    "login_path": "/forum/login.php",
    "topic_path": "/forum/viewtopic.php?t={id}",
    "download_href_regex": r"download\\.php\\?id=(\\d+)",
    "page_download": True,
    "cookie_names": [],
}


def test_lan_disabled_rejects_remote_peer_even_before_socket_restart(monkeypatch):
    monkeypatch.setattr("tow.web.services.load_config", lambda: {"allow_lan": False, "bind": "0.0.0.0"})
    client = TestClient(app, client=("192.168.1.9", 12345))

    assert client.get("/").status_code == 403
    assert client.get("/healthz").status_code == 403


def test_dns_rebound_host_is_rejected_before_origin_check(monkeypatch):
    monkeypatch.setattr("tow.web.services.load_config", lambda: {"allow_lan": True, "bind": "0.0.0.0"})
    client = TestClient(app, base_url="http://evil.example:8787", headers={"Origin": "http://evil.example:8787"})

    assert client.get("/").status_code == 403
    assert client.post("/settings/client", data={"host": "evil.example"}).status_code == 403
    lan_peer = TestClient(app, client=("192.168.1.9", 12345), base_url="http://rebound.example:8787")
    assert lan_peer.get("/").status_code == 403


def test_lan_auth_cannot_be_bypassed_by_stale_loopback_bind(monkeypatch):
    monkeypatch.setattr("tow.web.services.load_config", lambda: {"allow_lan": True, "bind": "127.0.0.1"})
    monkeypatch.setenv("TOW_LAN_AUTH_TOKEN", "t" * 32)
    client = TestClient(app, client=("192.168.1.9", 12345), base_url="http://192.168.1.2:8787")

    assert client.get("/").status_code == 401


def test_settings_secret_undo_preserves_unrelated_newer_values():
    from tow.undo.snapshots import restore_scoped as _restore_scoped_secrets

    current = {"clients": {"main": {"password": "new"}}, "telegram": {"token": "new-token"}}
    snapshot = {"clients": {"main": {"password": "old"}}, "telegram": {"token": "old-token"}}

    restored = _restore_scoped_secrets(current, snapshot, ["clients", "main"])

    assert restored["clients"]["main"]["password"] == "old"
    assert restored["telegram"]["token"] == "new-token"
    assert current["clients"]["main"]["password"] == "new"


def test_pages_ok():
    c = TestClient(app, headers={"Origin": "http://127.0.0.1"})
    assert c.get("/").status_code == 200
    home = c.get("/").text
    assert 'id="log-open"' in home
    assert 'id="log-pop"' in home
    js = c.get("/log.json")
    assert js.status_code == 200
    assert js.json()["ok"] is True
    assert "rows" in js.json()
    assert c.get("/sites").status_code == 200
    assert c.get("/settings/help").status_code == 200
    assert c.get("/doctor").status_code == 200

    home = c.get("/").text
    assert 'id="page-title"' in home
    assert 'aria-labelledby="log-title"' in home
    assert 'aria-labelledby="download-title"' in home
    assert 'id="search-status"' in home
    assert "onsubmit=" not in home
    sites = c.get("/sites").text
    assert 'id="page-title"' in sites
    assert 'name="topic_path"' in sites
    assert 'name="download_href_regex"' in sites
    assert 'name="page_download"' in sites
    assert "onsubmit=" not in sites
    settings = c.get("/settings").text
    st = settings
    assert 'id="page-title"' in settings
    assert 'action="/check"' not in settings
    help_text = c.get("/settings/help").text
    assert 'action="/check"' not in help_text
    assert "клиенты" in st
    assert "боты" in st
    assert "интервалы" in st
    assert 'name="flash_ttl_min"' in st
    assert "лог" in st
    assert "acc-log" in st
    assert "acc-transfer" in st
    assert "экспорт / импорт" in st
    assert 'action="/settings/portable/export"' in st
    assert 'action="/settings/portable/import"' in st
    assert "/settings/help" in st
    assert "Как экспортировать" not in st
    assert "Как импортировать" not in st
    assert "transfer-item" not in st
    assert 'href="/doctor"' in st  # Settings → Сервис links the diagnostics
    assert ">Состояние<" not in st
    assert "Другие клиенты" not in st
    assert "активный — qBit" not in st
    assert '<option value="transmission">Transmission</option>' in st  # can be added
    assert '<option value="deluge">Deluge</option>' in st
    assert "µTorrent" not in st
    assert "utorrent" not in st.lower()
    assert 'name="kind"' in st
    assert 'value="qbittorrent"' in st
    help_p = c.get("/settings/help")
    assert help_p.status_code == 200
    text = help_p.text
    for essential in (
        f"data{os.sep}secrets.enc",
        "master.key",
        "Файл TOW",
        "Восстановить",
        "Уведомления",
        "← Настройки",
    ):
        assert essential in text, essential
    assert re.search(r"раз в <b>\d+ (ч|мин)</b>", text)  # the real interval, not a hard-coded one
    assert "хранится не больше 5, по 5,0 МБ каждый" in text  # the real log limits
    # plain words for the owner; commands and internals live in the README
    for jargon in ("regex", "hash", "media bytes", "Fernet", "TOW_MASTER_KEY", "морде", "plaintext", "tow secrets"):
        assert jargon not in text, jargon
    health = c.get("/health.json")
    assert health.status_code == 200
    assert health.json()["ok"] is True
    assert c.get("/favicon.ico").status_code == 204
    css = c.get("/static/app.css")
    assert css.status_code == 200
    assert b"--bg" in css.content
    app_js = c.get("/static/app.js")
    assert app_js.status_code == 200
    assert "/health.json" in app_js.text
    assert "location.reload" not in app_js.text
    assert "app.js?v=" in home
    assert "торрент" in c.get("/").text
    assert 'action="/check"' in c.get("/").text
    home = c.get("/").text
    assert "next-check" in home
    settings = c.get("/settings").text
    assert 'id="allow-lan"' in settings
    assert 'name="allow_lan"' in settings
    assert 'id="lan-password"' in settings
    assert 'name="lan_password"' in settings
    assert "На этом компьютере TOW открывается без пароля. С других устройств" in settings
    hz = c.get("/healthz")
    assert hz.status_code == 200
    assert hz.json()["ok"] is True
    assert c.get("/").headers.get("x-frame-options") == "DENY"
    bad = c.post("/undo", headers={"Origin": "https://evil.example"}, follow_redirects=False)
    assert bad.status_code == 403


def test_store_error_response_does_not_expose_local_exception_details():
    from tow.store import StoreCorruptionError
    from tow.web.app import create_app

    isolated = create_app()

    @isolated.get("/test-store-error")
    def fail():
        raise StoreCorruptionError("secret=must-not-be-displayed")

    response = TestClient(isolated).get("/test-store-error")
    assert response.status_code == 503
    assert "must-not-be-displayed" not in response.text
    assert "данные TOW недоступны" in response.text


def test_post_forms_use_fetch_transport_for_origin_compatibility():
    c = TestClient(app, headers={"Origin": "http://127.0.0.1"})
    home = c.get("/").text
    app_js = c.get("/static/app.js").text

    assert re.search(r'app\.js\?v=[^"&]+-[0-9a-f]{12}', home)
    assert "new FormData(form)" in app_js
    assert 'credentials: "same-origin"' in app_js
    assert 'b.textContent = t("js.browser_auth.starting")' in app_js
    texts = re.search(r'<script type="application/json" id="tow-i18n">(.*?)</script>', home, re.DOTALL)
    assert texts
    assert json.loads(texts.group(1))["js.browser_auth.starting"] == "Запуск…"  # app.js texts come from the page
    assert "response.url" in app_js


def test_unauthenticated_browser_navigation_redirects_to_login_but_api_stays_401(monkeypatch):
    monkeypatch.setattr("tow.web.services.load_config", lambda: {"bind": "0.0.0.0", "allow_lan": True})
    monkeypatch.setenv("TOW_LAN_AUTH_TOKEN", "t" * 32)
    c = TestClient(app, client=("192.168.1.7", 50000))  # a phone or laptop on the home network

    browser = c.get("/", headers={"Accept": "text/html"}, follow_redirects=False)
    api = c.get("/health.json", headers={"Accept": "application/json"}, follow_redirects=False)

    assert browser.status_code == 303
    assert browser.headers["location"] == "/login"
    assert browser.headers["cache-control"] == "no-store"
    assert "www-authenticate" not in browser.headers
    assert api.status_code == 401
    assert api.headers["cache-control"] == "no-store"
    assert "www-authenticate" not in api.headers


def test_lan_always_needs_the_password_but_this_pc_never_does(monkeypatch):
    # lan_auth: false used to open the whole network; it no longer switches the password off.
    monkeypatch.setattr(
        "tow.web.services.load_config",
        lambda: {"bind": "0.0.0.0", "allow_lan": True, "lan_auth": False, "setup_done": True},
    )
    monkeypatch.setenv("TOW_LAN_AUTH_TOKEN", "t" * 32)

    lan = TestClient(app, client=("192.168.1.7", 50000)).get(
        "/", headers={"Accept": "text/html"}, follow_redirects=False
    )
    local = TestClient(app).get("/", headers={"Accept": "text/html"}, follow_redirects=False)

    assert lan.status_code == 303
    assert lan.headers["location"] == "/login"
    assert local.status_code == 200


def test_optional_lan_password_login_issues_session(monkeypatch):
    from tow.auth import lan_password_record
    from tow.store import save_secrets

    save_secrets({"lan_auth": lan_password_record("correct horse battery staple")})
    open_network()
    c = TestClient(app, headers={"Origin": "http://127.0.0.1"})

    bad = c.post("/login", data={"password": "wrong password"}, follow_redirects=False)
    good = c.post("/login", data={"password": "correct horse battery staple"}, follow_redirects=False)
    home = c.get("/", headers={"Accept": "text/html"})

    assert bad.status_code == 401
    assert good.status_code == 303
    assert good.headers["location"] == "/"
    assert home.status_code == 200
    assert 'id="page-title"' in home.text


def test_get_recovers_site_transaction_before_handler(monkeypatch):
    calls = []
    monkeypatch.setattr("tow.web.services.recover_store_transaction", lambda: calls.append(True) or False)
    c = TestClient(app)

    response = c.get("/healthz")

    assert response.status_code == 200
    assert calls == [True]


def test_http_recovery_precedes_pending_undo_cleanup(monkeypatch):
    calls = []
    monkeypatch.setattr("tow.web.services.recover_store_transaction", lambda: calls.append("recovery") or False)
    monkeypatch.setattr("tow.web.services.cleanup_secret_undo", lambda: calls.append("cleanup") or False)
    monkeypatch.setattr("tow.web.site_store.secret_undo_cleanup_pending", lambda: True)
    c = TestClient(app)

    response = c.get("/health.json")

    assert response.status_code == 200
    assert calls[:2] == ["recovery", "cleanup"]


def test_settings_page_has_service_lifecycle_controls(monkeypatch):
    monkeypatch.setattr(
        "tow.web.services.service_status",
        lambda: {"pid": 4321, "autostart": True, "task": {"status": "Ready"}, "restart": None},
    )
    settings = TestClient(app, headers={"Origin": "http://127.0.0.1"}).get("/settings").text

    assert 'id="acc-service"' in settings
    assert 'action="/settings/service/autostart"' in settings
    assert 'action="/settings/service/restart"' in settings
    assert 'name="enabled"' in settings


def test_settings_service_routes_require_adapter_success(monkeypatch):
    from tow.web.routes_service import settings_service_autostart, settings_service_restart

    monkeypatch.setattr("tow.web.services.set_autostart", lambda _enabled, **_kw: {"ok": True})
    monkeypatch.setattr("tow.web.services.service_status", lambda: {"autostart": True})
    monkeypatch.setattr("tow.web.services.request_restart", lambda: {"ok": True, "operation_id": "restart-test"})
    monkeypatch.setattr("tow.web.services.log_event", lambda *_args, **_kwargs: None)

    autostart = settings_service_autostart("1", "0")
    restart = settings_service_restart()

    assert autostart.status_code == 303
    assert "автозапуск включён" in shown(autostart.headers["location"])
    assert restart.status_code == 303
    assert "restart-test" in restart.headers["location"]


def test_settings_autostart_requires_semantic_readback(monkeypatch):
    from tow.web.routes_service import settings_service_autostart

    events = []
    monkeypatch.setattr("tow.web.services.set_autostart", lambda _enabled, **_kw: {"ok": True})
    monkeypatch.setattr("tow.web.services.service_status", lambda: {"autostart": False})
    monkeypatch.setattr("tow.web.services.log_event", lambda kind, **_kwargs: events.append(kind))

    response = settings_service_autostart("1", "0")

    assert response.status_code == 303
    assert "не подтверждён" in shown(response.headers["location"])
    assert events == ["settings_service_autostart_fail"]


def test_settings_restore_point_routes_report_success(monkeypatch):
    from tow.web.routes_backup import settings_restore_point_apply, settings_restore_point_create

    point_id = "20260924T091524Z-deadbeef"
    monkeypatch.setattr("tow.web.services.create_restore_point", lambda: {"id": point_id})
    monkeypatch.setattr(
        "tow.web.services.restore_from_point",
        lambda value: {"restored": value, "safety_point": "20260924T091600Z-cafebabe"},
    )
    monkeypatch.setattr("tow.web.services.log_event", lambda *_args, **_kwargs: None)

    created = settings_restore_point_create()
    restored = settings_restore_point_apply(point_id)

    assert created.status_code == 303
    assert "точка восстановления создана" in shown(created.headers["location"])
    assert restored.status_code == 303
    assert "доступ по сети сохранён" in shown(restored.headers["location"])


def test_settings_service_status_is_read_only(monkeypatch):
    from tow.web.routes_service import settings_service_status

    payload = {"pid": 4321, "autostart": False, "task": {"ok": False}, "restart": None}
    monkeypatch.setattr("tow.web.services.service_status", lambda: payload)

    response = settings_service_status()

    assert response.status_code == 200
    assert json.loads(response.body) == payload


def test_mutating_requests_require_strict_same_origin():
    c = TestClient(app)
    for headers in ({}, {"Origin": "null"}, {"Origin": "https://testserver"}, {"Origin": "http://127.0.0.1:8080"}):
        response = c.post("/undo", headers=headers, follow_redirects=False)
        assert response.status_code == 403


def test_home_uses_canonical_episode_progress_and_separates_event_time():
    save_state(
        {
            "topics": [
                {
                    "id": "mr-16",
                    "title": "Сериал Г / old [04x01-07 из 24]",
                    "tracker_title": ("Сериал Г / Show G [04x01-22 из 24] (2026)"),
                    "url": "https://rutor.info/torrent/1234567/x",
                    "save_path": r"M:\\anime",
                    "last_ok": True,
                    "last_error": None,
                }
            ]
        }
    )
    items = {
        f"episode:s04e{episode:02d}": {
            "identity": f"episode:s04e{episode:02d}",
            "kind": "episode",
            "label": f"S04E{episode:02d}",
            "status": "completed",
        }
        for episode in range(1, 23)
    }
    items["episode:s04e01#revision:old"] = {
        "identity": "episode:s04e01#revision:old",
        "kind": "episode",
        "label": "S04E01",
        "status": "completed",
    }
    save_download_history(
        {
            "schema_version": 1,
            "topics": {
                "mr-16": {
                    "expected": {"kind": "episodes", "total": 24},
                    "summary": {"completed": 43, "expected": 24, "is_complete": True},
                    "last_event": {"kind": "episode_completed", "label": "S04E22", "at": "2026-09-13T19:05:25+03:00"},
                    "items": items,
                }
            },
        }
    )

    home = TestClient(app, headers={"Origin": "http://127.0.0.1"}).get("/").text

    assert "Сериал Г" in home
    assert "22/24" in home
    assert "43/24" not in home
    assert 'S04E22</b> <small class="mut event-at">13.09.2026 19:05:25 IDT UTC+03:00</small>' in home
    assert "[04x01-22 из 24]" in home
    details = TestClient(app, headers={"Origin": "http://127.0.0.1"}).get("/topics/mr-16/downloads.json")
    assert details.status_code == 200
    assert "[04x01-22 из 24]" in details.json()["topic"]["title"]


def test_settings_renders_when_secret_store_requires_migration(monkeypatch):
    from tow.store import SecretStoreError

    def blocked():
        raise SecretStoreError("legacy plaintext TOW secrets require explicit migrate")

    monkeypatch.setattr("tow.web.services.load_secrets", blocked)
    response = TestClient(app, headers={"Origin": "http://127.0.0.1"}).get("/settings")

    assert response.status_code == 200
    assert "пароли и токены не открываются" in response.text.lower()
    assert 'title="Пароли и токены не открываются"' in response.text
    assert "экспорт / импорт" in response.text
    health = TestClient(app, headers={"Origin": "http://127.0.0.1"}).get("/health.json")
    assert health.json()["check_ok"] is False
    assert health.json()["check_error"] == "secrets_migration_required"


@pytest.mark.parametrize("value", ["", "false"])
def test_settings_access_closes_the_network(value):
    from tow.config import load_config, save_config
    from tow.paths import config_path

    cfg = load_config()
    cfg.update(bind="0.0.0.0", allow_lan=True)
    save_config(cfg)
    config_path().write_text(config_path().read_text(encoding="utf-8") + "lan_auth: true\n", encoding="utf-8")

    response = TestClient(app, headers={"Origin": "http://127.0.0.1"}).post(
        "/settings/access", data={"allow_lan": value}, follow_redirects=False
    )

    assert response.status_code == 303
    cfg = load_config()
    assert (cfg["bind"], cfg["allow_lan"]) == ("127.0.0.1", False)
    assert "lan_auth" not in config_path().read_text(encoding="utf-8")  # the old flag is dropped on save


def test_settings_access_can_opt_in_to_encrypted_password(monkeypatch):
    # The password is set on the password card (/settings/password, with its reminder);
    # /settings/access only opens the network, and only once a password exists.
    from cryptography.fernet import Fernet

    from tow.auth import lan_password_matches, password_hint
    from tow.config import load_config
    from tow.i18n import t
    from tow.store import load_secrets

    monkeypatch.setenv("TOW_MASTER_KEY", Fernet.generate_key().decode("ascii"))
    client = TestClient(app, headers={"Origin": "http://127.0.0.1"})

    refused = client.post("/settings/access", data={"allow_lan": "1"}, follow_redirects=False)
    assert t("web.settings.set_password_first", "ru") in shown(refused.headers["location"])
    assert load_config()["allow_lan"] is False

    client.post(
        "/settings/password",
        data={
            "lan_password": "correct horse battery staple",
            "lan_password2": "correct horse battery staple",
            "hint": "the comic",
        },
    )
    client.post("/settings/access", data={"allow_lan": "1"})

    cfg = load_config()
    assert (cfg["allow_lan"], cfg["bind"]) == (True, "0.0.0.0")
    record = load_secrets()["lan_auth"]
    assert "correct horse battery staple" not in json.dumps(record)
    assert lan_password_matches("correct horse battery staple", record)
    assert password_hint(record) == "the comic"  # the reminder is kept, not silently dropped


def test_a_failed_access_save_changes_nothing(monkeypatch):
    from tow.config import load_config, save_config

    cfg = load_config()
    cfg.update(bind="0.0.0.0", allow_lan=True)
    save_config(cfg)

    def disk_full(_state):
        raise OSError("disk full")

    monkeypatch.setattr("tow.store.save_state", disk_full)

    response = TestClient(app, headers={"Origin": "http://127.0.0.1"}).post(
        "/settings/access", data={"allow_lan": ""}, follow_redirects=False
    )

    assert "не сохранено" in shown(response.headers["location"])
    cfg = load_config()
    assert (cfg["bind"], cfg["allow_lan"]) == ("0.0.0.0", True)


def undo_last():
    """POST /undo from this computer, as the "Undo" button sends it."""
    return TestClient(app, headers={"Origin": "http://127.0.0.1"}).post("/undo", follow_redirects=False)


def _settings_record(interval_sec: int = 3600) -> dict:
    return {"kind": "settings", "secrets_undo_ref": "settings-v1", "interval_sec": interval_sec, "ts": iso_now()}


def test_settings_undo_rejects_site_shaped_secret_snapshot():
    from tow.store import load_secrets, save_secret_undo, save_secrets

    save_secrets({"telegram": {"token": "current"}})
    save_secret_undo({"name": "site", "present": True, "value": {}})
    save_state({"topics": [], "mirrors": {}, "undo": _settings_record()})

    response = undo_last()

    assert response.status_code == 303
    assert "сохранённые пароли" in shown(response.headers["location"])
    assert load_secrets() == {"telegram": {"token": "current"}}
    assert load_state()["undo"]["kind"] == "settings"  # nothing written: the undo stays


def test_site_undo_reports_committed_when_secret_cleanup_fails(monkeypatch):
    from tow.config import load_config
    from tow.store import SecretStoreError, save_secret_undo, secret_undo_path

    save_secret_undo({"name": "site-a", "present": False, "value": None})
    record = {"kind": "site", "name": "site-a", "spec": {"title": "site-a"}, "mirror_present": False}
    save_state({"topics": [], "mirrors": {}, "undo": {**record, "secret_undo_ref": "settings-v1", "ts": iso_now()}})

    def locked(_reference):
        raise SecretStoreError("cannot remove TOW secret undo snapshot")

    monkeypatch.setattr("tow.store.delete_secret_undo", locked)

    response = undo_last()

    text = shown(response.headers["location"])
    assert "изменение сайта отменено" in text
    assert "будут удалены чуть позже" in text
    assert load_config()["trackers"]["site-a"] == {"title": "site-a"}
    state = load_state()
    assert "undo" not in state
    assert state["secret_undo_cleanup_pending"]["reference"] == "settings-v1"
    assert secret_undo_path().exists()  # for the retry


def test_pending_secret_undo_cleanup_retries_when_reference_is_unowned():
    from tow.store import save_secret_undo, secret_undo_path
    from tow.undo import cleanup as _retry_pending_secret_undo_cleanup

    save_secret_undo({"telegram": {"token": "old"}})
    pending = {"reference": "settings-v1", "attempts": 1, "last_error": "OSError", "ts": iso_now()}
    save_state({"topics": [], "mirrors": {}, "secret_undo_cleanup_pending": pending})

    assert _retry_pending_secret_undo_cleanup() is True
    assert not secret_undo_path().exists()
    assert "secret_undo_cleanup_pending" not in load_state()


def test_settings_undo_records_cleanup_failure_for_retry(monkeypatch):
    from tow.store import SecretStoreError, load_secrets, save_secret_undo, save_secrets

    save_secrets({"telegram": {"token": "current"}})
    save_secret_undo({"telegram": {"token": "opaque"}})
    save_state({"topics": [], "mirrors": {}, "undo": _settings_record(43200)})

    def locked(_reference):
        raise SecretStoreError("cleanup")

    monkeypatch.setattr("tow.store.delete_secret_undo", locked)

    response = undo_last()

    assert response.status_code == 303
    assert load_secrets() == {"telegram": {"token": "opaque"}}
    assert load_state()["secret_undo_cleanup_pending"]["reference"] == "settings-v1"


def _interval(sec: int) -> None:
    from tow.config import set_interval_sec

    set_interval_sec(sec)


def test_settings_undo_puts_the_interval_back_in_the_config_alone():
    # `tow run` reads the interval from config.yaml: an undo touches no scheduler (1.21).
    from tow.config import load_config
    from tow.store import save_secret_undo

    save_secret_undo({"telegram": {"token": "opaque"}})
    save_state({"topics": [], "mirrors": {}, "undo": _settings_record(3600)})
    _interval(7200)

    response = undo_last()

    assert response.status_code == 303
    assert "планировщик" not in shown(response.headers["location"])
    assert load_config()["interval_sec"] == 3600


def _interval_setup():
    """The real stores (interval 3600 s, messages 60 s)."""
    from tow.config import set_flash_ttl_sec, set_interval_sec
    from tow.store import save_secrets

    set_interval_sec(3600)
    set_flash_ttl_sec(60)
    save_secrets({"telegram": {"token": "current"}})
    save_state({"topics": [{"id": "topic"}], "mirrors": {}})
    return _store_files()


def _store_files() -> dict:
    from tow.paths import config_path
    from tow.store import encrypted_secrets_path, secret_undo_path, state_path

    paths = (config_path(), state_path(), encrypted_secrets_path(), secret_undo_path())
    return {path.name: path.read_bytes() if path.is_file() else None for path in paths}


def test_settings_interval_compensates_when_config_write_fails(monkeypatch):
    from tow.web.routes_settings import settings_interval

    before = _interval_setup()

    def fail_interval(_sec):
        raise OSError("injected config write failure")

    monkeypatch.setattr("tow.config.set_interval_sec", fail_interval)

    response = settings_interval("120", "1")

    assert response.status_code == 303
    assert "не сохранено" in shown(response.headers["location"])
    assert _store_files() == before  # the undo, its snapshot and the message time are back too


def test_settings_interval_saves_config_state_and_undo_together(monkeypatch):
    from tow.config import load_config
    from tow.web.routes_settings import settings_interval

    _interval_setup()

    response = settings_interval("120", "2")

    assert shown(response.headers["location"]).endswith(" сохранено")  # no "development copy" warning
    assert (load_config()["interval_sec"], load_config()["flash_ttl_sec"]) == (7200, 120)
    record = load_state()["undo"]
    assert (record["kind"], record["interval_sec"], record["flash_ttl_sec"]) == ("settings", 3600, 60)


def test_add_edit_undo_delete(monkeypatch):
    monkeypatch.setattr("tow.web.services.run_check", lambda **kw: {"qbit": "ok", "results": []})
    c = TestClient(app, headers={"Origin": "http://127.0.0.1"})
    r = c.post(
        "/topics/add",
        data={
            "url": "http://rutor.info/torrent/1/x",
            "title": "Test Show",
            "save_path": r"M:\anime",
        },
        follow_redirects=False,
    )
    assert r.status_code == 303
    topics = load_state()["topics"]
    assert len(topics) == 1
    assert topics[0]["save_path"] == r"M:\anime"
    assert load_state()["save_roots"] == [r"M:\anime"]
    tid = topics[0]["id"]
    home = c.get("/").text + c.get(f"/topics/{tid}/edit-panel").text  # M2: the panel is fetched on demand
    assert 'list="save-roots"' in home
    assert 'id="save-roots"' in home
    assert r"M:\anime" in home
    assert f"/topics/{tid}/edit" in home
    assert f"/topics/{tid}/delete" in home
    assert 'data-confirm="Убрать раздачу из TOW? Торрент в торрент-клиенте и история останутся."' in home
    assert "onsubmit=" not in home
    assert "edit-actions" in home
    assert "Сохранить" in home
    assert "Убрать из TOW" in home
    r = c.post(f"/topics/{tid}/pause", follow_redirects=False)
    assert r.status_code == 303
    assert load_state()["topics"][0].get("paused") is True
    c.post(f"/topics/{tid}/pause", follow_redirects=False)
    assert not load_state()["topics"][0].get("paused")
    r = c.post(
        f"/topics/{tid}/edit",
        data={"title": "Renamed", "url": "http://rutor.info/torrent/1/x", "save_path": r"P:\x"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert load_state()["topics"][0]["title"] == "Renamed"
    assert load_state()["topics"][0]["save_path"] == r"P:\x"
    r = c.post("/undo", follow_redirects=False)
    assert r.status_code == 303
    assert load_state()["topics"][0]["title"] == "Test Show"
    r = c.post(f"/topics/{tid}/delete", follow_redirects=False)
    assert r.status_code == 303
    assert load_state()["topics"] == []
    c.post("/undo", follow_redirects=False)
    assert len(load_state()["topics"]) == 1


def test_add_and_edit_round_trip_selection_and_once_mode(monkeypatch):
    monkeypatch.setattr("tow.web.services.guess_topic_title", lambda _url: "")  # no real tracker request
    monkeypatch.setattr("tow.web.services.run_check", lambda **kw: {"qbit": "ok", "results": []})
    client = TestClient(app, headers={"Origin": "http://127.0.0.1"})

    response = client.post(
        "/topics/add",
        data={
            "url": "http://rutor.info/torrent/101/show",
            "title": "Show",
            "save_path": r"M:\anime",
            "client_id": "default",
            "selection_mode": "episodes",
            "selection_value": "S01E03-E05",
            "tracking_mode": "once",
        },
        follow_redirects=False,
    )

    assert response.status_code == 303
    topic = load_state()["topics"][0]
    assert topic["selection"] == {"mode": "episodes", "value": "S01E03-E05"}
    assert topic["tracking_mode"] == "once"
    html = client.get("/").text
    assert 'name="selection_mode"' in html
    assert 'name="tracking_mode"' in html
    assert "совпадение неоднозначно, ничего не запускается" in html.lower()

    topic["hash"] = "A" * 40
    topic["once_done"] = True
    state = load_state()
    state["topics"][0] = topic
    save_state(state)
    response = client.post(
        f"/topics/{topic['id']}/edit",
        data={
            "title": "Show",
            "url": topic["url"],
            "save_path": topic["save_path"],
            "client_id": "default",
            "selection_mode": "files",
            "selection_value": "*.mkv, Subs/*.srt",
            "tracking_mode": "watch",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    edited = load_state()["topics"][0]
    assert edited["selection"] == {"mode": "files", "value": "*.mkv, Subs/*.srt"}
    assert edited["selection_dirty"] is True
    assert edited["once_done"] is False


def test_active_topic_rejects_url_change(monkeypatch):
    monkeypatch.setattr("tow.web.services.run_check", lambda **kw: {"qbit": "ok", "results": []})
    state = {
        "topics": [
            {
                "id": "active",
                "title": "Show",
                "url": "http://rutor.info/torrent/101/show",
                "save_path": r"M:\anime",
                "client_id": "default",
                "hash": "A" * 40,
            }
        ]
    }
    save_state(state)
    client = TestClient(app, headers={"Origin": "http://127.0.0.1"})

    response = client.post(
        "/topics/active/edit",
        data={
            "title": "Show",
            "url": "http://rutor.info/torrent/102/other",
            "save_path": r"M:\anime",
            "client_id": "default",
            "selection_mode": "all",
            "tracking_mode": "watch",
        },
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert "новой" in shown(response.headers["location"])
    assert load_state()["topics"][0]["url"].endswith("/101/show")


def test_edit_of_a_deleted_topic_saves_nothing_and_says_so():
    from helpers import flash_kind

    from tow.paths import state_path
    from tow.web.text import t

    save_state({"topics": [{"id": "kept", "title": "Show", "url": "http://rutor.info/torrent/101/show"}]})
    before = state_path().read_bytes()
    client = TestClient(app, headers={"Origin": "http://127.0.0.1"})
    response = client.post(
        "/topics/gone/edit", data={"title": "Show", "save_path": r"M:\anime"}, follow_redirects=False
    )
    assert response.status_code == 303
    assert flash_of(response.headers["location"]) == t("web.topics.not_found")
    assert flash_kind(response.headers["location"]) == "err"
    assert state_path().read_bytes() == before


def test_undo_selection_edit_marks_old_policy_for_client_reapply():
    old_topic = {
        "id": "topic-undo-selection",
        "title": "Show",
        "url": "http://rutor.info/torrent/101/show",
        "save_path": r"M:\anime",
        "client_id": "default",
        "hash": "A" * 40,
        "selection": {"mode": "all", "value": ""},
        "tracking_mode": "once",
        "once_done": True,
        "selection_hash": "A" * 40,
        "selected_file_count": 10,
        "torrent_file_count": 10,
    }
    current = {
        **old_topic,
        "selection": {"mode": "episodes", "value": "S01E01-E03"},
        "once_done": False,
        "selection_hash": "A" * 40,
        "selection_verified": True,
        "selected_file_count": 3,
        "selected_episode_keys": [
            "episode:s01e01",
            "episode:s01e02",
            "episode:s01e03",
        ],
    }
    save_state(
        {
            "topics": [current],
            "undo": {"kind": "topic_put", "item": old_topic, "ts": iso_now()},
        }
    )

    response = TestClient(app, headers={"Origin": "http://127.0.0.1"}).post("/undo", follow_redirects=False)

    assert response.status_code == 303
    restored = load_state()["topics"][0]
    assert restored["selection"] == {"mode": "all", "value": ""}
    assert restored["selection_dirty"] is True
    assert restored["selected_file_count"] == 3
    assert restored["once_done"] is False


def test_edit_path_moves_in_client(monkeypatch):
    monkeypatch.setattr("tow.web.services.run_check", lambda **kw: {"qbit": "ok", "results": []})
    seen: list[tuple[str, str]] = []

    class Fake:
        def set_location(self, infohash: str, save_path: str) -> str:
            seen.append((infohash, save_path))
            return "ok"

        def has_hash(self, infohash: str) -> bool:
            return True

        def inspect_torrent(self, infohash: str) -> dict:
            return {"hash": "AA" * 20, "save_path": r"M:\TV", "tags": ["tow"]}

    monkeypatch.setattr("tow.web.services.client_from_secrets", lambda *a, **k: Fake())
    c = TestClient(app, headers={"Origin": "http://127.0.0.1"})
    c.post(
        "/topics/add",
        data={"url": "http://rutor.info/torrent/1/x", "title": "A", "save_path": r"M:\anime"},
        follow_redirects=False,
    )
    st = load_state()
    st["topics"][0]["hash"] = "AA" * 20
    from tow.store import save_state

    save_state(st)
    tid = st["topics"][0]["id"]
    r = c.post(
        f"/topics/{tid}/edit",
        data={"title": "A", "url": "http://rutor.info/torrent/1/x", "save_path": r"M:\TV"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert seen == [("AA" * 20, r"M:\TV")]

    assert "перенёс" in shown(r.headers.get("location") or "")
    assert "move_pending" not in load_state()["topics"][0]


def test_edit_path_records_move_in_progress(monkeypatch):
    monkeypatch.setattr("tow.web.services.run_check", lambda **kw: {"qbit": "ok", "results": []})
    monkeypatch.setattr("tow.check.client_ops.RELOCATION_WAIT_SEC", 0.0)

    class MovingClient:
        def set_location(self, infohash: str, save_path: str) -> str:
            return "ok"

        def has_hash(self, infohash: str) -> bool:
            return True

        def inspect_torrent(self, infohash: str) -> dict:
            return {"hash": "AA" * 20, "save_path": r"M:\anime", "state": "moving", "tags": ["tow"]}

    monkeypatch.setattr("tow.web.services.client_from_secrets", lambda *a, **k: MovingClient())
    c = TestClient(app, headers={"Origin": "http://127.0.0.1"})
    c.post(
        "/topics/add",
        data={"url": "http://rutor.info/torrent/1/x", "title": "A", "save_path": r"M:\anime"},
        follow_redirects=False,
    )
    st = load_state()
    st["topics"][0]["hash"] = "AA" * 20
    from tow.store import save_state

    save_state(st)
    tid = st["topics"][0]["id"]
    r = c.post(
        f"/topics/{tid}/edit",
        data={"title": "A", "url": "http://rutor.info/torrent/1/x", "save_path": r"M:\TV"},
        follow_redirects=False,
    )

    assert "переносит" in shown(r.headers.get("location") or "")
    topic = load_state()["topics"][0]
    assert topic["save_path"] == r"M:\TV"
    assert topic["move_pending"]["from"] == r"M:\anime"
    assert topic["move_pending"]["to"] == r"M:\TV"


def test_add_runs_check_apply(monkeypatch):
    monkeypatch.setattr("tow.web.services.guess_topic_title", lambda _url: "")  # no real tracker request
    seen: dict = {}

    def fake(**kw):
        seen.update(kw)
        return {"qbit": "ok", "results": []}

    monkeypatch.setattr("tow.web.services.run_check", fake)
    r = TestClient(app, headers={"Origin": "http://127.0.0.1"}).post(
        "/topics/add",
        data={"url": "http://rutor.info/torrent/1/x", "title": "X", "save_path": r"M:\TV"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert seen.get("apply") is True
    assert seen.get("notify") is True
    kinds = [e["kind"] for e in read_events(limit=20)]
    assert "topic_add" in kinds


@pytest.mark.parametrize(("path", "data"), [("/topics/t1/check", {}), ("/check", {"mode": "apply"})])
def test_manual_checks_notify_like_scheduled_ones(monkeypatch, path, data):
    # B1: manual checks saved history with notify=False, so their events were never sent.
    seen: dict = {}

    def fake(**kw):
        seen.update(kw)
        return {"qbit": "ok", "results": []}

    monkeypatch.setattr("tow.web.services.run_check", fake)
    client = TestClient(app, headers={"Origin": "http://127.0.0.1"})
    response = client.post(path, data=data, follow_redirects=False)

    assert response.status_code == 303
    job = parse_qs(urlparse(response.headers["location"]).query).get("check_job")
    if job:  # "check all" runs in the background (D1)
        assert wait_for_check_job(client, job[0])["status"] == "done"
    assert seen["notify"] is True
    assert seen["how"] == "manual"


def test_nnmclub_auth_failure_opens_home_credential_prompt(monkeypatch):
    def fake(**_kw):
        topic = load_state()["topics"][-1]
        return {
            "qbit": "ok",
            "results": [{"id": topic["id"], "ok": False, "error": "nnmclub: no download link on page"}],
        }

    monkeypatch.setattr("tow.web.services.run_check", fake)
    c = TestClient(app, headers={"Origin": "http://127.0.0.1"})
    response = c.post(
        "/topics/add",
        data={
            "url": "https://nnmclub.to/forum/viewtopic.php?t=2345680",
            "title": "Сериал В 3",
            "save_path": r"M:\\anime",
        },
        follow_redirects=False,
    )

    assert response.status_code == 303
    location = shown(response.headers["location"])
    topic_id = load_state()["topics"][-1]["id"]
    assert f"credential_topic={topic_id}" in location
    assert "qBittorrent не подтвердил" not in location

    home = c.get(location)
    assert home.status_code == 200
    assert f'action="/topics/{topic_id}/tracker-browser-auth"' in home.text
    assert 'name="password"' not in home.text
    dialog = home.text.split('<dialog class="credential-prompt"', 1)[1].split("</dialog>", 1)[0]
    assert "Нужен вход на сайт" in dialog
    assert 'data-native-post="1"' not in dialog
    assert "Войти" in dialog
    assert "NNMClub" not in dialog
    assert "Сериал В" in dialog
    assert "Turnstile" not in dialog


def test_browser_auth_get_redirects_to_home_prompt():
    topic_id = "nnm-browser-get"
    response = TestClient(app).get(f"/topics/{topic_id}/tracker-browser-auth", follow_redirects=False)

    assert response.status_code == 303
    location = shown(response.headers["location"])
    assert "credential_topic=nnm-browser-get" in location
    assert "нужен вход на сайт" in location


def test_nnmclub_browser_auth_start_returns_operation_and_does_not_accept_password(monkeypatch):
    topic_id = "nnm-browser"
    save_state(
        {
            "topics": [
                {
                    "id": topic_id,
                    "title": "NNM",
                    "url": "https://nnmclub.to/forum/viewtopic.php?t=2345680",
                    "save_path": r"M:\\anime",
                    "hash": None,
                }
            ],
            "mirrors": {},
        }
    )
    seen = {}
    monkeypatch.setattr(
        "tow.web.services.browser_auth.start",
        lambda **kw: (
            seen.update(kw)
            or {"operation_id": "browser-auth-test", "status": "starting", "message": "открываю браузер"}
        ),
    )
    response = TestClient(app, headers={"Origin": "http://127.0.0.1"}).post(
        f"/topics/{topic_id}/tracker-browser-auth", follow_redirects=False
    )

    assert response.status_code == 303
    location = shown(response.headers["location"])
    assert "browser_auth_id=browser-auth-test" in location
    assert seen["topic_id"] == topic_id
    assert seen["start_url"] == ("https://nnmclub.to/forum/login.php?redirect=viewtopic.php%3Ft%3D2345680")
    assert seen["timeout_sec"] == 900
    assert callable(seen["on_success"])


def test_browser_auth_callback_persists_session_and_rechecks_one_topic(monkeypatch):
    from tow.web.routes_topic_login import _browser_auth_callback

    monkeypatch.setattr("tow.web.services.load_config", lambda: {"trackers": {"nnmclub": NNM_HTTP_SPEC}})
    old = {"trackers": {"nnmclub": {"username": "saved-user", "password": "saved-pass"}}}
    saved = []
    seen = {}
    monkeypatch.setattr("tow.web.services.load_secrets", lambda: old)
    monkeypatch.setattr("tow.web.services.save_secrets", lambda value: saved.append(json.loads(json.dumps(value))))
    monkeypatch.setattr("tow.web.services.log_event", lambda *args, **kwargs: None)

    def check(**kwargs):
        seen.update(kwargs)
        return {"qbit": "ok", "results": [{"id": "browser-topic", "ok": True, "added": True}]}

    monkeypatch.setattr("tow.web.services.run_check", check)
    result = _browser_auth_callback("browser-topic", "nnmclub", "https://nnmclub.to/forum/viewtopic.php?t=2345680")(
        {"cf_clearance": "opaque", "session": "opaque"},
        "Mozilla/5.0 Edge/Test",
    )

    assert result["ok"] is True
    assert seen == {"apply": True, "notify": True, "ids": ["browser-topic"], "ignore_cool": True, "how": "manual"}
    assert saved[-1]["trackers"]["nnmclub"]["cookies_by_origin"]["https://nnmclub.to:443"] == {
        "cf_clearance": "opaque",
        "session": "opaque",
    }
    assert saved[-1]["trackers"]["nnmclub"]["browser_user_agent"] == "Mozilla/5.0 Edge/Test"
    assert saved[-1]["trackers"]["nnmclub"]["username"] == "saved-user"


def test_browser_auth_callback_requires_matching_successful_check_result(monkeypatch):
    from tow.web.routes_topic_login import _browser_auth_callback

    monkeypatch.setattr("tow.web.services.load_config", lambda: {"trackers": {"nnmclub": NNM_HTTP_SPEC}})
    monkeypatch.setattr("tow.web.services.load_secrets", lambda: {"trackers": {}})
    monkeypatch.setattr("tow.web.services.save_secrets", lambda _value: None)
    monkeypatch.setattr("tow.web.services.log_event", lambda *args, **kwargs: None)
    callback = _browser_auth_callback("wanted", "nnmclub", "https://nnmclub.to/forum/viewtopic.php?t=1")

    monkeypatch.setattr("tow.web.services.run_check", lambda **_kwargs: {"results": []})
    missing = callback({"session": "opaque"}, "UA")
    assert missing["ok"] is False

    monkeypatch.setattr(
        "tow.web.services.run_check", lambda **_kwargs: (_ for _ in ()).throw(RuntimeError("storage failed"))
    )
    failed = callback({"session": "opaque"}, "UA")
    assert failed["ok"] is False

    monkeypatch.setattr(
        "tow.web.services.run_check",
        lambda **_kwargs: {"results": [{"id": "wanted", "ok": False, "error": "qbit down"}]},
    )
    qbit_failure = callback({"session": "opaque"}, "UA")
    assert qbit_failure["ok"] is False

    monkeypatch.setattr(
        "tow.web.services.run_check",
        lambda **_kwargs: {
            "results": [
                {"id": "wanted", "ok": True, "source": "magnet", "fallback_reason": "tracker_auth", "added": True}
            ]
        },
    )
    magnet_fallback = callback({"session": "opaque"}, "UA")
    assert magnet_fallback["ok"] is False


def test_browser_auth_fallback_restores_only_its_own_unverified_session(monkeypatch, tmp_path):
    from tow.web.routes_topic_login import _browser_auth_callback

    monkeypatch.setenv("TOW_HOME", str(tmp_path))
    monkeypatch.setattr("tow.web.services.load_config", lambda: {"trackers": {"nnmclub": NNM_HTTP_SPEC}})
    stored = {"trackers": {"nnmclub": {"username": "prior"}, "other": {"username": "safe"}}}
    monkeypatch.setattr("tow.web.services.load_secrets", lambda: copy.deepcopy(stored))

    def save(value):
        stored.clear()
        stored.update(copy.deepcopy(value))

    monkeypatch.setattr("tow.web.services.save_secrets", save)
    monkeypatch.setattr("tow.web.services.log_event", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        "tow.web.services.run_check",
        lambda **_kwargs: {
            "results": [
                {"id": "wanted", "ok": True, "source": "magnet", "fallback_reason": "tracker_auth", "added": True}
            ]
        },
    )

    result = _browser_auth_callback("wanted", "nnmclub", "https://nnmclub.to/forum/viewtopic.php?t=1")(
        {"session": "unverified"}, "UA"
    )

    assert result["ok"] is False
    assert stored["trackers"]["nnmclub"] == {"username": "prior"}
    assert stored["trackers"]["other"] == {"username": "safe"}


def test_late_browser_auth_cannot_recreate_deleted_site_secrets(monkeypatch, tmp_path):
    from tow.web.routes_topic_login import _browser_auth_callback

    monkeypatch.setenv("TOW_HOME", str(tmp_path))
    monkeypatch.setattr("tow.web.services.load_config", lambda: {"trackers": {}})
    monkeypatch.setattr("tow.web.services.load_secrets", lambda: {"trackers": {}})
    monkeypatch.setattr("tow.web.services.save_secrets", lambda _value: pytest.fail("deleted site secret write"))
    monkeypatch.setattr("tow.web.services.run_check", lambda **_kwargs: pytest.fail("check deleted site"))

    result = _browser_auth_callback("wanted", "nnmclub", "https://nnmclub.to/forum/viewtopic.php?t=1")(
        {"session": "late"}, "UA"
    )
    assert result["ok"] is False
    assert "удалён" in result["message"]


def test_login_retry_never_claims_magnet_fallback_verified():
    from tow.web.routes_topic_login import _tracker_login_retry_flash

    message, prompt_again, kind = _tracker_login_retry_flash(
        {"ok": True, "source": "magnet", "fallback_reason": "tracker_auth", "added": True}, "nnmclub"
    )
    assert "не подтверждён" in message
    assert prompt_again is True
    assert kind == "warn"


def test_tracker_login_for_browser_auth_tracker_never_saves_form_password(monkeypatch):
    topic_id = "nnm-browser-login"
    save_state(
        {
            "topics": [
                {
                    "id": topic_id,
                    "title": "NNM",
                    "url": "https://nnmclub.to/forum/viewtopic.php?t=2345680",
                    "save_path": r"M:\\anime",
                    "hash": None,
                }
            ],
            "mirrors": {},
        }
    )
    saved = []
    monkeypatch.setattr("tow.web.services.load_secrets", lambda: {"trackers": {"nnmclub": {}}})
    monkeypatch.setattr("tow.web.services.save_secrets", lambda value: saved.append(value))

    response = TestClient(app, headers={"Origin": "http://127.0.0.1"}).post(
        f"/topics/{topic_id}/tracker-login",
        data={"username": "user", "password": "password"},
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert "для этого сайта нужен вход через браузер" in shown(response.headers["location"])
    assert saved == []


def test_tracker_login_saves_encrypted_site_credentials_and_retries_topic(monkeypatch):
    monkeypatch.setattr(
        "tow.web.services.load_config", lambda: {"trackers": {"nnmclub": NNM_HTTP_SPEC}, "language": "ru"}
    )
    topic_id = "nnm-topic"
    save_state(
        {
            "topics": [
                {
                    "id": topic_id,
                    "title": "NNM",
                    "url": "https://nnmclub.to/forum/viewtopic.php?t=2345680",
                    "save_path": r"M:\\anime",
                    "hash": None,
                }
            ],
            "mirrors": {},
        }
    )
    secrets = {"trackers": {"nnmclub": {}}}
    saved: list[dict] = []
    seen: dict = {}
    monkeypatch.setattr("tow.web.services.load_secrets", lambda: copy.deepcopy(secrets))

    def save_login_secrets(value):
        snapshot = json.loads(json.dumps(value))
        secrets.clear()
        secrets.update(copy.deepcopy(snapshot))
        saved.append(snapshot)

    monkeypatch.setattr("tow.web.services.save_secrets", save_login_secrets)
    monkeypatch.setattr("tow.web.services.log_event", lambda *args, **kwargs: None)

    def fake_check(**kw):
        seen.update(kw)
        return {"qbit": "ok", "results": [{"id": topic_id, "ok": True, "added": True}]}

    monkeypatch.setattr("tow.web.services.run_check", fake_check)
    response = TestClient(app, headers={"Origin": "http://127.0.0.1"}).post(
        f"/topics/{topic_id}/tracker-login",
        data={"username": "nnm-user", "password": "nnm-pass"},
        follow_redirects=False,
    )

    assert response.status_code == 303
    location = shown(response.headers["location"])
    assert "credential_topic=" not in location
    assert "вход на nnmclub выполнен" in location
    assert "nnm-pass" not in location
    assert saved[-1]["trackers"]["nnmclub"] == {"username": "nnm-user", "password": "nnm-pass"}
    assert seen == {
        "apply": True,
        "notify": True,
        "ids": [topic_id],
        "how": "manual",
        "ignore_cool": True,
        "wait": False,  # a request never waits for another running check
    }


def test_tracker_login_failure_keeps_home_prompt_and_shows_error(monkeypatch):
    monkeypatch.setattr("tow.web.services.load_config", lambda: {"trackers": {"nnmclub": NNM_HTTP_SPEC}})
    topic_id = "nnm-topic-fail"
    save_state(
        {
            "topics": [
                {
                    "id": topic_id,
                    "title": "NNM",
                    "url": "https://nnmclub.to/forum/viewtopic.php?t=2345680",
                    "save_path": r"M:\\anime",
                    "hash": None,
                }
            ],
            "mirrors": {},
        }
    )
    secrets = {"trackers": {"nnmclub": {"username": "old-user", "password": "old-pass"}}}
    saved: list[dict] = []
    monkeypatch.setattr("tow.web.services.load_secrets", lambda: copy.deepcopy(secrets))

    def save_login_secrets(value):
        snapshot = json.loads(json.dumps(value))
        secrets.clear()
        secrets.update(copy.deepcopy(snapshot))
        saved.append(snapshot)

    monkeypatch.setattr("tow.web.services.save_secrets", save_login_secrets)
    monkeypatch.setattr("tow.web.services.log_event", lambda *args, **kwargs: None)
    monkeypatch.setattr(
        "tow.web.services.run_check",
        lambda **_kw: {
            "qbit": "ok",
            "results": [{"id": topic_id, "ok": False, "error": "nnmclub: no download link on page"}],
        },
    )

    response = TestClient(app, headers={"Origin": "http://127.0.0.1"}).post(
        f"/topics/{topic_id}/tracker-login",
        data={"username": "wrong", "password": "wrong"},
        follow_redirects=False,
    )

    location = shown(response.headers["location"])
    assert response.status_code == 303
    assert f"credential_topic={topic_id}" in location
    assert "nnmclub: no download link on page" in location
    assert "qBittorrent не подтвердил" not in location
    assert saved[-1]["trackers"]["nnmclub"] == {"username": "old-user", "password": "old-pass"}


def test_password_login_magnet_fallback_restores_only_submitted_credentials(monkeypatch, tmp_path):
    monkeypatch.setenv("TOW_HOME", str(tmp_path))
    monkeypatch.setattr("tow.web.services.load_config", lambda: {"trackers": {"nnmclub": NNM_HTTP_SPEC}})
    topic_id = "password-magnet"
    save_state(
        {
            "topics": [
                {
                    "id": topic_id,
                    "title": "NNM",
                    "url": "https://nnmclub.to/forum/viewtopic.php?t=2345680",
                    "save_path": r"M:\anime",
                    "hash": "OLD",
                }
            ],
            "mirrors": {},
        }
    )
    stored = {
        "trackers": {
            "nnmclub": {"username": "old-user", "password": "old-pass"},
            "other": {"username": "old-other"},
        }
    }
    monkeypatch.setattr("tow.web.services.load_secrets", lambda: copy.deepcopy(stored))

    def save(value):
        stored.clear()
        stored.update(copy.deepcopy(value))

    monkeypatch.setattr("tow.web.services.save_secrets", save)
    monkeypatch.setattr("tow.web.services.log_event", lambda *args, **kwargs: None)

    def check(**_kwargs):
        stored["trackers"]["other"]["username"] = "updated-other"
        return {
            "results": [
                {
                    "id": topic_id,
                    "ok": True,
                    "source": "magnet",
                    "fallback_reason": "tracker_auth",
                    "added": True,
                }
            ]
        }

    monkeypatch.setattr("tow.web.services.run_check", check)
    response = TestClient(app, headers={"Origin": "http://127.0.0.1"}).post(
        f"/topics/{topic_id}/tracker-login",
        data={"username": "wrong", "password": "wrong"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert stored["trackers"]["nnmclub"]["username"] == "old-user"
    assert stored["trackers"]["nnmclub"]["password"] == "old-pass"
    assert stored["trackers"]["other"]["username"] == "updated-other"


def test_manual_add_bypasses_mirror_cooldown_and_reports_tracker_error(monkeypatch):
    monkeypatch.setattr("tow.web.services.guess_topic_title", lambda _url: "")  # no real tracker request
    seen: dict = {}

    def fake(**kw):
        seen.update(kw)
        state = load_state()
        topic = state["topics"][-1]
        return {
            "qbit": "ok",
            "results": [
                {
                    "id": topic["id"],
                    "ok": False,
                    "error": "rutor: все зеркала на паузе",
                }
            ],
        }

    monkeypatch.setattr("tow.web.services.run_check", fake)
    response = TestClient(app, headers={"Origin": "http://127.0.0.1"}).post(
        "/topics/add",
        data={"url": "http://rutor.info/torrent/1234569/x", "title": "X", "save_path": r"M:\TV"},
        follow_redirects=False,
    )

    location = shown(response.headers.get("location") or "")
    assert response.status_code == 303
    assert seen == {
        "apply": True,
        "notify": True,
        "ids": [load_state()["topics"][-1]["id"]],
        "how": "manual",
        "ignore_cool": True,
        "wait": False,
    }
    assert "rutor: все зеркала на паузе" in location
    assert "qBittorrent не подтвердил" not in location


def test_topic_check_auth_failure_redirects_to_home_prompt(monkeypatch):
    save_state(
        {
            "topics": [
                {
                    "id": "nnm-update",
                    "title": "NNM update",
                    "url": "https://nnmclub.to/forum/viewtopic.php?t=2345680",
                    "save_path": r"M:\\TV",
                    "hash": None,
                }
            ],
            "mirrors": {},
        }
    )
    monkeypatch.setattr(
        "tow.web.services.run_check",
        lambda **kw: {
            "qbit": "v5.2.3",
            "results": [
                {
                    "id": "nnm-update",
                    "tracker": "nnmclub",
                    "ok": False,
                    "error": "nnmclub: no download link on page",
                }
            ],
        },
    )
    response = TestClient(app, headers={"Origin": "http://127.0.0.1"}).post(
        "/topics/nnm-update/check", follow_redirects=False
    )

    location = shown(response.headers.get("location") or "")
    assert response.status_code == 303
    assert "credential_topic=nnm-update" in location
    assert "nnmclub: no download link on page" in location


def test_topic_check_reports_recovered_client_add(monkeypatch):
    monkeypatch.setattr(
        "tow.web.services.run_check",
        lambda **kw: {
            "qbit": "v5.2.3",
            "results": [
                {
                    "id": "recovered",
                    "ok": True,
                    "added": True,
                    "pending_add_recovered": True,
                    "changed": False,
                }
            ],
        },
    )

    response = TestClient(app, headers={"Origin": "http://127.0.0.1"}).post(
        "/topics/recovered/check", follow_redirects=False
    )

    assert response.status_code == 303
    assert "добавлено в торрент-клиент" in shown(response.headers["location"]).replace("+", " ")


def test_add_fills_title_from_page(monkeypatch):
    monkeypatch.setattr("tow.web.services.run_check", lambda **kw: {"qbit": "ok", "results": []})
    monkeypatch.setattr(
        "tow.web.services.guess_topic_title",
        lambda url, **k: "Сериал Д [01x01-05 из 10]",
    )
    r = TestClient(app, headers={"Origin": "http://127.0.0.1"}).post(
        "/topics/add",
        data={
            "url": "http://rutor.info/torrent/1234568/serial-d-01x01-05-iz-10",
            "title": "",
            "save_path": r"M:\TV",
        },
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert load_state()["topics"][-1]["title"].startswith("Сериал Д")


def test_add_replaces_slug_title(monkeypatch):
    monkeypatch.setattr("tow.web.services.run_check", lambda **kw: {"qbit": "ok", "results": []})
    monkeypatch.setattr("tow.web.services.guess_topic_title", lambda url, **k: "Сериал Д [01x01-05 из 10]")
    url = "http://rutor.info/torrent/1234568/serial-d-01x01-05-iz-10-2026-webrip-1080p-ot-exkinoray"
    r = TestClient(app, headers={"Origin": "http://127.0.0.1"}).post(
        "/topics/add",
        data={
            "url": url,
            "title": "serial-d-01x01-05-iz-10-2026-webrip-1080p-ot-exkinoray",
            "save_path": r"M:\TV",
        },
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert load_state()["topics"][-1]["title"].startswith("Сериал Д")


def test_add_cdn_becomes_rutor_topic(monkeypatch):
    monkeypatch.setattr("tow.web.services.run_check", lambda **kw: {"qbit": "ok", "results": []})
    monkeypatch.setattr("tow.web.services.guess_topic_title", lambda url, **k: "Сериал Д")
    r = TestClient(app, headers={"Origin": "http://127.0.0.1"}).post(
        "/topics/add",
        data={"url": "https://d.rutor.info/download/1234568", "title": "", "save_path": r"M:\TV\Show"},
        follow_redirects=False,
    )
    assert r.status_code == 303
    t = load_state()["topics"][-1]
    assert t["url"] == "http://rutor.info/torrent/1234568"
    assert t["save_path"] == r"M:\TV\Show"
    assert load_state()["save_roots"] == [r"M:\TV\Show"]


def test_add_empty_path_uses_last_root(monkeypatch):
    monkeypatch.setattr("tow.web.services.run_check", lambda **kw: {"qbit": "ok", "results": []})
    c = TestClient(app, headers={"Origin": "http://127.0.0.1"})
    c.post(
        "/topics/add",
        data={"url": "http://rutor.info/torrent/1/x", "title": "A", "save_path": r"M:\TV\Show"},
        follow_redirects=False,
    )
    r = c.post(
        "/topics/add",
        data={"url": "http://rutor.info/torrent/2/y", "title": "B", "save_path": ""},
        follow_redirects=False,
    )
    assert r.status_code == 303
    assert "укажи" not in (r.headers.get("location") or "")
    assert load_state()["topics"][-1]["save_path"] == r"M:\TV\Show"


def test_add_empty_path_without_history(monkeypatch):
    monkeypatch.setattr("tow.web.services.run_check", lambda **kw: {"qbit": "ok", "results": []})
    client = TestClient(app, headers={"Origin": "http://127.0.0.1"})
    r = client.post(
        "/topics/add",
        data={"url": "http://rutor.info/torrent/1/x", "title": "A", "save_path": ""},
        follow_redirects=False,
    )
    assert r.status_code == 303

    assert "укажи" in client.get(r.headers["location"]).text
    assert load_state().get("topics") == []


def test_guess_title_json(monkeypatch):
    monkeypatch.setattr("tow.web.services.guess_topic_title", lambda url, **k: "Hello World")
    r = TestClient(app, headers={"Origin": "http://127.0.0.1"}).post(
        "/topics/guess-title",
        data={"url": "http://rutor.info/torrent/1/hello_world"},
    )
    assert r.status_code == 200
    j = r.json()
    assert j["ok"] is True
    assert j["title"] == "Hello World"


def test_header_dots_follow_health():
    from tow.store import save_state

    save_state(
        {
            "health": {"qbit_ok": False, "telegram_ok": False},
            "doctor": {
                "probes": [
                    {"tracker": "kinozal", "ok": False, "host": "https://x"},
                    {"tracker": "rutor", "ok": True, "host": "https://y"},
                ]
            },
            "topics": [
                {"id": "1", "url": "https://kinozal.guru/details.php?id=1", "title": "A"},
                {"id": "2", "url": "http://rutor.info/torrent/1/x", "title": "B"},
            ],
        }
    )
    t = TestClient(app, headers={"Origin": "http://127.0.0.1"}).get("/").text
    assert 'title="qBit"' not in t or "hdr-svc" in t
    assert "hdr-sites" in t
    assert "hdr-svc" in t
    assert 'title="qBittorrent: Нет связи"' in t
    # No messenger connected: the header says so (it used to call it "бот молчит").
    assert 'class="trk trk-ico mut" href="/settings?open=bots" title="Уведомления: не подключены"' in t
    assert 'title="Kinozal"' in t
    assert "trk bad" in t
    assert "trk ok" in t
    assert "chip svc" not in t


def test_ui_explains_observation_deletion_and_qbit_relocation():
    state = load_state()
    state["topics"] = [
        {
            "id": "ui-topic",
            "title": "UI test topic",
            "url": "https://tracker.example/details.php?id=1",
            "save_path": r"M:\\TV",
            "hash": None,
        }
    ]
    save_state(state)
    c = TestClient(app, headers={"Origin": "http://127.0.0.1"})
    home = c.get("/").text + c.get("/topics/ui-topic/edit-panel").text  # M2
    sites = c.get("/sites").text
    assert "Убрать раздачу из TOW" in home
    assert "Изменение папки: TOW попросит торрент-клиент" in home
    assert "style=" not in home
    assert "style=" not in sites
    assert "Шаблон ссылки на раздачу (regex)" in sites


def test_settings_supports_distinct_client_forms_and_ping(monkeypatch):
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
            "main": {"host": "main-host", "username": "main-user", "password": "[REDACTED]"},
            "backup": {"host": "backup-host", "username": "backup-user", "password": "[REDACTED]"},
        }
    }
    monkeypatch.setattr("tow.web.services.load_config", lambda: cfg)
    monkeypatch.setattr("tow.web.services.load_secrets", lambda: secrets)
    page = TestClient(app, headers={"Origin": "http://127.0.0.1"}).get("/settings").text
    # save + check for each, plus "make default" and "remove" for the non-default one
    assert page.count('name="client_id"') == 6
    assert page.count('action="/settings/client/default"') == 1
    assert page.count('action="/settings/client/remove"') == 1
    assert page.count('id="q_host-') == 2
    assert page.count('id="q_port-') == 2
    assert page.count('id="q_user-') == 2
    assert page.count('id="q_pass-') == 2
    assert "main-host" in page
    assert "backup-host" in page
    assert page.count('action="/settings/client/ping"') == 2

    seen = []

    class FakeClient:
        def ping(self):
            seen.append("ping")
            return "ok"

    monkeypatch.setattr(
        "tow.web.services.client_from_secrets",
        lambda cfg, secrets, client_id=None: seen.append(client_id) or FakeClient(),
    )
    monkeypatch.setattr("tow.web.services.client_answers", lambda _client_id: True)
    response = TestClient(app, headers={"Origin": "http://127.0.0.1"}).post(
        "/settings/client/ping", data={"client_id": "backup"}, follow_redirects=False
    )
    assert response.status_code == 303
    assert "backup" in seen


def test_new_topic_routes_to_explicit_selected_client(monkeypatch):
    from tow.config import load_config as real_load_config

    cfg = real_load_config()
    cfg["clients"] = [
        {"id": "main", "kind": "qbittorrent", "default": True},
        {"id": "backup", "kind": "qbittorrent"},
    ]
    monkeypatch.setattr("tow.web.services.load_config", lambda: cfg)
    monkeypatch.setattr("tow.web.services.run_check", lambda **_kwargs: {"qbit": "ok", "results": []})
    client = TestClient(app, headers={"Origin": "http://127.0.0.1"})

    home = client.get("/")
    assert 'name="client_id"' in home.text
    response = client.post(
        "/topics/add",
        data={
            "url": "http://rutor.info/torrent/991/x",
            "title": "Explicit route",
            "save_path": r"M:\TV",
            "client_id": "backup",
        },
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert load_state()["topics"][-1]["client_id"] == "backup"


def test_invalid_topic_edit_is_no_write(monkeypatch):
    from tow.paths import state_path

    monkeypatch.setattr("tow.web.services.run_check", lambda **_kwargs: {"qbit": "ok", "results": []})
    state = {
        "topics": [
            {
                "id": "edit-no-write",
                "title": "Original",
                "url": "http://rutor.info/torrent/1/x",
                "save_path": r"M:\TV",
                "hash": None,
            }
        ],
        "mirrors": {},
    }
    save_state(state)
    before = state_path().read_bytes()

    response = TestClient(app, headers={"Origin": "http://127.0.0.1"}).post(
        "/topics/edit-no-write/edit",
        data={"title": "Must not stick", "url": "https://unknown.invalid/topic/1", "save_path": r"M:\TV"},
        follow_redirects=False,
    )

    assert response.status_code == 303
    assert state_path().read_bytes() == before


def test_download_history_pagination_exposes_second_page():
    save_state(
        {
            "topics": [
                {
                    "id": "many-downloads",
                    "title": "Many",
                    "url": "http://rutor.info/torrent/1/x",
                    "save_path": r"M:\TV",
                }
            ],
            "mirrors": {},
        }
    )
    save_download_history(
        {
            "schema_version": 1,
            "topics": {
                "many-downloads": {
                    "items": {
                        f"item-{index:03d}": {
                            "identity": f"item-{index:03d}",
                            "label": f"Episode {index:03d}",
                            "status": "seen",
                        }
                        for index in range(105)
                    }
                }
            },
        }
    )
    client = TestClient(app)

    first = client.get("/topics/many-downloads/downloads.json?limit=100").json()
    second = client.get("/topics/many-downloads/downloads.json?limit=100&offset=100").json()

    assert len(first["items"]) == 100
    assert first["pagination"]["has_more"] is True
    assert len(second["items"]) == 5
    assert second["pagination"]["has_more"] is False
    assert {row["identity"] for row in first["items"]}.isdisjoint({row["identity"] for row in second["items"]})


def test_lan_management_requires_authenticated_session(monkeypatch):
    token = "t" * 32
    monkeypatch.setattr("tow.web.services.load_config", lambda: {"bind": "0.0.0.0", "allow_lan": True})
    monkeypatch.setenv("TOW_LAN_AUTH_TOKEN", token)
    c = TestClient(app, headers={"Origin": "http://127.0.0.1"}, client=("192.168.1.7", 50000))

    assert c.get("/healthz").status_code == 200
    assert c.get("/settings").status_code == 401
    login = c.post("/login", data={"token": token}, follow_redirects=False)
    assert login.status_code == 303
    page = c.get("/settings")
    assert page.status_code == 200
    assert 'action="/logout"' in page.text  # the session is ended at the end of Settings
    assert 'action="/logout"' not in c.get("/").text  # not from the header of every page
    assert 'action="/logout"' not in TestClient(app).get("/settings").text  # this computer has none

    logout = c.post("/logout", headers={"Origin": "http://127.0.0.1"}, follow_redirects=False)
    assert logout.status_code == 303
    assert c.get("/settings").status_code == 401


def test_lan_management_fails_closed_without_external_auth_token(monkeypatch):
    monkeypatch.setattr("tow.web.services.load_config", lambda: {"bind": "0.0.0.0", "allow_lan": True})
    monkeypatch.delenv("TOW_LAN_AUTH_TOKEN", raising=False)
    monkeypatch.delenv("TOW_LAN_AUTH_TOKEN_FILE", raising=False)
    c = TestClient(app, headers={"Origin": "http://127.0.0.1"}, client=("192.168.1.7", 50000))

    assert c.get("/settings").status_code == 503


def test_lan_login_still_requires_same_origin(monkeypatch):
    monkeypatch.setattr("tow.web.services.load_config", lambda: {"bind": "0.0.0.0", "allow_lan": True})
    monkeypatch.setenv("TOW_LAN_AUTH_TOKEN", "t" * 32)
    c = TestClient(app)

    response = c.post("/login", data={"token": "test-lan-token"}, headers={"Origin": "https://evil.example"})
    assert response.status_code == 403


def test_lan_login_page_and_session_cookie_are_rendered_safely(monkeypatch):
    token = "t" * 32
    monkeypatch.setattr("tow.web.services.load_config", lambda: {"bind": "0.0.0.0", "allow_lan": True})
    monkeypatch.setenv("TOW_LAN_AUTH_TOKEN", token)
    c = TestClient(app, headers={"Origin": "http://127.0.0.1"})

    page = c.get("/login")
    assert page.status_code == 200
    assert 'name="password"' in page.text
    assert "app.css?v=" in page.text

    response = c.post("/login", data={"token": "wrong"}, follow_redirects=False)
    assert response.status_code == 401
    assert "tow_session=" not in response.headers.get("set-cookie", "")

    response = c.post("/login", data={"token": token}, follow_redirects=False)
    assert response.status_code == 303
    cookie = response.headers["set-cookie"]
    assert "HttpOnly" in cookie
    assert "SameSite=lax" in cookie
    assert "Path=/" in cookie
    assert "Secure" not in cookie


def test_edit_path_refuses_client_move_without_tow_ownership(monkeypatch):
    monkeypatch.setattr("tow.web.services.run_check", lambda **kw: {"qbit": "ok", "results": []})
    seen: list[tuple[str, str]] = []

    class Fake:
        def set_location(self, infohash: str, save_path: str) -> str:
            seen.append((infohash, save_path))
            return "ok"

        def has_hash(self, infohash: str) -> bool:
            return True

        def inspect_torrent(self, infohash: str) -> dict:
            return {"hash": "AA" * 20, "save_path": r"M:\\TV", "tags": []}

    monkeypatch.setattr("tow.web.services.client_from_secrets", lambda *a, **k: Fake())
    c = TestClient(app, headers={"Origin": "http://127.0.0.1"})
    c.post(
        "/topics/add",
        data={"url": "http://rutor.info/torrent/1/x", "title": "A", "save_path": r"M:\\anime"},
        follow_redirects=False,
    )
    st = load_state()
    st["topics"][0]["hash"] = "AA" * 20
    from tow.store import save_state

    save_state(st)
    tid = st["topics"][0]["id"]
    r = c.post(
        f"/topics/{tid}/edit",
        data={"title": "A", "url": "http://rutor.info/torrent/1/x", "save_path": r"M:\\TV"},
        follow_redirects=False,
    )

    assert r.status_code == 303
    assert seen == []
    assert "не подтвердил" in shown(r.headers.get("location") or "")


def test_add_secret_gate_is_not_reported_as_success(monkeypatch):
    from tow.store import SecretStoreError

    def blocked(**_kwargs):
        raise SecretStoreError("legacy plaintext TOW secrets require explicit migrate")

    sent = []
    monkeypatch.setattr("tow.web.services.run_check", blocked)
    monkeypatch.setattr("tow.web.services.notify_send", lambda *args, **kwargs: sent.append((args, kwargs)))
    c = TestClient(app, headers={"Origin": "http://127.0.0.1"})

    response = c.post(
        "/topics/add",
        data={"url": "http://rutor.info/torrent/1/x", "title": "Blocked", "save_path": r"M:\\anime"},
        follow_redirects=False,
    )

    location = shown(response.headers.get("location") or "")
    assert response.status_code == 303
    assert "проверка заблокирована" in location
    assert "добавлено" not in location
    assert sent == []


def test_tracker_degraded_topic_is_warning_on_home():
    state = load_state()
    state["topics"] = [
        {
            "id": "tracker-warning",
            "title": "Example",
            "url": "http://rutor.info/torrent/1/example",
            "save_path": r"M:\\anime",
            "last_ok": False,
            "last_error": "rutor: все зеркала на паузе",
            "last_changed": False,
        }
    ]
    save_state(state)

    html = TestClient(app, headers={"Origin": "http://127.0.0.1"}).get("/").text

    assert 'class="dot lg warn"' in html
    assert 'class="dot lg bad"' not in html


def test_frozen_tracker_degradation_is_warning_on_home():
    state = load_state()
    state["topics"] = [
        {
            "id": "frozen-warning",
            "title": "Frozen example",
            "url": "http://rutor.info/torrent/2/frozen-example",
            "save_path": r"M:\\anime",
            "last_ok": False,
            "last_error": "rutor: frozen",
            "last_changed": False,
        }
    ]
    save_state(state)

    html = TestClient(app, headers={"Origin": "http://127.0.0.1"}).get("/").text

    assert 'class="dot lg warn"' in html
    assert 'class="dot lg bad"' not in html


def test_successful_torrent_action_does_not_hide_latest_tracker_failure():
    topic_id = "known-good"
    state = load_state()
    state["topics"] = [
        {
            "id": topic_id,
            "title": "Known good",
            "url": "http://rutor.info/torrent/3/known-good",
            "hash": "A" * 40,
            "save_path": r"M:\\anime",
            "last_ok": False,
            "last_error": "rutor: все зеркала на паузе",
            "last_changed": False,
            "last_check": "14.09.2026 01:00:00 IL+03",
        }
    ]
    save_state(state)
    history = load_download_history()
    history["topics"] = {
        topic_id: {
            "client_present": True,
            "last_event": {
                "kind": "episode_completed",
                "label": "S01E01",
                "at": "2026-09-14T00:30:00+03:00",
                "source": "tow",
            },
            "items": {},
        }
    }
    save_download_history(history)

    html = TestClient(app, headers={"Origin": "http://127.0.0.1"}).get("/").text

    # The latest tracker error comes first (AGENTS.md), even after a recorded client event;
    # every status mark is labelled, also for a record without an error class.
    assert re.search(r'class="dot lg warn" title="[^"]+"', html)
    assert re.search(r'class="row-ico warn" href="[^"]+" target="_blank" rel="noopener" title="[^"]+"', html)
    assert 'class="dot lg ok"' not in html


def test_undo_of_topic_edit_keeps_status_written_after_the_edit(monkeypatch):
    from tow.store import save_state

    monkeypatch.setattr("tow.web.services.run_check", lambda **kw: {"qbit": "ok", "results": []})
    c = TestClient(app, headers={"Origin": "http://127.0.0.1"})
    c.post(
        "/topics/add",
        data={"url": "http://rutor.info/torrent/1/x", "title": "Old title", "save_path": r"M:\anime"},
        follow_redirects=False,
    )
    tid = load_state()["topics"][0]["id"]
    c.post(
        f"/topics/{tid}/edit",
        data={"title": "New title", "url": "http://rutor.info/torrent/1/x", "save_path": r"M:\anime"},
        follow_redirects=False,
    )
    st = load_state()  # a scheduled check records a fresh failure after the edit
    st["topics"][0].update({"last_ok": False, "last_error": "tracker: all hosts failed", "hash": "AB" * 20})
    save_state(st)

    assert c.post("/undo", follow_redirects=False).status_code == 303

    topic = load_state()["topics"][0]
    assert topic["title"] == "Old title"
    assert topic["last_error"] == "tracker: all hosts failed"
    assert topic["last_ok"] is False
    assert topic["hash"] == "AB" * 20


def test_check_all_runs_in_the_background_and_reports_when_done(monkeypatch):
    # D1: the request used to wait for the whole check while holding the HTTP lock.
    import threading

    release = threading.Event()

    def slow_check(**_kw):
        release.wait(5)
        return {"qbit": "ok", "results": [{"ok": True, "changed": True}, {"ok": False}]}

    monkeypatch.setattr("tow.web.services.run_check", slow_check)
    client = TestClient(app, headers={"Origin": "http://127.0.0.1"})

    started = client.post("/check", data={"mode": "apply"}, follow_redirects=False)
    job = parse_qs(urlparse(started.headers["location"]).query)["check_job"][0]
    assert "проверка запущена" in shown(started.headers["location"])
    assert client.get(f"/check/status?job={job}").json()["status"] == "running"
    again = client.post("/check", data={"mode": "apply"}, follow_redirects=False)
    assert "проверка уже идёт" in shown(again.headers["location"])

    release.set()
    status = wait_for_check_job(client, job)
    assert status["status"] == "done"
    assert status["flash"] == "применено: новое 1, без изменений 0, сбой 1"
    assert flash_of(status["redirect"]) == status["flash"]  # Home shows it; the address holds a token
    assert "применено" not in status["redirect"]
    assert client.get("/check/status?job=other").json() == {"status": "unknown"}


def test_forms_posted_by_app_js_get_json_instead_of_a_second_render(monkeypatch):
    # D6: fetch followed the 303 and the page was rendered twice per form.
    monkeypatch.setattr("tow.web.services.run_check", lambda **_kw: {"qbit": "ok", "results": []})
    client = TestClient(app, headers={"Origin": "http://127.0.0.1"})

    as_js = client.post("/check", data={"mode": "dry"}, headers={"X-TOW-Fetch": "1"}, follow_redirects=False)
    native = client.post("/check", data={"mode": "dry"}, follow_redirects=False)

    assert as_js.status_code == 200
    assert as_js.json()["redirect"].startswith("/?check_job=")
    assert native.status_code == 303


def test_fetch_redirect_keeps_the_login_cookie(monkeypatch):
    open_network(monkeypatch, token="t" * 32)
    client = TestClient(app, headers={"Origin": "http://127.0.0.1"})

    response = client.post("/login", data={"token": "t" * 32}, headers={"X-TOW-Fetch": "1"}, follow_redirects=False)

    assert response.status_code == 200
    assert "redirect" in response.json()
    assert "set-cookie" in response.headers


def test_a_refused_add_reopens_the_form_with_the_typed_values(monkeypatch):
    # D2: a validation error used to drop everything the owner typed.
    monkeypatch.setattr("tow.web.services.run_check", lambda **_kw: {"qbit": "ok", "results": []})
    client = TestClient(app, headers={"Origin": "http://127.0.0.1"})

    response = client.post(
        "/topics/add",
        data={
            "url": "http://rutor.info/torrent/1/x",
            "title": "Мой сериал",
            "save_path": "Media",  # relative: refused
            "selection_mode": "episodes",
            "selection_value": "S01E03-E05",
            "tracking_mode": "once",
        },
    )

    page = response.text
    assert '<details class="add card" id="new" open>' in page
    assert 'id="add-error" role="alert"' in page
    assert "<p>Не добавлено: " in page
    assert 'value="Media"' in page
    assert 'value="Мой сериал"' in page
    assert ">S01E03-E05</textarea>" in page
    assert '<option value="episodes" selected>' in page
    assert '<option value="once" selected>' in page
    assert load_state()["topics"] == []


def test_the_add_form_is_closed_and_empty_normally():
    page = TestClient(app).get("/").text

    assert '<details class="add card" id="new">' in page
    assert "Не добавлено" not in page


def test_draft_values_are_escaped(monkeypatch):
    monkeypatch.setattr("tow.web.services.run_check", lambda **_kw: pytest.fail("a refused add runs no check"))
    client = TestClient(app, headers={"Origin": "http://127.0.0.1"})

    page = client.post(
        "/topics/add",
        data={"url": "https://unknown.example/1", "title": '"><script>alert(1)</script>', "save_path": r"M:\a"},
    ).text

    assert "<script>alert(1)</script>" not in page
    assert "&#34;&gt;&lt;script&gt;" in page or "&quot;&gt;&lt;script&gt;" in page


def _topic_state(**fields):
    save_state(
        {"topics": [{"id": "t1", "title": "Show", "url": "http://rutor.info/torrent/1/x", **fields}], "mirrors": {}}
    )


def test_pause_and_resume_say_which_and_missing_topics_are_named():
    # D3: one "пауза" flash for both directions, and "success" for a topic that is gone.
    _topic_state()
    client = TestClient(app, headers={"Origin": "http://127.0.0.1"})

    paused = shown(client.post("/topics/t1/pause", follow_redirects=False).headers["location"])
    resumed = shown(client.post("/topics/t1/pause", follow_redirects=False).headers["location"])
    missing = shown(client.post("/topics/nope/pause", follow_redirects=False).headers["location"])
    deleted_missing = shown(client.post("/topics/nope/delete", follow_redirects=False).headers["location"])

    assert "раздача на паузе" in paused
    assert "раздача снята с паузы" in resumed
    assert "раздача не найдена" in missing
    assert "раздача не найдена; ничего не удалено" in deleted_missing
    assert load_state()["topics"][0]["paused"] is False


def test_manual_check_of_a_missing_topic_says_so(monkeypatch):
    monkeypatch.setattr("tow.web.services.run_check", lambda **_kw: {"qbit": "ok", "results": []})
    client = TestClient(app, headers={"Origin": "http://127.0.0.1"})

    location = shown(client.post("/topics/nope/check", follow_redirects=False).headers["location"])

    assert "раздача не найдена" in location


def test_clamped_interval_is_reported_not_silent():
    client = TestClient(app, headers={"Origin": "http://127.0.0.1"})

    location = shown(
        client.post(
            "/settings/interval", data={"interval_min": "5", "flash_ttl_min": "1"}, follow_redirects=False
        ).headers["location"]
    )

    assert "интервал 15 мин (допустимо 15–1440)" in location


@pytest.mark.parametrize("value", ["abc", "1.5", "-"])
def test_an_interval_that_is_not_a_whole_number_is_refused(value):
    from tow.config import interval_sec_of, load_config
    from tow.i18n import t

    before = interval_sec_of(load_config())
    client = TestClient(app, headers={"Origin": "http://127.0.0.1"})

    response = client.post(
        "/settings/interval", data={"interval_min": value, "flash_ttl_min": "1"}, follow_redirects=False
    )

    assert t("web.settings.bad_minutes", "ru") in shown(response.headers["location"])
    assert interval_sec_of(load_config()) == before


@pytest.mark.parametrize("field", ["interval_min", "flash_ttl_min"])
@pytest.mark.parametrize("value", ["", "   ", "٦٠", "６０", "1_000"])
def test_an_empty_or_unusual_number_in_checks_is_refused_not_taken_as_a_default(field, value):
    from tow.config import interval_sec_of, load_config
    from tow.i18n import t

    client = TestClient(app, headers={"Origin": "http://127.0.0.1"})
    client.post("/settings/interval", data={"interval_min": "30", "flash_ttl_min": "5"})
    data = {"interval_min": "30", "flash_ttl_min": "5", field: value}

    response = client.post("/settings/interval", data=data, follow_redirects=False)

    assert t("web.settings.bad_minutes", "ru") in shown(response.headers["location"])
    cfg = load_config()
    assert (interval_sec_of(cfg), cfg["flash_ttl_sec"]) == (1800, 300)


@pytest.mark.parametrize(
    ("hand_written", "posted", "expected"),
    [
        # The Undo time changed: an interval the form cannot show (10 min) is kept, not clamped.
        ({"interval_sec": 600, "flash_ttl_sec": 60}, {"interval_min": "10", "flash_ttl_min": "5"}, (600, 300)),
        # The interval changed: 45 seconds of Undo (shown as 1 min) stay 45 seconds.
        ({"interval_sec": 3600, "flash_ttl_sec": 45}, {"interval_min": "30", "flash_ttl_min": "1"}, (1800, 45)),
    ],
)
def test_a_checks_field_sent_back_unchanged_keeps_its_value(hand_written, posted, expected):
    from tow.config import load_config, save_config

    cfg = load_config()
    cfg.update(hand_written)
    save_config(cfg)
    client = TestClient(app, headers={"Origin": "http://127.0.0.1"})

    response = client.post("/settings/interval", data=posted, follow_redirects=False)

    cfg = load_config()
    assert (cfg["interval_sec"], cfg["flash_ttl_sec"]) == expected
    assert "допустимо" not in shown(response.headers["location"])  # nothing was clamped


def test_a_value_clamped_to_the_current_one_says_so():
    client = TestClient(app, headers={"Origin": "http://127.0.0.1"})
    client.post("/settings/interval", data={"interval_min": "1440", "flash_ttl_min": "1"})

    location = shown(
        client.post(
            "/settings/interval", data={"interval_min": "5000", "flash_ttl_min": "1"}, follow_redirects=False
        ).headers["location"]
    )

    assert "без изменений" in location
    assert "интервал 1440 мин (допустимо 15–1440)" in location


def test_undo_hint_only_on_the_page_right_after_the_action():
    from tow.web.templating import undo_just_made

    _topic_state()
    state = load_state()
    state["undo"] = {"kind": "topic_add", "id": "t1", "ts": iso_now()}
    save_state(state)
    assert undo_just_made() is True

    from datetime import UTC, datetime, timedelta

    from tow.undo import undo_just_made as just_made

    state["undo"]["ts"] = (datetime.now(UTC) - timedelta(seconds=40)).isoformat()  # 40 s after the action
    assert just_made(state) is False
    save_state(state)
    assert undo_just_made() is False


def test_site_probe_reports_how_many_mirrors_answer(monkeypatch):
    from tow.config import load_config

    name = next(iter(load_config()["trackers"]))
    monkeypatch.setattr(
        "tow.web.services.doctor_report",
        lambda **_kw: {"probes": [{"tracker": name, "ok": True}, {"tracker": name, "ok": False}]},
    )
    client = TestClient(app, headers={"Origin": "http://127.0.0.1"})

    location = shown(client.post(f"/sites/{name}/probe", follow_redirects=False).headers["location"])

    assert f"{name}: отвечающих зеркал — 1 из 2" in location


def test_rows_and_the_edit_panel_show_the_error_in_words():
    # D4: an error was only a dot colour plus a tooltip.
    long_error = (
        "previous torrent revision is still active on an overlapping file: Show.S01E01.mkv"
        " — остановите прежнюю раздачу в клиенте"
    )
    _topic_state(last_ok=False, last_error=long_error, last_error_class="qbit", last_check="01.10.2026 11:56")

    page = TestClient(app).get("/").text + TestClient(app).get("/topics/t1/edit-panel").text  # M2

    assert 'class="row-error bad"' in page
    assert '>Торрент-клиент: остановите прежнюю раздачу в клиенте<span class="sr-only">' in page
    assert "Последняя ошибка (01.10.2026 11:56): previous torrent revision" in page


def test_error_line_is_short_and_falls_back_to_the_message():
    from tow.web.views import _error_line

    assert _error_line("nnmclub: all hosts failed: http 503", "tracker") == "Сайт недоступен: nnmclub: перегружен (503)"
    assert _error_line("x" * 200, "error").endswith("…")
    assert len(_error_line("x" * 200, "error")) < 110
    assert _error_line("", "error") == ""


def test_a_removed_topic_gives_one_reason_not_a_transport_error():
    from tow.web.views import _error_line

    line = _error_line("example: no mirror answered: the site answered with error 404", "gone")

    assert line == "Раздача удалена с сайта"


def _row_summaries(page: str) -> list[str]:
    return re.findall(r'<details class="row-edit[^"]*"[^>]*>\s*<summary>(.*?)</summary>', page, flags=re.DOTALL)


def test_row_buttons_are_not_inside_the_clickable_summary():
    # D5: a link and two forms sat inside <summary> (invalid; read as one big button).
    _topic_state()
    page = TestClient(app).get("/").text

    summaries = _row_summaries(page)
    assert summaries
    for summary in summaries:
        assert "<form" not in summary
        assert 'class="row-ops"' not in summary
    assert '<div class="row-wrap"' in page
    assert 'aria-label="Сайт раздачи «Show»: ' in page


def test_site_rows_have_no_controls_inside_the_summary():
    # M6: the mirror buttons were forms inside <summary> too; the choice is now in the panel.
    page = TestClient(app).get("/sites").text

    summaries = _row_summaries(page)
    assert summaries
    for summary in summaries:
        assert "<form" not in summary
        assert "<button" not in summary
        assert "/freeze" not in summary
        assert "/probe" not in summary
    assert 'action="/sites/rutor/prefer" class="mirror-pick"' in page


def test_home_list_tools_and_row_data_for_search():
    # E1: chips, sort and the per-row data the client-side search and sort use.
    _topic_state(paused=True)
    page = TestClient(app).get("/").text

    assert 'id="list-tools" hidden' in page  # shown by app.js (progressive enhancement)
    for filter_name in ('data-filter="problem"', 'data-filter="new"', 'data-filter="paused"'):
        assert filter_name in page
    assert '<select id="list-sort">' in page
    assert 'data-tracker="' in page
    assert 'data-name="show"' in page
    assert "data-hl>Show</span>" in page


def test_search_script_folds_yo_and_matches_all_words():
    app_js = TestClient(app).get("/static/app.js").text

    assert 'replaceAll("ё", "е")' in app_js
    assert "tokens.every((token) => haystack.includes(token))" in app_js
    assert "const haystacks = new Map(searchable.map((el) => [el, fold(el.dataset.q)]));" in app_js
    assert 'event.key !== "/"' in app_js


def test_a_complete_season_offers_to_stop_watching():
    # G5: "Сезон собран N/N" with a one-click stop in the edit panel.
    _topic_state()
    items = {
        f"e{n}": {"episode_key": f"episode:s01e{n:02d}", "status": "completed", "label": f"S01E{n:02d}"} for n in (1, 2)
    }
    expected = {"kind": "episodes", "total": 2, "source": "selection", "confidence": "exact"}
    save_download_history({"schema_version": 1, "topics": {"t1": {"items": items, "expected": expected}}})

    page = TestClient(app).get("/").text + TestClient(app).get("/topics/t1/edit-panel").text  # M2

    assert "Сезон собран 2/2." in page
    assert 'form="stop-t1">Перестать следить</button>' in page


def test_attention_banner_lists_what_needs_the_owner(monkeypatch):
    # G3: a stale schedule, a dead qBit and sites waiting for a login, in one place.
    import time as _time

    state = {
        "topics": [
            {
                "id": "a",
                "title": "A",
                "url": "https://nnmclub.to/forum/viewtopic.php?t=1",
                "last_error_class": "tracker_auth",
                "last_error": "x",
            },
            {
                "id": "b",
                "title": "B",
                "url": "https://nnmclub.to/forum/viewtopic.php?t=2",
                "last_error_class": "tracker_auth",
                "last_error": "x",
            },
            {
                "id": "c",
                "title": "C",
                "url": "https://nnmclub.to/forum/viewtopic.php?t=3",
                "last_error_class": "tracker_auth",
                "paused": True,
            },
        ],
        "mirrors": {},
        "health": {"auto_at_ts": int(_time.time()) - 10 * 24 * 3600, "qbit_ok": False},
    }
    save_state(state)

    page = TestClient(app).get("/").text

    assert "Требует внимания" in page
    assert "Плановые проверки не выполнялись с" in page
    assert "Торрент-клиент недоступен" in page
    assert "Нужен вход на сайт nnmclub (раздач: 2)" in page


def test_no_banner_when_all_is_well():
    import time as _time

    save_state({"topics": [], "mirrors": {}, "health": {"auto_at_ts": int(_time.time()), "qbit_ok": True}})

    assert "Требует внимания" not in TestClient(app).get("/").text


def test_a_successful_check_records_when_it_last_worked(monkeypatch):
    from types import SimpleNamespace

    from tow import check
    from tow.clients import factory as client_factory

    # A later health write must not make the topic depend on the wall-clock second.
    success_at = "01.01.2026 10:00:00 UTC"
    stamps = iter([success_at, "01.01.2026 10:00:01 UTC"])
    monkeypatch.setattr(check_rows, "now", lambda: next(stamps))
    _topic_state(save_path=r"M:\TV", hash="H")
    monkeypatch.setattr(
        check_run,
        "check_topic",
        lambda topic, run: (check_rows.stamp_result(topic, {"ok": True}), {"id": topic["id"], "ok": True})[1],
    )
    monkeypatch.setattr(client_factory, "from_secrets", lambda *a, **k: SimpleNamespace(ping=lambda: "ok"))
    monkeypatch.setattr(check_reconcile, "reconcile_topic", lambda *a, **k: {"events": []})

    result = check.run_check(apply=True, notify=False, how="test")

    topic = load_state()["topics"][0]
    assert result["results"][0]["ok"] is True
    assert topic["last_ok"] is True
    assert not topic["last_error"]
    assert topic["last_ok_at"] == topic["last_check"] == success_at


@pytest.mark.parametrize("status", ["failed", "skipped"])
def test_a_failed_or_skipped_check_keeps_the_previous_success_time(monkeypatch, status):
    topic = {"last_ok_at": "previous success"}
    monkeypatch.setattr(check_rows, "now", lambda: "current attempt")
    check_rows.stamp_result(topic, {"ok": status == "skipped", "status": status})
    assert topic["last_check"] == "current attempt"
    assert topic["last_ok_at"] == "previous success"


def test_edit_panel_links_to_tracker_search_and_the_next_season():
    # E2: links only (no scraping), from the site's active mirror.
    save_state(
        {
            "topics": [
                {
                    "id": "t1",
                    "title": "Сериал А / Show A [S02E01-12 из 12]",
                    "url": "http://rutor.info/torrent/1/show-a",
                }
            ],
            "mirrors": {"rutor": {"active": "http://rutor.is"}},
        }
    )
    items = {f"e{n}": {"episode_key": f"episode:s02e{n:02d}", "status": "completed"} for n in range(1, 13)}
    expected = {"kind": "episodes", "total": 12, "source": "title", "confidence": "exact"}
    save_download_history({"schema_version": 1, "topics": {"t1": {"items": items, "expected": expected}}})

    page = TestClient(app).get("/").text + TestClient(app).get("/topics/t1/edit-panel").text  # M2

    assert 'href="http://rutor.is/search/0/0/100/0/%D0%A1%D0%B5%D1%80%D0%B8%D0%B0%D0%BB%20%D0%90"' in page
    assert ">Найти на Rutor</a>" in page  # the site as the pages name it, not its key
    assert 'href="http://rutor.is/search/0/0/100/0/%D0%A1%D0%B5%D1%80%D0%B8%D0%B0%D0%BB%20%D0%90%203"' in page
    assert ">Следующий сезон</a>" in page


def test_no_search_link_for_an_unknown_tracker_without_search_path():
    from types import SimpleNamespace

    from tow.web.views import _search_href

    plain = SimpleNamespace(name="custom", spec={"fetch_hosts": ["https://custom.example"]})
    assert _search_href(plain, {}, "Show") == ""
    with_path = SimpleNamespace(
        name="custom", spec={"fetch_hosts": ["https://custom.example"], "search_path": "/find?q={q}"}
    )
    assert _search_href(with_path, {}, "Show 2") == "https://custom.example/find?q=Show%202"


class _RevisionClient:
    def __init__(self, tags=("tow",)):
        self.tags = list(tags)
        self.stopped = []

    def has_hash(self, h):
        return h in {"OLD", "OLDER"}

    def stop_owned_torrent(self, h):
        if "tow" not in self.tags:
            raise RuntimeError("torrent was not added by TOW; stop it in the client yourself")
        self.stopped.append(h)
        return {"hash": h, "state": "stoppedUP"}


def _blocked_topic():
    _topic_state(
        hash="OLD",
        previous_hashes=["OLDER"],
        last_error="previous torrent revision is still active on an overlapping file: S01E01.mkv",
    )


def test_owner_can_stop_the_previous_revision_and_add_the_new_one(monkeypatch):
    # G1: one click instead of stopping the seeding revision by hand in qBittorrent.
    _blocked_topic()
    client = _RevisionClient()
    monkeypatch.setattr("tow.web.services.client_from_secrets", lambda *a, **k: client)
    checks = []
    monkeypatch.setattr(
        "tow.web.services.run_check",
        lambda **kw: checks.append(kw["ids"]) or {"qbit": "ok", "results": [{"id": "t1", "ok": True, "added": True}]},
    )
    page = TestClient(app).get("/").text + TestClient(app).get("/topics/t1/edit-panel").text  # M2
    assert "Остановить прежний и добавить" in page

    response = TestClient(app, headers={"Origin": "http://127.0.0.1"}).post(
        "/topics/t1/replace-revision", follow_redirects=False
    )

    assert client.stopped == ["OLD", "OLDER"]
    assert checks == [["t1"]]
    assert "добавлено" in shown(response.headers["location"])


def test_a_revision_tow_did_not_add_is_never_stopped(monkeypatch):
    _blocked_topic()
    client = _RevisionClient(tags=())
    monkeypatch.setattr("tow.web.services.client_from_secrets", lambda *a, **k: client)
    monkeypatch.setattr("tow.web.services.run_check", lambda **kw: (_ for _ in ()).throw(AssertionError("no check")))

    response = TestClient(app, headers={"Origin": "http://127.0.0.1"}).post(
        "/topics/t1/replace-revision", follow_redirects=False
    )

    assert client.stopped == []
    assert "не добавлена TOW" in shown(response.headers["location"]) or "not added by TOW" in shown(
        response.headers["location"]
    )


def test_a_paused_topic_does_not_stop_its_previous_revision(monkeypatch):
    _topic_state(
        hash="OLD",
        previous_hashes=["OLDER"],
        paused=True,
        last_error="previous torrent revision is still active on an overlapping file: S01E01.mkv",
    )
    client = _RevisionClient()
    monkeypatch.setattr("tow.web.services.client_from_secrets", lambda *a, **k: client)
    monkeypatch.setattr("tow.web.services.run_check", lambda **kw: (_ for _ in ()).throw(AssertionError("no check")))

    response = TestClient(app, headers={"Origin": "http://127.0.0.1"}).post(
        "/topics/t1/replace-revision", follow_redirects=False
    )

    assert client.stopped == []  # the check would skip it: nothing would replace the stopped one
    assert "раздача на паузе: сначала снимите её с паузы" in shown(response.headers["location"])


def test_no_stop_button_without_an_overlap_error():
    _topic_state(hash="OLD", last_error="nnmclub: all hosts failed")

    assert "Остановить прежний и добавить" not in TestClient(app).get("/").text
    location = (
        TestClient(app, headers={"Origin": "http://127.0.0.1"})
        .post("/topics/t1/replace-revision", follow_redirects=False)
        .headers["location"]
    )
    assert "останавливать не нужно" in shown(location)


def test_qbit_adapter_stops_only_tow_torrents():
    from tow.clients.qbittorrent import QBittorrentClient

    class Api:
        def __init__(self, tags, state="uploading"):
            self.tags, self.state, self.stop_calls = tags, state, []

        def torrents_info(self, torrent_hashes=None, **_kw):
            from types import SimpleNamespace

            return [SimpleNamespace(hash=torrent_hashes, tags=self.tags, state=self.state, save_path="M:\\", name="x")]

        def torrents_files(self, torrent_hash=None, **_kw):
            return []

        def torrents_stop(self, torrent_hashes):
            self.stop_calls.append(torrent_hashes)
            self.state = "stoppedUP"

    owned = QBittorrentClient.__new__(QBittorrentClient)
    owned._c = Api("tow")
    assert owned.stop_owned_torrent("ABC")["state"] == "stoppedUP"
    assert owned._c.stop_calls == ["abc"]

    foreign = QBittorrentClient.__new__(QBittorrentClient)
    foreign._c = Api("someone-else")
    with pytest.raises(RuntimeError) as refused:
        foreign.stop_owned_torrent("ABC")
    assert refused.value.code == "client.managed.not_owned_stop"
    assert foreign._c.stop_calls == []


def test_history_reads_rotated_logs_and_filters_by_group_and_text():
    # G7: the log window showed only the last 200 events of the current file.
    from tow.log import log_path

    path = log_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    old = {"kind": "file_completed", "title": "Старый сериал", "created_at": "2026-09-01T10:00:00+00:00"}
    new = {"kind": "check_fail", "title": "Новый сериал", "error": "boom", "created_at": "2026-10-01T10:00:00+00:00"}
    noise = {"kind": "tracker_checked", "title": "шум", "created_at": "2026-10-01T10:00:00+00:00"}
    path.with_name("tow.jsonl.1").write_text(json.dumps(old, ensure_ascii=False) + "\n", encoding="utf-8")
    path.write_text("\n".join(json.dumps(r, ensure_ascii=False) for r in (noise, new)) + "\n", encoding="utf-8")
    client = TestClient(app)

    everything = client.get("/history").text
    downloads = client.get("/history?group=downloads").text
    searched = client.get("/history?q=новый").text

    assert "Новый сериал" in everything
    assert "Старый сериал" in everything  # from the rotated file
    assert "шум" not in everything  # bookkeeping events stay out
    assert "Старый сериал" in downloads
    assert "Новый сериал" not in downloads
    assert "Новый сериал" in searched
    assert "Старый сериал" not in searched


def test_history_and_log_window_name_events_recorded_with_only_topic_identity():
    save_state(
        {
            "topics": [{"id": "topic-a", "title": "Понятный сериал", "hash": "A" * 40}],
            "mirrors": {},
        }
    )
    log_event("file_completed", topic="topic-a", hash="A" * 40)
    client = TestClient(app)

    assert "Понятный сериал" in client.get("/history?group=downloads").text
    assert "Понятный сериал" in client.get("/history?q=понятный").text
    assert "Понятный сериал" in client.get("/log.json").json()["rows"][0]["detail"]
    assert "Понятный сериал" in client.get("/settings").text


def test_log_window_links_to_the_full_history():
    assert 'href="/history"' in TestClient(app).get("/").text
