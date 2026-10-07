"""The undo engine (tow.undo): every kind round-trips, a crash mid-undo leaves the stores as they
were, the encrypted secret snapshot never outlives its undo, and every kind is named in words.

Changes are made through the real routes against the isolated TOW_HOME/TOW_CONFIG of conftest;
a crash is staged with the store transaction's test seam (see test_store_transaction.py).
"""

from __future__ import annotations

import contextvars
from datetime import UTC, datetime, timedelta

import pytest
from fastapi.testclient import TestClient
from helpers import flash_kind, flash_of

from tow import i18n, store_transaction, undo
from tow.clock import iso_now
from tow.config import load_config, save_config
from tow.log import read_events
from tow.paths import config_path
from tow.store import (
    encrypted_secrets_path,
    load_secrets,
    load_state,
    persistence_lock,
    save_secret_undo,
    save_secrets,
    save_state,
    secret_undo_path,
    state_path,
)
from tow.web import app

ORIGIN = {"Origin": "http://127.0.0.1"}
RUTOR_URL = "http://rutor.info/torrent/101/show"
PASSWORD = "correct-horse-battery"


@pytest.fixture
def client():
    return TestClient(app, headers=ORIGIN)


def _topic(tid: str, n: int = 1, **extra) -> dict:
    return {
        "id": tid,
        "title": f"Show {tid}",
        "url": f"http://rutor.info/torrent/{n}/x",
        "save_path": r"M:\anime",
        "client_id": "default",
        "hash": None,
        **extra,
    }


def _contents() -> dict:
    """What the owner sees of the stores (the undo record itself aside)."""
    state = load_state()
    state.pop("undo", None)
    return {"config": load_config(), "secrets": load_secrets(), "state": state}


def _store_bytes() -> dict[str, bytes | None]:
    paths = (config_path(), state_path(), encrypted_secrets_path(), secret_undo_path())
    return {path.name: path.read_bytes() if path.is_file() else None for path in paths}


def _undo(client) -> str:
    response = client.post("/undo", follow_redirects=False)
    assert response.status_code == 303
    return flash_of(response.headers["location"])


def _events(kind: str) -> list[dict]:
    return [event for event in reversed(read_events(limit=200)) if event.get("kind") == kind]


# --------------------------------------------------------------------------- one change of each kind


def _seed_site() -> None:
    cfg = load_config()
    cfg["trackers"] = {"rutor": {"title": "rutor", "url_regex": "rutor", "fetch_hosts": ["http://rutor"]}}
    save_config(cfg)
    save_state({"topics": [], "mirrors": {"rutor": {"active": "http://rutor"}}})
    save_secrets({"trackers": {"rutor": {"username": "fixture-user", "password": "fixture-secret"}}})


def _set_password(client) -> None:
    client.post("/settings/password", data={"lan_password": PASSWORD, "lan_password2": PASSWORD})


def _change_topic_delete(client, monkeypatch):
    save_state({"topics": [_topic("t1", 1), _topic("t2", 2), _topic("t3", 3)], "mirrors": {}})
    return lambda: client.post("/topics/t2/delete")


def _change_topic_add(client, monkeypatch):
    monkeypatch.setattr("tow.title.guess_topic_title", lambda _url: "")
    monkeypatch.setattr("tow.web.services.run_check", lambda **_kw: {"results": []})
    save_state({"topics": [_topic("keep", 5)], "mirrors": {}, "recent_save_roots": [r"M:\a"]})
    return lambda: client.post("/topics/add", data={"url": RUTOR_URL, "title": "New", "save_path": r"M:\a"})


def _change_topic_edit(client, monkeypatch):
    save_state({"topics": [_topic("t1")], "mirrors": {}, "recent_save_roots": [r"M:\anime"]})
    form = {"title": "Renamed", "url": "", "save_path": r"M:\anime", "client_id": "default"}
    return lambda: client.post("/topics/t1/edit", data=form)


def _change_site(client, monkeypatch):
    _seed_site()
    return lambda: client.post("/sites/rutor/delete")


def _change_settings(client, monkeypatch):
    save_secrets({"qbittorrent": {"host": "127.0.0.1", "port": 8080, "username": "admin", "password": "pw"}})
    form = {"kind": "qbittorrent", "host": "nas", "port": "8081", "username": "admin", "password": "new-pw"}
    return lambda: client.post("/settings/client", data=form)


def _change_interval(client, monkeypatch):
    save_secrets({"telegram": {"token": "kept"}})
    return lambda: client.post("/settings/interval", data={"interval_min": "120", "flash_ttl_min": "2"})


def _change_access(client, monkeypatch):
    _set_password(client)
    return lambda: client.post("/settings/access", data={"allow_lan": "1"})


def _change_password(client, monkeypatch):
    return lambda: _set_password(client)


def _change_clients(client, monkeypatch):
    save_secrets({"qbittorrent": {"host": "127.0.0.1", "port": 8080, "username": "admin", "password": "pw"}})
    return lambda: client.post("/settings/client/add", data={"kind": "transmission"})


# kind -> (set up and return the change, the message of its undo, the part that must come back)
ROUND_TRIPS = {
    "topic": (_change_topic_delete, "удаление отменено: наблюдение снова в списке", None),
    "topic_add": (_change_topic_add, None, "topics"),
    "topic_put": (_change_topic_edit, "правка раздачи отменена", "titles"),
    "site": (_change_site, "изменение сайта отменено", None),
    "settings": (_change_settings, "настройки возвращены", None),
    "settings_access": (_change_access, "доступ вернул", None),
    "settings_clients": (_change_clients, "клиенты возвращены", None),
}
EXTRA_ROUND_TRIPS = {
    "settings:interval": (_change_interval, "настройки возвращены", None),
    "settings_access:password": (_change_password, None, None),
}


def test_every_kind_has_a_round_trip():
    assert set(ROUND_TRIPS) == set(undo.KINDS)


@pytest.mark.parametrize("name", [*ROUND_TRIPS, *EXTRA_ROUND_TRIPS])
def test_each_kind_puts_its_change_back(client, monkeypatch, name):
    prepare, message, part = {**ROUND_TRIPS, **EXTRA_ROUND_TRIPS}[name]
    change = prepare(client, monkeypatch)
    before = _contents()

    change()
    assert load_state()["undo"]["kind"] == name.split(":")[0]
    assert _contents() != before
    said = _undo(client)

    after = _contents()
    if part == "topics":
        assert [t["id"] for t in after["state"]["topics"]] == [t["id"] for t in before["state"]["topics"]]
    elif part == "titles":
        assert [t["title"] for t in after["state"]["topics"]] == [t["title"] for t in before["state"]["topics"]]
    else:
        assert after == before
    if message:
        assert said == message
    assert "undo" not in load_state()
    assert not secret_undo_path().exists()  # its saved secrets went with it
    assert "secret_undo_cleanup_pending" not in load_state()


def test_a_deleted_topic_comes_back_at_its_place(client):
    save_state({"topics": [_topic("t1", 1), _topic("t2", 2), _topic("t3", 3)], "mirrors": {}})
    client.post("/topics/t1/delete")

    _undo(client)

    assert [t["id"] for t in load_state()["topics"]] == ["t1", "t2", "t3"]


# --------------------------------------------------------------------------- a crash in the middle


class Crash(BaseException):
    """The process dies here."""


@pytest.mark.parametrize("name", ["site", "settings:interval", "settings_access:password", "settings_clients"])
@pytest.mark.parametrize("writes", [1, 2, 3, 4])
def test_a_crash_in_the_middle_of_an_undo_leaves_the_stores_as_they_were(client, monkeypatch, name, writes):
    prepare, _message, _part = {**ROUND_TRIPS, **EXTRA_ROUND_TRIPS}[name]
    change = prepare(client, monkeypatch)
    before_change = _contents()
    change()
    before = _store_bytes()
    seen: list[str] = []

    def crash(store_name):
        seen.append(store_name)
        if len(seen) == writes:
            raise Crash(store_name)

    monkeypatch.setattr(store_transaction, "_after_write", crash)
    with pytest.raises(Crash):
        undo.apply()
    monkeypatch.setattr(store_transaction, "_after_write", None)
    with persistence_lock():  # the next process: the recovery hook runs first
        pass

    assert _store_bytes() == before  # the change is still there, its undo too
    assert load_state()["undo"]["kind"] == name.split(":")[0]
    _undo(client)  # and it can still be undone
    if name != "settings_access:password":
        assert _contents() == before_change


def test_an_undo_that_cannot_be_written_changes_nothing_and_stays(client, monkeypatch):
    _change_clients(client, monkeypatch)()
    before = _store_bytes()

    def disk_full(_data):
        raise OSError("disk full")

    monkeypatch.setattr("tow.config.save_config", disk_full)

    assert _undo(client) == "откат не применён"
    assert _store_bytes() == before
    assert _events("undo_fail")[-1]["status"] == "restored"


# --------------------------------------------------------------------------- the secret snapshot


def _settings_undo(ts: str) -> None:
    reference = save_secret_undo({"telegram": {"token": "old-token"}})
    save_state({"topics": [], "mirrors": {}, "undo": {"kind": "settings", "secrets_undo_ref": reference, "ts": ts}})


def test_an_expired_undo_takes_its_secrets_with_it_before_the_next_request(client):
    _settings_undo((datetime.now(UTC) - timedelta(hours=2)).isoformat())

    assert client.get("/health.json").status_code == 200

    assert not secret_undo_path().exists()
    assert "undo" not in load_state()
    assert _events("undo_expired")


def test_a_live_undo_keeps_its_secrets(client):
    _settings_undo(iso_now())

    assert client.get("/health.json").status_code == 200

    assert secret_undo_path().exists()
    assert load_state()["undo"]["kind"] == "settings"


def test_a_change_without_secrets_replacing_one_with_secrets_removes_them(client):
    _settings_undo(iso_now())
    save_state({**load_state(), "topics": [_topic("t1")]})

    client.post("/topics/t1/delete")

    assert load_state()["undo"]["kind"] == "topic"
    assert not secret_undo_path().exists()


def test_an_older_pending_removal_does_not_colour_an_unrelated_undo(client):
    given_up = {"reference": "settings-v1", "attempts": 3}
    save_state({"topics": [_topic("t1")], "mirrors": {}, "secret_undo_cleanup_pending": given_up})
    client.post("/topics/t1/delete")

    response = client.post("/undo", follow_redirects=False)

    assert flash_kind(response.headers["location"]) == "ok"
    assert flash_of(response.headers["location"]) == "удаление отменено: наблюдение снова в списке"


@pytest.mark.parametrize("name", ["site", "settings", "settings_access:password", "settings_clients"])
def test_a_snapshot_that_cannot_be_removed_is_retried_for_every_kind(client, monkeypatch, name):
    prepare, _message, _part = {**ROUND_TRIPS, **EXTRA_ROUND_TRIPS}[name]
    prepare(client, monkeypatch)()

    def locked(_reference):
        raise OSError("file in use")

    with monkeypatch.context() as patched:
        patched.setattr("tow.store.delete_secret_undo", locked)
        said = _undo(client)

    assert "будут удалены чуть позже" in said
    assert load_state()["secret_undo_cleanup_pending"]["reference"] == "settings-v1"
    assert secret_undo_path().exists()

    client.get("/health.json")  # the retry before the next request

    assert "secret_undo_cleanup_pending" not in load_state()
    assert not secret_undo_path().exists()


def test_a_new_snapshot_settles_an_older_pending_removal(client):
    _set_password(client)  # an undo with a snapshot...
    state = load_state()
    state["secret_undo_cleanup_pending"] = {"reference": "settings-v1", "attempts": 1}
    save_state(state)

    client.post("/settings/client/add", data={"kind": "transmission"})  # ...replaced by another one

    state = load_state()
    assert "secret_undo_cleanup_pending" not in state  # the file holds the new snapshot: nothing to remove
    assert state["undo"]["kind"] == "settings_clients"
    assert secret_undo_path().exists()


# --------------------------------------------------------------------------- what "Undo" says


_SAMPLES = {
    "topic": {"item": {"id": "t1", "title": "Show / Сериал"}, "index": 0},
    "topic_put": {"item": {"id": "t1", "title": "Show"}},
    "topic_add": {"id": "t1"},
    "site": {"name": "rutor", "spec": {}},
    "settings": {"secrets_undo_ref": "settings-v1"},
    "settings_access": {"old_bind": "127.0.0.1", "old_allow_lan": False, "old_lan_auth": False},
    "settings_clients": {"config_before": {}, "secrets_undo_ref": "settings-v1"},
}


def _label(lang: str, state: dict) -> str:
    def in_language() -> str:
        i18n.use(lang)
        return undo.undo_label(state)

    return contextvars.copy_context().run(in_language)


@pytest.mark.parametrize("kind", sorted(undo.KINDS))
def test_every_kind_is_named_in_every_language(kind):
    state = {"undo": {"kind": kind, "ts": iso_now(), **_SAMPLES[kind]}}
    labels = {lang: _label(lang, state) for lang in ("en", "ru")}

    for lang, label in labels.items():
        assert label, lang
        assert label != i18n.translate("web.undo.last", lang), (kind, lang)
        assert not label.startswith("web."), (kind, lang)
    assert labels["en"] != labels["ru"]


def test_a_label_names_the_topic_or_site():
    assert _label("en", {"undo": {"kind": "topic", "ts": iso_now(), **_SAMPLES["topic"]}}) == "deleting “Show”"
    assert _label("ru", {"undo": {"kind": "site", "ts": iso_now(), **_SAMPLES["site"]}}) == "изменение сайта «rutor»"
    assert _label("en", {"undo": {"kind": "topic", "ts": iso_now(), "item": {}}}) == "deleting a topic"
    assert _label("en", {"undo": {"kind": "mystery", "ts": iso_now()}}) == "the last change"
    assert _label("en", {}) == "the last change"


def test_the_bar_reads_a_state_it_is_given():
    state = {"undo": {"kind": "topic_add", "id": "t1", "ts": iso_now()}}
    assert undo.can_undo(state) is True
    assert 0 < undo.undo_left_sec(state) <= undo.ttl_sec()
    assert undo.undo_just_made(state) is True
    stale = {"undo": {"kind": "topic_add", "id": "t1", "ts": "2020-01-01T00:00:00+00:00"}}
    assert undo.can_undo(stale) is False
    assert undo.undo_left_sec(stale) == 0
    assert undo.undo_just_made(stale) is False
    assert undo.can_undo({}) is False
