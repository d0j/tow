"""Local, transactional optional-module settings; rendering never tests external services."""

from __future__ import annotations

import re

import pytest
from bs4 import BeautifulSoup
from fastapi.testclient import TestClient
from helpers import flash_kind, open_network

from tow.config import load_config, save_config
from tow.paths import config_path, state_path
from tow.store import load_state, save_state
from tow.web import app, services


def client(**kwargs):
    return TestClient(app, headers={"Origin": "http://127.0.0.1"}, **kwargs)


def form(**changes):
    fields = {
        "enabled": "1",
        "timeout_sec": "30",
        "min_interval_sec": "1800",
        "max_attempts": "2",
        "provider_id": ["first", "second"],
        "provider_kind": ["flaresolverr", "byparr"],
        "provider_url": ["http://127.0.0.1:8191", "http://127.0.0.1:8192"],
        "provider_order": ["1", "2"],
        "provider_enabled": ["first", "second"],
    }
    fields.update(changes)
    return fields


def card(page):
    return page.split('id="acc-protection"', 1)[1].split('id="acc-log"', 1)[0]


def test_default_card_closed_off_and_no_contacts(monkeypatch):
    monkeypatch.setattr("tow.protection.chain.candidates", lambda *a, **kw: pytest.fail("network requested"))
    page = client().get("/settings").text
    opening = re.search(r'<details[^>]+id="acc-protection"[^>]*>', page).group()
    assert " open" not in opening
    section = card(page)
    assert re.search(r'<details[^>]+id="protection-guide"[^>]* open>', section)  # nothing saved yet: the steps
    assert 'name="enabled" value="1" checked' not in section
    assert 'class="pill mut">Выключено' in section
    assert 'name="provider_kind"' in section
    assert '<option value="flaresolverr">' in section
    assert '<option value="byparr">' in section
    assert section.count('class="integration-card protection-card"') == 1
    assert "textarea" not in section
    assert 'style="' not in section


def test_save_order_enable_and_remove():
    response = client().post("/settings/protection", data=form(provider_order=["2", "1"]), follow_redirects=False)
    assert response.status_code == 303
    assert flash_kind(response.headers["location"]) == "ok"
    configured = load_config()["protection"]
    assert [item["id"] for item in configured["providers"]] == ["second", "first"]
    assert configured["providers"][0]["url"] == "http://127.0.0.1:8192/v1"
    response = client().post("/settings/protection", data=form(provider_remove=["first"]), follow_redirects=False)
    assert flash_kind(response.headers["location"]) == "ok"
    assert [item["id"] for item in load_config()["protection"]["providers"]] == ["second"]


def test_blank_disabled_cards_are_not_saved():
    response = client().post(
        "/settings/protection",
        data=form(enabled="", provider_url=["", ""], provider_enabled=[]),
        follow_redirects=False,
    )
    assert flash_kind(response.headers["location"]) == "ok"
    assert services.validate_protection_settings(load_config().get("protection"))["providers"] == []


@pytest.mark.parametrize(
    "changes",
    [
        {"provider_url": ["https://public.example", "http://127.0.0.1:8192"]},
        {"provider_order": ["1", "1"]},
        {"provider_order": ["x", "2"]},
        {"provider_id": ["first"]},
        {"provider_enabled": ["unlisted"]},
        {"provider_remove": ["unlisted"]},
        {"timeout_sec": "4"},
        {"timeout_sec": "30.5"},
        {"min_interval_sec": "0"},
        {"max_attempts": "5"},
        {"enabled": "true"},
        {"provider_enabled": [], "enabled": "1"},
        {"provider_url": ["", ""], "provider_enabled": ["first"]},
    ],
)
def test_invalid_form_preserves_configuration(changes):
    before = config_path().read_bytes()
    response = client().post("/settings/protection", data=form(**changes), follow_redirects=False)
    assert response.status_code == 303
    assert flash_kind(response.headers["location"]) == "err"
    assert config_path().read_bytes() == before


def test_json_payload_not_applied():
    before = config_path().read_bytes()
    response = client().post("/settings/protection", json=form(), follow_redirects=False)
    assert response.status_code == 303
    assert flash_kind(response.headers["location"]) == "err"
    assert config_path().read_bytes() == before


@pytest.mark.parametrize("origin", [None, "http://evil.example"])
def test_csrf_refused_before_save(origin):
    before = config_path().read_bytes()
    c = TestClient(app)
    headers = {"Origin": origin} if origin else {}
    response = c.post("/settings/protection", data=form(), headers=headers, follow_redirects=False)
    assert response.status_code == 403
    assert config_path().read_bytes() == before


@pytest.mark.parametrize("path", ["/settings/protection", "/settings/protection/reset"])
def test_authenticated_network_cannot_change_service(monkeypatch, path):
    from tow.auth import SESSION_COOKIE, issue_session, lan_password_record, lan_password_session_key
    from tow.store import save_secrets

    open_network(monkeypatch)
    record = lan_password_record("synthetic-password-long-enough")
    save_secrets({"lan_auth": record})
    before = config_path().read_bytes()
    c = TestClient(
        app,
        base_url="http://192.168.1.2",
        client=("192.168.1.3", 50000),
        cookies={SESSION_COOKIE: issue_session(lan_password_session_key(record))},
    )
    response = c.post(
        path,
        data=form(),
        headers={"Origin": "http://192.168.1.2"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert flash_kind(response.headers["location"]) == "err"
    assert config_path().read_bytes() == before


def test_existing_undo_invalidated_in_same_transaction():
    state = load_state()
    state["undo"] = {"kind": "topic", "item": {"id": "synthetic"}, "index": 0, "ts": "2026-10-13T00:00:00Z"}
    save_state(state)
    response = client().post("/settings/protection", data=form(), follow_redirects=False)
    assert flash_kind(response.headers["location"]) == "ok"
    assert "undo" not in load_state()


def test_transaction_failure_does_not_claim_saved(monkeypatch):
    from tow import store_transaction

    before = config_path().read_bytes()

    def fail(self, cfg):
        raise OSError("synthetic write failure")

    monkeypatch.setattr(store_transaction.StoreTransaction, "save_config", fail)
    response = client().post("/settings/protection", data=form(), follow_redirects=False)
    assert response.status_code == 303
    assert flash_kind(response.headers["location"]) == "err"
    assert config_path().read_bytes() == before


def test_unavailable_module_retained_and_rendered_without_success_claim():
    configured = services.validate_protection_settings(
        {
            "enabled": True,
            "providers": [{"id": "future", "kind": "future_provider", "url": "http://127.0.0.1:8193", "enabled": True}],
        }
    )
    cfg = load_config()
    cfg["protection"] = configured
    save_config(cfg)
    section = card(client().get("/settings?open=protection").text)
    assert 'class="pill mut">Включено' in section
    assert "Модуль недоступен" in section
    assert 'name="provider_kind" value="future_provider"' in section
    assert 'class="pill ok"' not in section


def test_more_instances_can_be_added_each_save():
    client().post("/settings/protection", data=form())
    section = card(client().get("/settings").text)
    assert section.count('name="provider_kind" value="flaresolverr"') == 1
    assert section.count('name="provider_kind" value="byparr"') == 1
    assert '<option value="flaresolverr">' in section
    assert '<option value="byparr">' in section


@pytest.mark.parametrize("kind", ["flare_bypasser", "trawl"])
def test_new_service_is_saved_without_any_external_contact(monkeypatch, kind):
    monkeypatch.setattr("tow.protection.chain.candidates", lambda *a, **kw: pytest.fail("network requested"))
    response = client().post(
        "/settings/protection",
        data=form(
            enabled="",
            provider_id=["new-connection-1"],
            provider_kind=[kind],
            provider_url=["http://127.0.0.1:8193"],
            provider_order=["1"],
            provider_enabled=[],
        ),
        follow_redirects=False,
    )
    assert flash_kind(response.headers["location"]) == "ok"
    configured = load_config()["protection"]
    assert configured["enabled"] is False
    assert configured["providers"] == [
        {"id": "new-connection-1", "kind": kind, "url": "http://127.0.0.1:8193/v1", "enabled": False}
    ]


@pytest.mark.parametrize("status", ["unknown", "succeeded", "unavailable", "busy", "protocol"])
def test_service_observation_is_read_only_and_not_site_success(monkeypatch, status):
    from tow.paths import data_dir
    from tow.protection import store

    client().post("/settings/protection", data=form())
    configured = load_config()["protection"]
    monkeypatch.setattr(services.time, "time", lambda: 1000)
    if status != "unknown":
        store.note_service(configured["providers"][0], status, now=1000, retry_after=60)
    paths = [config_path(), state_path(), data_dir() / "protection.enc"]
    before = [p.read_bytes() if p.exists() else None for p in paths]
    view = services.protection_settings_view(load_config())
    assert view["cards"][0]["api_status"] == status
    section = card(client().get("/settings?open=protection").text)
    assert 'class="pill ok"' not in section
    if status == "succeeded":
        assert ">Сервис ответил</span>" in section  # what the service said, not that the site is reachable
    elif status in {"busy", "unavailable", "protocol"}:
        assert "Пауза сервиса: ещё 60 с" in section
    assert [p.read_bytes() if p.exists() else None for p in paths] == before


def test_corrupt_observation_does_not_break_render_or_claim_healthy():
    from tow.paths import data_dir

    client().post("/settings/protection", data=form())
    path = data_dir() / "protection.enc"
    path.write_bytes(b"synthetic-corrupt-data")
    section = card(client().get("/settings?open=protection").text)
    assert "не читается" in section
    assert 'class="pill ok"' not in section
    assert path.read_bytes() == b"synthetic-corrupt-data"


def test_full_connection_limit_has_no_unsaveable_extra_card():
    cfg = load_config()
    cfg["protection"] = services.validate_protection_settings(
        {
            "providers": [
                {"id": f"service-{i}", "kind": "trawl", "url": f"http://127.0.0.1:{8200 + i}/v1"} for i in range(16)
            ]
        }
    )
    save_config(cfg)
    section = card(client().get("/settings").text)
    assert section.count('class="integration-card protection-card"') == 16
    assert "Добавить подключение" not in section
    assert "максимальное число подключений: 16" in section


def test_render_does_not_change_provider_configuration():
    client().post("/settings/protection", data=form())
    before = {p.name: p.read_bytes() if p.exists() else None for p in (config_path(), state_path())}
    response = client().get("/settings?open=protection")
    assert response.status_code == 200
    assert {p.name: p.read_bytes() if p.exists() else None for p in (config_path(), state_path())} == before


@pytest.mark.parametrize("language", ["ru", "en"])
def test_site_access_help_is_open_and_control_hints_are_bound(monkeypatch, language):
    monkeypatch.setattr("tow.protection.chain.candidates", lambda *a, **kw: pytest.fail("network requested"))
    cfg = load_config()
    cfg["language"] = language
    save_config(cfg)
    client().post("/settings/protection", data=form())
    before = {p.name: p.read_bytes() if p.exists() else None for p in (config_path(), state_path())}
    page = BeautifulSoup(client().get("/settings?open=protection").text, "html.parser")
    section = page.find(id="acc-protection")
    guide = section.find(id="protection-guide")
    assert not guide.has_attr("open")  # open only until a connection is saved
    assert len(guide.find_all("li")) == 4
    assert guide.find("a")["href"] == "/settings/help#site-access"
    assert not section.find(class_="protection-limits").has_attr("open")
    fields = section.find_all(attrs={"aria-describedby": True})
    assert len(fields) == 8  # switch, the address of each card, three limits and reset action
    for control in fields:
        for ident in control["aria-describedby"].split():
            hints = page.find_all(id=ident)
            assert len(hints) == 1
            assert hints[0].get_text(strip=True)
    assert {p.name: p.read_bytes() if p.exists() else None for p in (config_path(), state_path())} == before


def test_installed_module_explanation_is_visible_without_starting_it(monkeypatch):
    client().post("/settings/protection", data=form())
    monkeypatch.setattr(services, "_local_service_managed", lambda provider: True)
    monkeypatch.setattr("tow.protection.local_service.ensure", lambda *a, **kw: pytest.fail("module started"))
    page = BeautifulSoup(client().get("/settings?open=protection").text, "html.parser")
    cards = page.select(".protection-card")
    assert len(cards) == 3
    for existing in cards[:2]:
        assert existing.has_attr("open")
        assert "Модуль уже установлен" in existing.get_text()
        assert "отдельный автозапуск не нужен" in existing.get_text()
    assert "Модуль уже установлен" not in cards[-1].get_text()


@pytest.mark.parametrize("language", ["ru", "en"])
def test_in_app_guide_has_site_access_anchor_and_back_link(language):
    cfg = load_config()
    cfg["language"] = language
    save_config(cfg)
    page = BeautifulSoup(client().get("/settings/help").text, "html.parser")
    assert page.find(id="site-access").name == "h2"
    assert page.find("a", href="/settings?open=protection#acc-protection") is not None


def test_network_view_explains_read_only_before_setup_and_keeps_controls_disabled(monkeypatch):
    from tow.auth import SESSION_COOKIE, issue_session, lan_password_record, lan_password_session_key
    from tow.store import save_secrets

    open_network(monkeypatch)
    record = lan_password_record("synthetic-password-long-enough")
    save_secrets({"lan_auth": record})
    remote = TestClient(
        app,
        base_url="http://192.168.1.2",
        client=("192.168.1.3", 50000),
        cookies={SESSION_COOKIE: issue_session(lan_password_session_key(record))},
    )
    response = remote.get("/settings?open=protection")
    assert response.status_code == 200
    section = BeautifulSoup(response.text, "html.parser").find(id="acc-protection")
    words = section.get_text()
    assert words.index("только для просмотра") < words.index("Как подключить")
    assert section.find("fieldset").has_attr("disabled")
    assert section.find(id="protection-reset-hint") is None


def test_cards_escape_unknown_plugin_titles(monkeypatch):
    monkeypatch.setattr(services, "protection_adapters", lambda: {"<script>alert(1)</script>": lambda *a: None})
    section = card(client().get("/settings").text)
    assert "<script>alert(1)</script>" not in section
    assert "&lt;script&gt;alert(1)&lt;/script&gt;" in section


def test_reset_corrupt_record_preserves_bytes_and_settings():
    from tow.paths import data_dir, tmp_dir

    path = data_dir() / "protection.enc"
    path.write_bytes(b"synthetic-corrupt-encrypted-data")
    before = config_path().read_bytes()
    response = client().post("/settings/protection/reset", follow_redirects=False)
    assert response.status_code == 303
    assert flash_kind(response.headers["location"]) == "ok"
    assert not path.exists()
    retained = list(tmp_dir().glob("protection-reset-*.enc"))
    assert len(retained) == 1
    assert retained[0].read_bytes() == b"synthetic-corrupt-encrypted-data"
    assert config_path().read_bytes() == before


def test_reset_running_check_refused_without_mutation():
    from tow.paths import data_dir
    from tow.storage.locks import check_run_lock

    path = data_dir() / "protection.enc"
    path.write_bytes(b"synthetic")
    with check_run_lock():
        response = client().post("/settings/protection/reset", follow_redirects=False)
    assert flash_kind(response.headers["location"]) == "err"
    assert path.read_bytes() == b"synthetic"


def test_reset_nonplain_record_refused():
    from tow.paths import data_dir

    path = data_dir() / "protection.enc"
    path.mkdir()
    response = client().post("/settings/protection/reset", follow_redirects=False)
    assert flash_kind(response.headers["location"]) == "err"
    assert path.is_dir()


def test_reset_linklike_record_refused(monkeypatch):
    from tow.paths import data_dir
    from tow.protection import store

    path = data_dir() / "protection.enc"
    path.write_bytes(b"synthetic")
    monkeypatch.setattr(store, "is_plain_file", lambda info: False)
    response = client().post("/settings/protection/reset", follow_redirects=False)
    assert flash_kind(response.headers["location"]) == "err"
    assert path.read_bytes() == b"synthetic"


def test_reset_nonplain_temporary_folder_refused():
    from tow.paths import data_dir, tmp_dir

    path = data_dir() / "protection.enc"
    path.write_bytes(b"synthetic")
    folder = tmp_dir(create=False)
    folder.write_bytes(b"not-a-folder")
    response = client().post("/settings/protection/reset", follow_redirects=False)
    assert flash_kind(response.headers["location"]) == "err"
    assert path.read_bytes() == b"synthetic"
    assert folder.read_bytes() == b"not-a-folder"


def test_reset_is_csrf_protected():
    response = TestClient(app).post("/settings/protection/reset", headers={"Origin": "http://evil.example"})
    assert response.status_code == 403


def test_reset_posix_exclusive_publication_preserves_old_record(monkeypatch):
    import os
    from types import SimpleNamespace

    from tow.paths import data_dir, tmp_dir
    from tow.protection import store

    path = data_dir() / "protection.enc"
    path.write_bytes(b"synthetic-encrypted-record")
    backend = SimpleNamespace(publish_exclusive=lambda source, destination: os.link(source, destination))
    monkeypatch.setattr(store, "current_platform", lambda: backend)
    assert store.reset() is True
    assert not path.exists()
    retained = list(tmp_dir().glob("protection-reset-*.enc"))
    assert len(retained) == 1
    assert retained[0].read_bytes() == b"synthetic-encrypted-record"


def test_reset_publish_failure_keeps_source(monkeypatch):
    from types import SimpleNamespace

    from tow.paths import data_dir
    from tow.protection import store

    path = data_dir() / "protection.enc"
    path.write_bytes(b"synthetic-encrypted-record")

    def fail(*args):
        raise PermissionError("synthetic publication failure")

    monkeypatch.setattr(store, "current_platform", lambda: SimpleNamespace(publish_exclusive=fail))
    assert services.reset_protection() == "refused"
    assert path.read_bytes() == b"synthetic-encrypted-record"


def test_reset_oversized_record_refused(monkeypatch):
    from tow.paths import data_dir
    from tow.protection import store

    path = data_dir() / "protection.enc"
    path.write_bytes(b"synthetic-record-over-test-bound")
    monkeypatch.setattr(store, "_MAX_BYTES", 4)
    assert services.reset_protection() == "refused"
    assert path.read_bytes() == b"synthetic-record-over-test-bound"
