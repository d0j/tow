"""Settings: adding, choosing and removing torrent clients (with undo)."""

from __future__ import annotations

import httpx
import pytest
from fastapi.testclient import TestClient
from helpers import add_unready_client, flash_of

from tow.config import load_config
from tow.store import load_secrets, load_state, save_secrets, save_state


def _client() -> TestClient:
    from tow.web import app

    return TestClient(app, headers={"Origin": "http://127.0.0.1"})


def _flash(response: httpx.Response) -> str:
    return flash_of(response.headers["location"])


@pytest.fixture
def legacy_qbit():
    save_secrets({"qbittorrent": {"host": "127.0.0.1", "port": 8080, "username": "admin", "password": "pw"}})


def test_adding_a_client_converts_the_single_client_config(legacy_qbit):
    c = _client()
    response = c.post("/settings/client/add", data={"kind": "transmission"}, follow_redirects=False)
    assert _flash(response) == "Transmission добавлен — заполните подключение ниже и нажмите «Сохранить»"
    cfg = load_config()
    assert cfg["clients"] == [
        {"kind": "qbittorrent", "id": "default", "default": True},
        {"id": "transmission", "kind": "transmission", "title": "Transmission"},
    ]
    secrets = load_secrets()
    assert secrets["clients"]["default"] == secrets["qbittorrent"]  # the saved login came along

    page = c.get("/settings").text
    assert 'id="client-transmission"' in page
    assert "Включите «Разрешить удалённое управление»" in page
    assert '<span class="pill ok">Основной</span>' in page

    saved = c.post(
        "/settings/client",
        data={"client_id": "transmission", "kind": "transmission", "host": "nas", "port": "9091", "password": "x"},
        follow_redirects=False,
    )
    assert _flash(saved) == "сохранено"
    assert load_secrets()["clients"]["transmission"] == {"host": "nas", "port": 9091, "username": "", "password": "x"}

    from tow.clients.factory import from_secrets
    from tow.clients.qbittorrent import QBittorrentClient
    from tow.clients.transmission import TransmissionClient

    assert isinstance(from_secrets(load_config(), load_secrets()), QBittorrentClient)
    assert isinstance(from_secrets(load_config(), load_secrets(), "transmission"), TransmissionClient)


def test_second_client_of_a_kind_gets_its_own_id(legacy_qbit):
    c = _client()
    c.post("/settings/client/add", data={"kind": "deluge"})
    c.post("/settings/client/add", data={"kind": "deluge"})
    assert [row["id"] for row in load_config()["clients"]] == ["default", "deluge", "deluge-2"]


def test_unknown_or_unready_kind_is_refused(legacy_qbit, monkeypatch):
    add_unready_client(monkeypatch)
    for kind in ("draft", "icq", ""):
        response = _client().post("/settings/client/add", data={"kind": kind}, follow_redirects=False)
        assert _flash(response) == "нет такого клиента"
    assert "clients" not in load_config()


def test_default_and_remove(legacy_qbit):
    c = _client()
    c.post("/settings/client/add", data={"kind": "deluge"})
    refused = c.post("/settings/client/remove", data={"client_id": "default"}, follow_redirects=False)
    assert _flash(refused) == "это основной клиент — сначала сделайте основным другой"

    made = c.post("/settings/client/default", data={"client_id": "deluge"}, follow_redirects=False)
    assert _flash(made) == "основной клиент выбран: в него TOW добавляет новые раздачи"
    from tow.clients.factory import default_client_id

    assert default_client_id(load_config()) == "deluge"

    state = load_state()
    state["topics"] = [{"id": "t1", "client_id": "default"}, {"id": "t2"}]
    save_state(state)
    busy = c.post("/settings/client/remove", data={"client_id": "default"}, follow_redirects=False)
    assert _flash(busy) == "клиент выбран у раздач: 1 — сначала выберите им другой клиент"

    state["topics"] = [{"id": "t2"}]  # follows the default client, now deluge
    save_state(state)
    removed = c.post("/settings/client/remove", data={"client_id": "default"}, follow_redirects=False)
    assert _flash(removed) == "клиент удалён"
    assert [row["id"] for row in load_config()["clients"]] == ["deluge"]
    assert "default" not in load_secrets().get("clients", {})

    only = c.post("/settings/client/remove", data={"client_id": "deluge"}, follow_redirects=False)
    assert _flash(only) == "это единственный клиент — его можно только перенастроить"


def test_undo_puts_clients_back(legacy_qbit):
    c = _client()
    before_cfg = load_config()
    before_secrets = load_secrets()
    c.post("/settings/client/add", data={"kind": "transmission"})
    response = c.post("/undo", follow_redirects=False)
    assert _flash(response) == "клиенты возвращены"
    after = load_config()
    assert "clients" not in after
    assert after["client"] == before_cfg["client"]
    assert load_secrets() == before_secrets


def test_check_button_shows_the_version_or_the_exact_problem(legacy_qbit, monkeypatch):
    class Alive:
        def ping(self):
            return "5.0.1 webapi 2.11"

    class Broken:
        def ping(self):
            from tow.clients.managed import ClientError

            raise ClientError("Transmission: неверный логин или пароль")  # what the adapter raises

    monkeypatch.setattr("tow.clients.factory.from_secrets", lambda *_a: Alive())
    ok = _client().post("/settings/client/ping", data={"client_id": ""}, follow_redirects=False)
    assert _flash(ok) == "связь есть: 5.0.1 webapi 2.11"
    monkeypatch.setattr("tow.clients.factory.from_secrets", lambda *_a: Broken())
    bad = _client().post("/settings/client/ping", data={"client_id": ""}, follow_redirects=False)
    assert _flash(bad) == "нет связи: Transmission: неверный логин или пароль"


def test_deluge_card_has_no_login_field(legacy_qbit):
    c = _client()
    c.post("/settings/client/add", data={"kind": "deluge"})
    page = c.get("/settings").text
    card = page[page.index('id="client-deluge"') :]
    card = card[: card.index("</article>")]
    assert 'name="username"' not in card
    assert "Пароль Deluge Web" in card
    assert "TOW сам включит в Deluge стандартный модуль Label" in card


@pytest.mark.parametrize(
    ("host", "port", "key"),
    [
        ("nas", "99999", "web.settings.bad_port"),
        ("nas", "0", "web.settings.bad_port"),
        ("nas", "80a", "web.settings.bad_port"),
        ("../../x", "8080", "web.settings.bad_client_host"),
        ("<b>x</b>", "8080", "web.settings.bad_client_host"),
        ("my nas", "8080", "web.settings.bad_client_host"),
        ("http://nas/../admin", "8080", "web.settings.bad_client_host"),
        ("ftp://nas", "8080", "web.settings.bad_client_host"),
    ],
)
def test_an_invalid_client_address_or_port_is_never_stored(legacy_qbit, host, port, key):
    from tow.i18n import t

    before = load_secrets()
    response = _client().post("/settings/client", data={"host": host, "port": port}, follow_redirects=False)

    assert _flash(response) == t(key, "ru")
    assert load_secrets() == before


@pytest.mark.parametrize(
    "host", ["nas", "192.168.1.10", "[fd00::1]", "http://nas.lan:8080/qbt/", "https://сервер.example"]
)
def test_a_client_address_may_be_a_name_an_ip_or_a_url(legacy_qbit, host):
    response = _client().post(
        "/settings/client",
        data={"host": host, "port": "8080", "username": "admin", "password": "pw"},
        follow_redirects=False,
    )

    assert _flash(response) == "сохранено"
    assert load_secrets()["qbittorrent"]["host"] == host
