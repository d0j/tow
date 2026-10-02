"""Behaviour of the web topic, undo, site and site-store routes that other suites leave untested.

Every test runs against the isolated TOW_HOME/TOW_CONFIG from conftest with a test-only
master key, so config.yaml, state.json and the encrypted secrets are the real stores.
Tracker, client, scheduler and browser calls are stubbed; nothing leaves the process.
"""

from __future__ import annotations

import base64
import html
import json
import shutil
from datetime import UTC, datetime, timedelta
from urllib.parse import parse_qs, urlparse

import pytest
from fastapi.testclient import TestClient
from helpers import flash_of, open_network, wait_for_check_job

from tow import store_transaction
from tow.auth import SESSION_COOKIE
from tow.clock import iso_now
from tow.config import load_config, save_config
from tow.log import read_events
from tow.store import (
    SecretStoreError,
    encrypted_secrets_path,
    load_secrets,
    load_state,
    save_secret_undo,
    save_secrets,
    save_state,
    secret_undo_path,
)
from tow.web import app, services

ORIGIN = {"Origin": "http://127.0.0.1"}
RUTOR_URL = "http://rutor.info/torrent/101/show"
KINOZAL_URL = "https://kinozal.guru/details.php?id=55"
NNM_URL = "https://nnmclub.to/forum/viewtopic.php?t=77"
INFOHASH = "AB" * 20
LAN_PASSWORD = "correct-horse-battery"


@pytest.fixture(autouse=True)
def _test_master_key(monkeypatch):
    monkeypatch.setenv("TOW_MASTER_KEY", base64.urlsafe_b64encode(b"cov-web-routes-test-key-32-bytes").decode())
    monkeypatch.delenv("TOW_MASTER_KEY_FILE", raising=False)
    monkeypatch.setattr("tow.web.site_form._resolve_addresses", lambda _name: ["93.184.216.34"])


@pytest.fixture
def client():
    return TestClient(app, headers=ORIGIN)


@pytest.fixture
def no_check(monkeypatch):
    """Any unexpected tracker/client check fails the test."""
    monkeypatch.setattr("tow.web.services.run_check", lambda **_kw: pytest.fail("run_check must not be called"))
    monkeypatch.setattr("tow.title.guess_topic_title", lambda _url: "")


def _query(response) -> dict[str, str]:
    return {key: values[0] for key, values in parse_qs(urlparse(response.headers["location"]).query).items()}


def _flash(response) -> str:
    """The message a redirect left: kept on the server, the address carries only its token."""
    assert response.status_code == 303, response.text
    return flash_of(response.headers["location"])


def _path(response) -> str:
    return urlparse(response.headers["location"]).path


def _topic(tid: str = "t1", url: str = RUTOR_URL, **extra) -> dict:
    return {
        "id": tid,
        "title": "Show",
        "url": url,
        "save_path": r"M:\anime",
        "client_id": "default",
        "hash": None,
        **extra,
    }


def _seed(*topics: dict, **extra) -> None:
    save_state({"topics": list(topics), "mirrors": {}, **extra})


def _events(kind: str) -> list[dict]:
    """Logged events of one kind, oldest first (read_events lists the newest first)."""
    return [event for event in reversed(read_events(limit=200)) if event.get("kind") == kind]


def _two_clients() -> None:
    cfg = load_config()
    cfg["clients"] = {
        "default": {"kind": "qbittorrent", "default": True},
        "spare": {"kind": "qbittorrent"},
        "off": {"kind": "qbittorrent", "enabled": False},
    }
    save_config(cfg)


def _stale_ts() -> str:
    return (datetime.now(UTC) - timedelta(hours=2)).isoformat()


class FakeClient:
    """Client adapter double: reports the torrent at ``observed_path`` with TOW's tag."""

    def __init__(self, observed_path: str, *, state: str = "uploading", tags: tuple[str, ...] = ("tow",)):
        self.observed_path = observed_path
        self.state = state
        self.tags = list(tags)
        self.moves: list[tuple[str, str]] = []

    def has_hash(self, _infohash: str) -> bool:
        return True

    def inspect_torrent(self, _infohash: str) -> dict:
        return {"hash": INFOHASH, "save_path": self.observed_path, "state": self.state, "tags": self.tags}

    def set_location(self, infohash: str, save_path: str) -> str:
        self.moves.append((infohash, save_path))
        return "ok"


def _use_client(monkeypatch, fake: FakeClient) -> None:
    monkeypatch.setattr("tow.clients.factory.from_secrets", lambda *_a, **_k: fake)
    monkeypatch.setattr("tow.check.RELOCATION_WAIT_SEC", 0.0)


# --------------------------------------------------------------------------- login


def test_login_page_says_lan_auth_is_not_configured():
    open_network()  # no password and no access key

    page = TestClient(app).get("/login")

    assert page.status_code == 200
    assert "Пароль для входа с других устройств ещё не задан." in page.text
    assert "disabled" in page.text


def test_login_post_without_lan_auth_just_goes_home(client):
    response = client.post("/login", data={"password": "anything"}, follow_redirects=False)

    assert response.status_code == 303
    assert response.headers["location"] == "/"
    assert SESSION_COOKIE not in response.cookies


def test_login_post_refuses_with_503_when_credential_is_missing(client):
    open_network()  # no password and no access key

    response = client.post("/login", data={"password": "anything"}, follow_redirects=False)

    assert response.status_code == 503
    assert "Пароль для входа с других устройств ещё не задан" in response.text
    assert SESSION_COOKIE not in response.cookies


# --------------------------------------------------------------------------- downloads


def test_downloads_json_for_unknown_topic_is_404(client):
    _seed(_topic())

    response = client.get("/topics/missing/downloads.json")

    assert response.status_code == 404
    assert response.json() == {"ok": False, "error": "topic not found"}


# --------------------------------------------------------------------------- add


@pytest.mark.parametrize(
    ("form", "problem"),
    [
        ({"url": "https://unknown.example/topic/1"}, "ссылка не подходит ни к одному сайту из настроек"),
        ({"url": RUTOR_URL, "client_id": "nope"}, "выберите торрент-клиент"),
        ({"url": RUTOR_URL, "client_id": "off"}, "этот клиент отключён в настройках"),
        ({"url": RUTOR_URL, "selection_mode": "bogus"}, "неизвестный способ выбора файлов"),
        (
            {"url": RUTOR_URL, "selection_mode": "episodes", "selection_value": ""},
            "укажите, какие серии или файлы брать",
        ),
    ],
)
def test_refused_add_reopens_form_with_draft_and_saves_nothing(no_check, client, form, problem):
    _two_clients()
    data = {"title": "Show", "save_path": r"M:\anime", **form}

    response = client.post("/topics/add", data=data, follow_redirects=False)

    query = _query(response)
    assert "flash" not in query  # the reason is shown once, in the form
    assert set(query) == {"add"}  # the draft stays on the server, not in the link
    assert urlparse(response.headers["location"]).path == "/"
    from tow.web.views import _ADD_DRAFTS

    draft = _ADD_DRAFTS.get(query["add"]) or {}
    assert draft["error"] == problem
    assert draft["url"] == form["url"]
    assert draft["save_path"] == r"M:\anime"
    assert load_state()["topics"] == []
    assert "undo" not in load_state()


def test_unknown_tracker_add_is_logged_as_no_tracker(no_check, client):
    client.post(
        "/topics/add", data={"url": "https://unknown.example/t/1", "save_path": r"M:\a"}, follow_redirects=False
    )

    failures = _events("check_fail")
    assert failures
    assert failures[-1]["cls"] == "no_tracker"


def test_add_of_already_watched_url_is_refused(no_check, client):
    _seed(_topic())

    response = client.post(
        "/topics/add", data={"url": RUTOR_URL, "title": "Again", "save_path": r"M:\other"}, follow_redirects=False
    )

    assert _flash(response) == "эта раздача уже отслеживается"
    assert [t["title"] for t in load_state()["topics"]] == ["Show"]


@pytest.mark.parametrize(
    ("added", "flash"),
    [
        (True, "наблюдение TOW добавлено; раздача подтверждена клиентом"),
        (False, "наблюдение TOW добавлено; новой раздачи нет"),
    ],
)
def test_add_reports_whether_the_client_confirmed_a_torrent(monkeypatch, client, added, flash):
    monkeypatch.setattr("tow.title.guess_topic_title", lambda _url: "")
    monkeypatch.setattr(
        "tow.web.services.run_check", lambda **kw: {"results": [{"id": kw["ids"][0], "ok": True, "added": added}]}
    )

    response = client.post(
        "/topics/add", data={"url": RUTOR_URL, "title": "Show", "save_path": r"M:\anime"}, follow_redirects=False
    )

    assert _flash(response) == flash
    assert len(load_state()["topics"]) == 1


def test_add_keeps_observation_when_check_crashes_and_telegram_fails(monkeypatch, client):
    monkeypatch.setattr("tow.title.guess_topic_title", lambda _url: "")

    def crash(**_kw):
        raise RuntimeError("storage exploded")

    def telegram_down(*_args, **_kwargs):
        raise RuntimeError("telegram unreachable")

    monkeypatch.setattr("tow.web.services.run_check", crash)
    monkeypatch.setattr("tow.notify.send", telegram_down)

    response = client.post(
        "/topics/add", data={"url": RUTOR_URL, "title": "Show", "save_path": r"M:\anime"}, follow_redirects=False
    )

    assert _flash(response) == "наблюдение TOW сохранено; проверка не выполнена — подробности в журнале"
    topics = load_state()["topics"]
    assert [t["url"] for t in topics] == [RUTOR_URL]
    assert _events("add_check_fail")[-1]["topic"] == topics[0]["id"]
    assert _events("add_check_notify_fail")


def test_undo_of_add_removes_only_the_observation(monkeypatch, client):
    monkeypatch.setattr("tow.title.guess_topic_title", lambda _url: "")
    monkeypatch.setattr("tow.web.services.run_check", lambda **_kw: {"results": []})
    _seed(_topic("keep", url="http://rutor.info/torrent/5/other"))
    client.post("/topics/add", data={"url": RUTOR_URL, "title": "New", "save_path": r"M:\a"}, follow_redirects=False)
    assert len(load_state()["topics"]) == 2

    response = client.post("/undo", follow_redirects=False)

    assert _flash(response) == "добавление отменено: наблюдение убрано; раздача в торрент-клиенте не тронута"
    state = load_state()
    assert [t["id"] for t in state["topics"]] == ["keep"]
    assert "undo" not in state


# --------------------------------------------------------------------------- browser auth


def test_browser_auth_start_for_unknown_topic(client):
    _seed()

    response = client.post("/topics/nope/tracker-browser-auth", follow_redirects=False)

    assert _flash(response) == "наблюдение не найдено"
    assert "credential_topic" not in _query(response)


def test_browser_auth_start_refused_for_tracker_without_browser_auth(client):
    _seed(_topic())

    response = client.post("/topics/t1/tracker-browser-auth", follow_redirects=False)

    assert _flash(response) == "для этого сайта вход через браузер не настроен"
    assert _query(response)["credential_topic"] == "t1"


def test_browser_auth_start_failure_is_reported(monkeypatch, client):
    _seed(_topic(url=NNM_URL))

    def cannot_start(**_kwargs):
        raise RuntimeError("no browser")

    monkeypatch.setattr(services.browser_auth, "start", cannot_start)

    response = client.post("/topics/t1/tracker-browser-auth", follow_redirects=False)

    assert _flash(response) == "не удалось запустить авторизацию"
    assert _query(response)["credential_topic"] == "t1"


def test_browser_auth_status_of_unknown_operation_is_idle(client):
    response = client.get("/topics/t1/tracker-browser-auth/status?operation_id=browser-auth-unknown")

    assert response.status_code == 200
    assert response.json() == {"status": "idle", "topic_id": "t1"}


# --------------------------------------------------------------------------- tracker login


@pytest.mark.parametrize(
    ("tid", "form", "flash", "prompt"),
    [
        ("missing", {"username": "u", "password": "p"}, "наблюдение не найдено", False),
        ("rutor", {"username": "u", "password": "p"}, "для этого сайта вход не нужен", False),
        ("kinozal", {"username": "u", "password": " "}, "введите логин и пароль", True),
    ],
)
def test_tracker_login_refusals_store_nothing(no_check, client, tid, form, flash, prompt):
    _seed(_topic("rutor"), _topic("kinozal", url=KINOZAL_URL))

    response = client.post(f"/topics/{tid}/tracker-login", data=form, follow_redirects=False)

    assert _flash(response) == flash
    assert ("credential_topic" in _query(response)) is prompt
    assert not encrypted_secrets_path().exists()


def test_tracker_login_reports_unavailable_secret_store(no_check, monkeypatch, client):
    monkeypatch.delenv("TOW_MASTER_KEY")
    _seed(_topic(url=KINOZAL_URL))

    response = client.post("/topics/t1/tracker-login", data={"username": "u", "password": "p"}, follow_redirects=False)

    assert _flash(response) == "логин не сохранён: хранилище паролей недоступно"
    assert _query(response)["credential_topic"] == "t1"
    assert not encrypted_secrets_path().exists()


def test_tracker_login_blocked_check_restores_previous_password(monkeypatch, client):
    _seed(_topic(url=KINOZAL_URL))
    save_secrets({"trackers": {"kinozal": {"username": "old-user", "password": "old-pw"}}})

    def blocked(**_kw):
        raise SecretStoreError("secret gate")

    monkeypatch.setattr("tow.web.services.run_check", blocked)

    response = client.post(
        "/topics/t1/tracker-login", data={"username": "new-user", "password": "new-pw"}, follow_redirects=False
    )

    assert _flash(response) == "вход не подтверждён: проверка заблокирована"
    assert load_secrets()["trackers"]["kinozal"] == {"username": "old-user", "password": "old-pw"}


def test_tracker_login_auth_failure_drops_unverified_new_login(monkeypatch, client):
    _seed(_topic(url=KINOZAL_URL))

    def auth_failed(**_kw):
        raise RuntimeError("tracker auth failed")

    monkeypatch.setattr("tow.web.services.run_check", auth_failed)

    response = client.post("/topics/t1/tracker-login", data={"username": "u", "password": "p"}, follow_redirects=False)

    assert _flash(response) == "вход в kinozal не прошёл: tracker auth failed"
    assert _query(response)["credential_topic"] == "t1"
    assert "kinozal" not in (load_secrets().get("trackers") or {})


def test_tracker_login_transport_failure_keeps_the_new_login(monkeypatch, client):
    _seed(_topic(url=KINOZAL_URL))

    def mirrors_down(**_kw):
        raise RuntimeError("all hosts failed")

    monkeypatch.setattr("tow.web.services.run_check", mirrors_down)

    response = client.post("/topics/t1/tracker-login", data={"username": "u", "password": "p"}, follow_redirects=False)

    assert _flash(response) == "вход в kinozal не прошёл: all hosts failed"
    assert load_secrets()["trackers"]["kinozal"] == {"username": "u", "password": "p"}


def test_tracker_login_rejected_row_restores_previous_partial_entry(monkeypatch, client):
    _seed(_topic(url=KINOZAL_URL))
    save_secrets({"trackers": {"kinozal": {"username": "old-user"}}})
    monkeypatch.setattr(
        "tow.web.services.run_check",
        lambda **_kw: {"results": [{"id": "t1", "ok": False, "error": "tracker auth required"}]},
    )

    response = client.post(
        "/topics/t1/tracker-login", data={"username": "new-user", "password": "new-pw"}, follow_redirects=False
    )

    assert _flash(response) == "вход в kinozal не прошёл: tracker auth required"
    assert _query(response)["credential_topic"] == "t1"
    assert load_secrets()["trackers"]["kinozal"] == {"username": "old-user"}


@pytest.mark.parametrize(
    "concurrent",
    [
        {"trackers": {"kinozal": {"username": "someone-else", "password": "their-pw"}}},
        {},
    ],
    ids=["password-changed-meanwhile", "secrets-cleared-meanwhile"],
)
def test_tracker_login_never_overwrites_a_concurrent_secret_change(monkeypatch, client, concurrent):
    _seed(_topic(url=KINOZAL_URL))

    def check_while_someone_edits(**_kw):
        save_secrets(concurrent)
        return {"results": [{"id": "t1", "ok": False, "error": "tracker auth required"}]}

    monkeypatch.setattr("tow.web.services.run_check", check_while_someone_edits)

    response = client.post("/topics/t1/tracker-login", data={"username": "u", "password": "p"}, follow_redirects=False)

    assert _flash(response) == (
        "вход в kinozal не прошёл: tracker auth required; прежний пароль не восстановлен: данные уже изменились"
    )
    assert load_secrets() == concurrent


def test_tracker_login_reports_when_restore_itself_fails(monkeypatch, client):
    _seed(_topic(url=KINOZAL_URL))
    monkeypatch.setattr(
        "tow.web.services.run_check",
        lambda **_kw: {"results": [{"id": "t1", "ok": True, "source": "matching_magnet"}]},
    )

    def restore_fails(*_args):
        raise SecretStoreError("store locked")

    monkeypatch.setattr("tow.web.routes_topic_login.restore_unverified_password", restore_fails)

    response = client.post("/topics/t1/tracker-login", data={"username": "u", "password": "p"}, follow_redirects=False)

    assert _flash(response) == "вход в kinozal не подтверждён; не удалось восстановить прежний пароль"
    assert _query(response)["credential_topic"] == "t1"
    assert _events("site_login_restore_failed")


# --------------------------------------------------------------------------- delete / undo


def test_delete_of_unknown_topic_changes_nothing(client):
    _seed(_topic())

    response = client.post("/topics/nope/delete", follow_redirects=False)

    assert _flash(response) == "раздача не найдена; ничего не удалено"
    state = load_state()
    assert [t["id"] for t in state["topics"]] == ["t1"]
    assert "undo" not in state


def test_expired_undo_is_dropped_without_restoring(client):
    _seed(undo={"kind": "topic", "item": _topic("gone"), "ts": _stale_ts()})

    response = client.post("/undo", follow_redirects=False)

    assert _flash(response) == "срок отмены истёк"
    state = load_state()
    assert state["topics"] == []
    assert "undo" not in state
    assert _events("undo_expired")


def test_an_expired_undo_takes_its_saved_secrets_with_it(client):
    reference = save_secret_undo({"telegram": {"token": "old-token"}})
    _seed(undo={"kind": "settings", "secrets_undo_ref": reference, "interval_sec": 3600, "ts": _stale_ts()})

    client.post("/undo", follow_redirects=False)

    assert not secret_undo_path().exists()


def test_an_undo_without_secrets_replacing_one_with_secrets_removes_them(client):
    save_secrets({"telegram": {"token": "now"}})
    reference = save_secret_undo({"telegram": {"token": "old-token"}})
    _seed(_topic(), undo={"kind": "settings", "secrets_undo_ref": reference, "interval_sec": 3600, "ts": iso_now()})

    client.post("/topics/t1/delete", follow_redirects=False)  # a new undo (the deleted topic), no secrets

    assert load_state()["undo"]["kind"] == "topic"
    assert not secret_undo_path().exists()


def test_a_new_undo_with_its_own_secrets_keeps_the_file():
    from tow import store_transaction, undo
    from tow.store import load_secret_undo

    state = load_state()
    state["undo"] = {"kind": "settings", "secrets_undo_ref": save_secret_undo({"a": {}}), "ts": iso_now()}

    with store_transaction.transaction() as txn:
        undo.stamp(state, "settings", txn=txn, snapshot={"b": {}})
        txn.save_state(state)
    assert load_secret_undo(state["undo"]["secrets_undo_ref"]) == {"b": {}}  # the snapshot the new undo needs
    with store_transaction.transaction() as txn:
        undo.stamp(state, "site", txn=txn, snapshot={"name": "rutor", "present": False, "value": None}, name="rutor")
        txn.save_state(state)
    assert state["undo"]["secret_undo_ref"]  # a site change writes its own snapshot in its transaction
    assert secret_undo_path().exists()


def test_a_failed_delete_is_left_for_the_cleanup_retry(client, monkeypatch):
    reference = save_secret_undo({"telegram": {"token": "old-token"}})
    state = load_state()
    state["undo"] = {"kind": "settings", "secrets_undo_ref": reference, "ts": iso_now()}

    def refuse(_reference):
        raise SecretStoreError("cannot remove TOW secret undo snapshot")

    monkeypatch.setattr("tow.store.delete_secret_undo", refuse)
    from tow import undo

    undo.stamp(state, "topic_add", id="t9")

    assert state["secret_undo_cleanup_pending"]["reference"] == reference


@pytest.mark.parametrize(
    "undo",
    [
        {"kind": "topic", "item": "not-a-topic"},
        {"kind": "site", "name": "rutor", "spec": "broken"},
        {"kind": "mystery"},
    ],
)
def test_incomplete_or_unknown_undo_restores_nothing(client, undo):
    _seed(_topic(), undo={**undo, "ts": iso_now()})
    config_before = load_config()

    response = client.post("/undo", follow_redirects=False)

    assert _flash(response) == "нечего возвращать"
    state = load_state()
    assert [t["id"] for t in state["topics"]] == ["t1"]
    assert "undo" not in state
    assert load_config() == config_before


def _edited_topic_with_undo() -> None:
    old = _topic(hash=INFOHASH, save_path=r"M:\anime", tracking_mode="watch")  # no "selection" key
    current = _topic(
        hash=INFOHASH,
        title="Edited",
        save_path=r"P:\new",
        selection={"mode": "files", "value": "*.mkv"},
        tracking_mode="watch",
        once_done=True,
        last_ok=True,
        move_pending={"from": r"M:\anime", "to": r"P:\new", "since": iso_now()},
    )
    _seed(current, undo={"kind": "topic_put", "item": old, "ts": iso_now()})


def test_undo_of_path_edit_moves_torrent_back_and_restores_form_fields(monkeypatch, client):
    _edited_topic_with_undo()
    fake = FakeClient(r"M:\anime")
    _use_client(monkeypatch, fake)

    response = client.post("/undo", follow_redirects=False)

    assert _flash(response) == "правка раздачи отменена"
    assert fake.moves == [(INFOHASH, r"M:\anime")]
    restored = load_state()["topics"][0]
    assert restored["title"] == "Show"
    assert restored["save_path"] == r"M:\anime"
    assert "selection" not in restored
    assert "move_pending" not in restored
    assert restored["selection_dirty"] is True
    assert restored["once_done"] is False
    assert restored["last_ok"] is True  # check results written after the edit stay
    assert _events("qbit_move_undo")[-1]["status"] == "succeeded"


def test_undo_of_path_edit_records_a_move_still_in_progress(monkeypatch, client):
    _edited_topic_with_undo()
    _use_client(monkeypatch, FakeClient(r"P:\new", state="moving"))

    response = client.post("/undo", follow_redirects=False)

    assert _flash(response) == "правка раздачи отменена"
    restored = load_state()["topics"][0]
    assert restored["save_path"] == r"M:\anime"
    assert restored["move_pending"]["from"] == r"P:\new"
    assert restored["move_pending"]["to"] == r"M:\anime"
    assert _events("qbit_move_undo")[-1]["status"] == "moving"


@pytest.mark.parametrize(
    ("fake", "moved"),
    [
        (FakeClient(r"P:\new", state="stalledUP"), True),
        (FakeClient(r"P:\new", tags=()), False),
    ],
    ids=["relocation-not-confirmed", "not-owned-by-tow"],
)
def test_undo_of_path_edit_keeps_topic_when_client_does_not_confirm(monkeypatch, client, fake, moved):
    _edited_topic_with_undo()
    _use_client(monkeypatch, fake)

    response = client.post("/undo", follow_redirects=False)

    assert _flash(response) == "не удалось вернуть расположение"
    assert bool(fake.moves) is moved
    topic = load_state()["topics"][0]
    assert topic["title"] == "Edited"
    assert topic["save_path"] == r"P:\new"
    assert _events("qbit_move_undo_fail")


@pytest.mark.parametrize(
    "snapshot",
    [
        {"name": "someone-else", "present": True, "value": {"username": "u"}},
        {"name": "ghost", "present": True, "value": "not-a-dict"},
    ],
)
def test_site_undo_with_invalid_secret_snapshot_is_refused(client, snapshot):
    reference = save_secret_undo(snapshot)
    _seed(
        undo={
            "kind": "site",
            "name": "ghost",
            "spec": {"title": "ghost", "url_regex": "ghost"},
            "secret_undo_ref": reference,
            "ts": iso_now(),
        }
    )

    response = client.post("/undo", follow_redirects=False)

    assert _path(response) == "/sites"
    assert _flash(response) == "не удалось вернуть сохранённые пароли"
    assert "ghost" not in load_config()["trackers"]
    assert load_secrets() == {}


# --------------------------------------------------------------------------- settings undo


def _settings_undo(secret_scope, snapshot, **extra) -> None:
    reference = save_secret_undo(snapshot)
    _seed(
        undo={"kind": "settings", "secrets_undo_ref": reference, "secret_scope": secret_scope, "ts": iso_now(), **extra}
    )


def test_settings_undo_restores_scoped_secret_interval_and_flash_ttl(client):
    save_secrets({"telegram": {"token": "new-token"}, "trackers": {"rutor": {"username": "kept"}}})
    _settings_undo(["telegram"], {"telegram": {"token": "old-token"}}, interval_sec=7200, flash_ttl_sec=120)

    response = client.post("/undo", follow_redirects=False)

    assert _path(response) == "/settings"
    assert _flash(response) == "настройки возвращены"
    cfg = load_config()
    assert cfg["interval_sec"] == 7200
    assert cfg["flash_ttl_sec"] == 120
    assert load_secrets() == {"telegram": {"token": "old-token"}, "trackers": {"rutor": {"username": "kept"}}}
    assert "undo" not in load_state()
    assert not secret_undo_path().exists()


@pytest.mark.parametrize(
    ("current", "scope", "flash", "expected"),
    [
        (
            {"clients": {"main": {"password": "new"}, "spare": {"password": "keep"}}},
            ["clients", "main"],
            "настройки возвращены",
            {"clients": {"spare": {"password": "keep"}}},
        ),
        ({"telegram": {"token": "now"}}, [], "настройки возвращены", {"telegram": {"token": "now"}}),
        (
            {"telegram": {"token": "now"}},
            ["a", "b", "c"],
            "не удалось вернуть сохранённые пароли",
            {"telegram": {"token": "now"}},
        ),
        ({"clients": "broken"}, ["clients", "main"], "не удалось вернуть сохранённые пароли", {"clients": "broken"}),
    ],
    ids=["missing-key-is-removed", "empty-scope", "scope-too-deep", "target-not-a-mapping"],
)
def test_settings_undo_secret_scope(client, current, scope, flash, expected):
    save_secrets(current)
    _settings_undo(scope, {"telegram": {"token": "old"}})

    response = client.post("/undo", follow_redirects=False)

    assert _flash(response) == flash
    assert load_secrets() == expected


def test_settings_undo_rolls_back_when_config_write_fails(monkeypatch, client):
    import tow.config

    real_set_interval = tow.config.set_interval_sec
    calls: list[int] = []

    def set_interval_once_failing(sec):
        calls.append(sec)
        if len(calls) == 1:
            raise OSError("config is read-only")
        real_set_interval(sec)

    monkeypatch.setattr("tow.config.set_interval_sec", set_interval_once_failing)
    save_secrets({"telegram": {"token": "new-token"}})
    _settings_undo(["telegram"], {"telegram": {"token": "old-token"}}, interval_sec=7200, flash_ttl_sec=120)

    response = client.post("/undo", follow_redirects=False)

    assert _flash(response) == "не удалось применить откат"
    cfg = load_config()
    assert cfg["interval_sec"] == 3600
    assert cfg["flash_ttl_sec"] == 60
    assert load_secrets() == {"telegram": {"token": "new-token"}}
    assert _events("undo_fail")[-1]["status"] == "restored"


def _set_lan_password(client) -> None:
    """The password is set on the password card; /settings/access only opens the network."""
    saved = client.post(
        "/settings/password",
        data={"lan_password": LAN_PASSWORD, "lan_password2": LAN_PASSWORD},
        follow_redirects=False,
    )
    assert saved.status_code == 303
    assert "lan_auth" in load_secrets()


def test_settings_access_undo_removes_a_new_lan_password(client):
    _set_lan_password(client)

    response = client.post("/undo", follow_redirects=False)

    assert _flash(response) == "пароль отменён: у TOW снова нет пароля; все устройства, вошедшие по сети, вышли"
    assert "lan_auth" not in load_secrets()
    assert not secret_undo_path().exists()
    assert "undo" not in load_state()


def test_settings_access_undo_restores_bind(client):
    _set_lan_password(client)
    opened = client.post("/settings/access", data={"allow_lan": "1"}, follow_redirects=False)
    assert opened.status_code == 303
    assert load_config()["allow_lan"] is True

    response = client.post("/undo", follow_redirects=False)

    assert _flash(response) == "доступ вернул"
    cfg = load_config()
    assert (cfg["bind"], cfg["allow_lan"]) == ("127.0.0.1", False)
    assert "lan_auth" in load_secrets()  # the password was set by an earlier, separate change
    assert "undo" not in load_state()
    assert _events("settings_access_undo")[-1]["bind"] == "127.0.0.1"


def test_settings_access_ignores_a_password_field(client):
    # Older pages posted lan_password here and the reminder was silently dropped; now ignored.
    client.post("/settings/access", data={"allow_lan": "1", "lan_password": LAN_PASSWORD}, follow_redirects=False)

    assert "lan_auth" not in load_secrets()
    assert load_config()["allow_lan"] is False  # no password yet: the network stays closed


def test_settings_access_undo_keeps_everything_when_config_write_fails(monkeypatch, client):
    cfg = load_config()
    cfg.update(allow_lan=True, bind="0.0.0.0")
    save_config(cfg)
    _set_lan_password(client)
    secrets_before = load_secrets()

    def read_only(_cfg):
        raise OSError("config is read-only")

    monkeypatch.setattr("tow.config.save_config", read_only)

    response = client.post("/undo", follow_redirects=False)

    assert _flash(response) == "откат доступа не применён"
    assert _query(response)["open"] == "access"
    assert load_secrets() == secrets_before
    assert load_config()["allow_lan"] is True


@pytest.mark.parametrize(
    ("undo", "flash"),
    [
        ({}, "нечего возвращать"),
        (
            {"old_bind": "127.0.0.1", "old_allow_lan": False, "old_lan_auth": False, "secrets_undo_ref": "unknown"},
            "не удалось вернуть пароль",
        ),
    ],
)
def test_settings_access_undo_refusals_leave_config_alone(client, undo, flash):
    cfg = load_config()
    cfg.update({"allow_lan": True, "bind": "0.0.0.0"})
    save_config(cfg)
    _seed(undo={"kind": "settings_access", "ts": iso_now(), **undo})

    response = client.post("/undo", follow_redirects=False)

    assert _path(response) == "/settings"
    assert _flash(response) == flash
    assert load_config()["bind"] == "0.0.0.0"


# --------------------------------------------------------------------------- edit


@pytest.mark.parametrize(
    ("form", "flash"),
    [
        ({"selection_mode": "bogus"}, "неизвестный способ выбора файлов"),
        ({"client_id": "nope"}, "выберите клиент"),
        ({"client_id": "spare"}, "клиент активной раздачи менять нельзя; создайте новое наблюдение"),
        ({"save_path": ""}, "укажите папку"),
        ({"save_path": r"relative\dir"}, "нужен полный путь, например D:\\Media"),
    ],
)
def test_refused_edit_changes_nothing(client, form, flash):
    _two_clients()
    # No folder anywhere in state: an empty form path has nothing to fall back to.
    _seed(_topic("other", url="http://rutor.info/torrent/9/x", save_path=""), _topic(hash=INFOHASH, save_path=""))
    data = {"title": "Renamed", "url": RUTOR_URL, "save_path": r"M:\anime", "client_id": "default", **form}

    response = client.post("/topics/t1/edit", data=data, follow_redirects=False)

    assert _flash(response) == flash
    state = load_state()
    assert state["topics"][1]["title"] == "Show"
    assert state["topics"][1]["client_id"] == "default"
    assert "undo" not in state


def test_edit_tracks_a_move_the_client_took_but_did_not_confirm(monkeypatch, client):
    # The client got the command but still reports the old folder at the deadline: the topic
    # records the move as pending (reconcile accepts the old folder until the client agrees)
    # instead of keeping the old path and failing every later check with "path differs".
    fake = FakeClient(r"M:\anime")
    _use_client(monkeypatch, fake)
    _seed(_topic(hash=INFOHASH))

    response = client.post(
        "/topics/t1/edit", data={"title": "Show", "url": RUTOR_URL, "save_path": r"P:\new"}, follow_redirects=False
    )

    assert _flash(response) == (
        "сохранено, торрент-клиент принял команду, перенос пока не подтверждён — TOW проверит позже"
    )
    assert fake.moves == [(INFOHASH, r"P:\new")]
    topic = load_state()["topics"][0]
    assert topic["save_path"] == r"P:\new"
    assert topic["move_pending"]["from"] == r"M:\anime"
    assert topic["move_pending"]["unconfirmed"] is True
    assert _events("qbit_move")[-1]["status"] == "unconfirmed"


def test_reconcile_accepts_the_old_folder_while_a_move_is_unconfirmed():
    from tow.progress import _check_save_path

    topic = {"save_path": r"P:\new", "move_pending": {"from": r"M:\anime", "to": r"P:\new", "unconfirmed": True}}
    assert _check_save_path(topic, {"save_path": r"M:\anime", "state": "uploading"}) == r"M:\anime"
    assert _check_save_path(topic, {"save_path": r"P:\new", "state": "uploading"}) == r"P:\new"
    assert "move_pending" not in topic  # the client agrees now


# --------------------------------------------------------------------------- manual check


def test_topic_check_blocked_by_secret_store(monkeypatch, client):
    def blocked(**_kw):
        raise SecretStoreError("secret gate")

    monkeypatch.setattr("tow.web.services.run_check", blocked)

    response = client.post("/topics/t1/check", follow_redirects=False)

    assert _flash(response) == "проверка заблокирована: хранилище паролей недоступно"


@pytest.mark.parametrize(
    ("row", "flash"),
    [
        ({"ok": True, "selection_updated": True}, "выбор файлов обновлён"),
        ({"ok": True, "changed": True}, "новая раздача"),
        ({"ok": True, "skipped": "уже в клиенте"}, "без изменений: уже в клиенте"),
        ({"ok": True}, "без изменений"),
    ],
)
def test_topic_check_flash_describes_the_result(monkeypatch, client, row, flash):
    _seed(_topic())
    monkeypatch.setattr("tow.web.services.run_check", lambda **_kw: {"results": [{"id": "t1", **row}]})

    response = client.post("/topics/t1/check", follow_redirects=False)

    assert _flash(response) == flash


def _wait_job(client, response) -> dict:
    """The job's end as the page sees it; its message also waits on the server for Home."""
    body = wait_for_check_job(client, _query(response)["check_job"])
    redirect = body.pop("redirect")
    assert flash_of(redirect) == body["flash"]
    return body


def test_check_all_preview_counts_results(monkeypatch, client):
    seen: dict = {}

    def fake(**kw):
        seen.update(kw)
        return {"results": [{"ok": True, "changed": True}, {"ok": True}, {"ok": False}]}

    monkeypatch.setattr("tow.web.services.run_check", fake)

    response = client.post("/check", data={"mode": "dry"}, follow_redirects=False)

    assert _flash(response) == "проверка запущена"
    body = _wait_job(client, response)
    assert body == {"status": "done", "flash": "предпросмотр: новое 1, без изменений 1, сбой 1"}
    assert seen["apply"] is False


@pytest.mark.parametrize(
    ("error", "flash"),
    [
        (SecretStoreError("secret gate"), "проверка заблокирована: хранилище паролей недоступно"),
        (ValueError("bad state"), "проверка не выполнена: ValueError"),
    ],
)
def test_check_all_failure_is_reported_by_status(monkeypatch, client, error, flash):
    def fail(**_kw):
        raise error

    monkeypatch.setattr("tow.web.services.run_check", fail)

    response = client.post("/check", data={"mode": "apply"}, follow_redirects=False)

    assert _wait_job(client, response) == {"status": "failed", "flash": flash}


def test_check_status_of_unknown_job(client):
    assert client.get("/check/status?job=not-a-job").json() == {"status": "unknown"}
    assert client.get("/check/status").json() == {"status": "unknown"}


# --------------------------------------------------------------------------- site guesses


def test_guess_title_never_fails_the_form(monkeypatch, client):
    def tracker_down(_url):
        raise RuntimeError("tracker down")

    monkeypatch.setattr("tow.title.guess_topic_title", tracker_down)
    assert client.post("/topics/guess-title", data={"url": RUTOR_URL}).json() == {"ok": False, "title": ""}

    monkeypatch.setattr("tow.title.guess_topic_title", lambda url: f"title for {url}")
    assert client.post("/topics/guess-title", data={"url": f"  {RUTOR_URL} "}).json() == {
        "ok": True,
        "title": f"title for {RUTOR_URL}",
    }


def test_site_guess_reports_errors_and_existing_sites(client):
    magnet = client.post("/sites/guess", data={"url": "magnet:?xt=urn:btih:x"}).json()
    assert magnet["ok"] is False
    assert "magnet" in magnet["error"]

    known = client.post("/sites/guess", data={"url": "https://rutor.info/torrent/1"}).json()
    assert (known["ok"], known["name"], known["exists"]) == (True, "rutor", True)

    fresh = client.post("/sites/guess", data={"url": "https://fresh.example/viewtopic.php?t=5"}).json()
    assert (fresh["ok"], fresh["name"], fresh["exists"]) == (True, "fresh", False)


# --------------------------------------------------------------------------- sites add


@pytest.fixture
def no_probe(monkeypatch):
    probes: list[dict] = []
    monkeypatch.setattr("tow.web.services.doctor_report", lambda **kw: probes.append(kw) or {"probes": []})
    return probes


@pytest.mark.parametrize(
    ("form", "flash"),
    [
        ({"from_url": "magnet:?xt=urn:btih:x"}, "это magnet — нужна ссылка на страницу или файл с сайта"),
        ({"name": "new", "url_regex": "x", "fetch_hosts": "https://a.example"}, "Имя: только латинские буквы"),
        ({"name": "fresh", "url_regex": "x", "fetch_hosts": "ftp://a.example"}, "Зеркала: каждая строка — адрес сайта"),
        ({"name": "fresh", "fetch_hosts": "https://a.example"}, "Шаблон ссылки на раздачу: заполните его"),
        ({"name": "fresh", "url_regex": "x"}, "Зеркала: укажите хотя бы один адрес сайта"),
        (
            {"name": "fresh", "url_regex": "x", "fetch_hosts": "https://a.example", "download_path": "http://e/x"},
            "Путь к торрент-файлу: нужен путь на сайте, начинающийся с /",
        ),
        ({"name": "fresh", "url_regex": "(a+)+c", "fetch_hosts": "https://a.example"}, "подвесить TOW"),
    ],
)
def test_refused_site_add_writes_nothing(no_probe, client, form, flash):
    config_before = load_config()

    response = client.post("/sites/new", data=form, follow_redirects=False)

    # The form comes back open with the reason (a draft kept on the server), not as a flash.
    assert _path(response) == "/sites"
    assert response.headers["location"].startswith("/sites?add=")
    assert flash in html.unescape(client.get(response.headers["location"]).text)
    assert load_config() == config_before
    assert no_probe == []


def test_site_add_for_existing_site_without_login_changes_nothing(no_probe, client):
    _seed(undo={"kind": "topic", "item": _topic(), "ts": iso_now()})

    response = client.post("/sites/new", data={"name": "rutor"}, follow_redirects=False)

    assert "Сайт «rutor» уже есть" in html.unescape(client.get(response.headers["location"]).text)
    assert not encrypted_secrets_path().exists()
    assert load_state()["undo"]["kind"] == "topic"


def test_site_add_for_existing_site_saves_login_and_drops_older_undo(no_probe, client):
    _seed(undo={"kind": "topic", "item": _topic(), "ts": iso_now()})

    response = client.post(
        "/sites/new", data={"name": "rutor", "username": " user ", "password": "pw"}, follow_redirects=False
    )

    assert _flash(response) == "логин сохранён"
    assert load_secrets()["trackers"]["rutor"] == {"username": "user", "password": "pw"}
    assert "undo" not in load_state()
    assert no_probe == []


def test_site_add_from_url_uses_the_guess_and_probes_the_new_site(no_probe, client):
    response = client.post(
        "/sites/new",
        data={"from_url": "https://fresh.example/viewtopic.php?t=5", "username": "me", "password": "pw"},
        follow_redirects=False,
    )

    assert _flash(response) == "сайт добавлен"
    spec = load_config()["trackers"]["fresh"]
    assert spec["fetch_hosts"] == ["https://fresh.example"]
    assert spec["login_hosts"] == ["https://fresh.example"]
    assert spec["login_path"] == "/login.php"
    assert spec["page_download"] is True
    assert spec["topic_path"] == "/viewtopic.php?t={id}"
    assert spec["download_href_regex"]
    assert load_secrets()["trackers"]["fresh"] == {"username": "me", "password": "pw"}
    assert no_probe == [{"probe": True, "names": ["fresh"]}]
    assert _events("site_add")[-1]["tracker"] == "fresh"


# --------------------------------------------------------------------------- sites edit / actions


@pytest.mark.parametrize(
    ("path", "data"),
    [
        ("/sites/ghost/freeze", {}),
        ("/sites/ghost/delete", {}),
        ("/sites/ghost", {"fetch_hosts": "https://ghost.example"}),
        ("/sites/ghost/login", {"username": "u", "password": "p"}),
        ("/sites/ghost/probe", {}),
    ],
)
def test_actions_on_unknown_site_are_refused(monkeypatch, client, path, data):
    monkeypatch.setattr("tow.web.services.doctor_report", lambda **_kw: pytest.fail("no probe for unknown site"))
    config_before = load_config()

    response = client.post(path, data=data, follow_redirects=False)

    assert _flash(response) == "нет сайта"
    assert load_config() == config_before
    assert not encrypted_secrets_path().exists()


def test_freeze_toggles_site_pause(client):
    first = client.post("/sites/rutor/freeze", follow_redirects=False)
    assert _flash(first) == "пауза"
    assert load_state()["mirrors"]["rutor"]["frozen"] is True

    second = client.post("/sites/rutor/freeze", follow_redirects=False)
    assert _flash(second) == "возобновлено"
    assert load_state()["mirrors"]["rutor"]["frozen"] is False
    assert [e["status"] for e in _events("site_pause")] == ["paused", "resumed"]


@pytest.mark.parametrize(
    ("form", "flash"),
    [
        ({"login_hosts": "https://elsewhere.example"}, "адрес входа должен быть одним из зеркал"),
        ({"new_name": "kinozal"}, "имя занято"),
        ({"login_path": "relative"}, "Путь входа: нужен путь на сайте, начинающийся с /"),
        ({"download_href_regex": "(a*)*"}, "подвесить TOW"),
    ],
)
def test_refused_site_save_changes_nothing(client, form, flash):
    config_before = load_config()

    response = client.post("/sites/rutor", data={"fetch_hosts": "http://rutor.info", **form}, follow_redirects=False)

    assert flash in _flash(response)
    assert load_config() == config_before
    assert "undo" not in load_state()


def test_site_save_sets_and_clears_optional_page_fields(client):
    filled = client.post(
        "/sites/rutor",
        data={
            "fetch_hosts": "http://rutor.info",
            "topic_path": "/torrent/{id}",
            "page_download": "on",
            "download_href_regex": r"download/(\d+)",
        },
        follow_redirects=False,
    )
    assert _flash(filled) == "сохранено"
    spec = load_config()["trackers"]["rutor"]
    assert (spec["topic_path"], spec["page_download"], spec["download_href_regex"]) == (
        "/torrent/{id}",
        True,
        r"download/(\d+)",
    )
    assert spec["download_path"] == "/download/{id}"  # blank form value keeps the old path

    cleared = client.post("/sites/rutor", data={"fetch_hosts": "http://rutor.info"}, follow_redirects=False)
    assert _flash(cleared) == "сохранено"
    spec = load_config()["trackers"]["rutor"]
    assert not {"topic_path", "page_download", "download_href_regex"} & set(spec)


def test_site_rename_without_mirror_state_and_undo(client):
    _seed()

    renamed = client.post(
        "/sites/rutor", data={"fetch_hosts": "http://rutor.info", "new_name": "rutor_two"}, follow_redirects=False
    )

    assert _flash(renamed) == "сохранено"
    trackers = load_config()["trackers"]
    assert "rutor" not in trackers
    assert trackers["rutor_two"]["title"] == "rutor_two"
    assert load_state()["mirrors"] == {}

    undone = client.post("/undo", follow_redirects=False)

    assert _flash(undone) == "изменение сайта отменено"
    trackers = load_config()["trackers"]
    assert "rutor_two" not in trackers
    assert trackers["rutor"]["fetch_hosts"][0] == "http://d.rutor.info"
    assert load_state()["mirrors"] == {}


def test_prefer_moves_mirror_first_and_marks_it_active(client):
    response = client.post("/sites/rutor/prefer", data={"host": "http://rutor.is/"}, follow_redirects=False)

    assert _flash(response) == "основное зеркало"
    hosts = load_config()["trackers"]["rutor"]["fetch_hosts"]
    assert hosts[0] == "http://rutor.is"
    assert sorted(hosts) == sorted(
        ["http://d.rutor.info", "http://rutor.info", "https://new-rutor.org", "http://rutor.is"]
    )
    assert load_state()["mirrors"]["rutor"]["active"] == "http://rutor.is"


def test_prefer_unknown_mirror_changes_nothing(client):
    config_before = load_config()

    response = client.post("/sites/rutor/prefer", data={"host": "http://nope.example"}, follow_redirects=False)

    assert _flash(response) == "нет такого зеркала"
    assert load_config() == config_before


@pytest.mark.parametrize(
    ("report", "referer", "path", "flash"),
    [
        (
            {"ok": True, "degraded": ["a", "b"]},
            "http://127.0.0.1/sites",
            "/sites",
            "проверка состояния: всё в порядке; не отвечают зеркал: 2",
        ),
        ({"ok": True}, "http://127.0.0.1/settings?open=x", "/settings", "проверка состояния: всё в порядке"),
        (
            {
                "ok": False,
                "qbit": "FAIL: connection refused",
                "probes": [
                    {"tracker": "rutor", "ok": False},
                    {"tracker": "rutor", "ok": False},
                    {"tracker": "kinozal", "ok": False},
                    {"tracker": "kinozal", "ok": True},
                ],
            },
            "http://127.0.0.1/topics/t1/edit",
            "/doctor",
            "проверка состояния: торрент-клиент не отвечает; ни одно зеркало не отвечает: rutor",
        ),
        ({"ok": False, "qbit": "ok"}, None, "/", "проверка состояния: есть проблемы"),
    ],
)
def test_doctor_run_summarises_and_returns_to_known_page(monkeypatch, client, report, referer, path, flash):
    monkeypatch.setattr("tow.web.services.doctor_report", lambda **_kw: report)
    headers = {"Referer": referer} if referer else {}

    response = client.post("/doctor/run", headers=headers, follow_redirects=False)

    assert _path(response) == path
    assert _flash(response) == flash
    assert _events("site_probe")


# --------------------------------------------------------------------------- site store journal


def _open_journal():
    root = store_transaction.begin()
    manifest = json.loads((root / "MANIFEST.json").read_text(encoding="utf-8"))
    return root, manifest


def _write_manifest(root, manifest) -> None:
    (root / "MANIFEST.json").write_text(json.dumps(manifest), encoding="utf-8")


def _entry(manifest, key) -> dict:
    return next(entry for entry in manifest["targets"] if entry["key"] == key)


def test_interrupted_site_transaction_is_rolled_back_on_next_request(client):
    _seed(_topic())
    config_before = load_config()
    state_before = load_state()
    # A transaction runs under the data lock (the recovery hook cannot interrupt it); the
    # process "crashed" after writing some stores and the lock went with it.
    with services.persistence_lock():
        root, _manifest = _open_journal()
        save_config({**config_before, "trackers": {}})
        save_state({"topics": [], "mirrors": {}})
        save_secrets({"trackers": {"x": {"password": "half-written"}}})

    assert client.get("/healthz").status_code == 200

    assert load_config() == config_before
    assert load_state() == state_before
    assert not encrypted_secrets_path().exists()
    assert not root.exists()


def test_committed_site_journal_is_only_cleared(client):
    with services.persistence_lock():
        root, manifest = _open_journal()
        save_config({**load_config(), "trackers": {}})
        manifest["status"] = "committed"
        _write_manifest(root, manifest)

    assert client.get("/healthz").status_code == 200

    assert load_config()["trackers"] == {}
    assert not root.exists()


def _corrupt_unreadable(root, _manifest):
    (root / "MANIFEST.json").write_text("{", encoding="utf-8")


def _corrupt_format(root, manifest):
    manifest["format"] = "something/v0"
    _write_manifest(root, manifest)


def _corrupt_status(root, manifest):
    manifest["status"] = "half-done"
    _write_manifest(root, manifest)


def _corrupt_targets(root, manifest):
    manifest["targets"] = manifest["targets"][:-1]
    _write_manifest(root, manifest)


def _corrupt_target_path(root, manifest):
    _entry(manifest, "config")["target"] = str(root / "elsewhere.yaml")
    _write_manifest(root, manifest)


def _corrupt_backup_name(root, manifest):
    _entry(manifest, "config")["backup"] = "state.bin"
    _write_manifest(root, manifest)


def _corrupt_backup_missing(root, _manifest):
    (root / "config.bin").unlink()


def _corrupt_checksum(root, _manifest):
    (root / "config.bin").write_bytes(b"tampered")


def _corrupt_unexpected_backup(root, manifest):
    _entry(manifest, "secrets")["sha256"] = "0" * 64
    _write_manifest(root, manifest)


def _corrupt_root_is_file(root, _manifest):
    shutil.rmtree(root)
    root.write_text("not a journal", encoding="utf-8")


@pytest.mark.parametrize(
    "corrupt",
    [
        _corrupt_unreadable,
        _corrupt_format,
        _corrupt_status,
        _corrupt_targets,
        _corrupt_target_path,
        _corrupt_backup_name,
        _corrupt_backup_missing,
        _corrupt_checksum,
        _corrupt_unexpected_backup,
        _corrupt_root_is_file,
    ],
)
def test_untrusted_site_journal_blocks_requests_without_touching_stores(client, corrupt):
    _seed(_topic())
    with services.persistence_lock():
        root, manifest = _open_journal()
        changed = {**load_config(), "trackers": {}}
        save_config(changed)
        corrupt(root, manifest)

    response = client.get("/healthz")

    assert response.status_code == 503
    assert "recovery unavailable" in response.text
    assert load_config() == changed  # nothing was half-restored from an untrusted journal
    assert root.exists()


def test_failed_site_commit_restores_every_store(monkeypatch):
    save_state({"topics": [], "mirrors": {"rutor": {"active": "http://rutor.info"}}})
    config_before = load_config()

    def secrets_unwritable(_value):
        raise SecretStoreError("disk full")

    monkeypatch.setattr("tow.store.save_secrets", secrets_unwritable)
    client = TestClient(app, headers=ORIGIN, raise_server_exceptions=False)

    response = client.post("/sites/rutor/delete", follow_redirects=False)

    assert _flash(response) == "изменение сайта не сохранено; всё осталось как было"
    assert load_config() == config_before
    state = load_state()
    assert state["mirrors"]["rutor"]["active"] == "http://rutor.info"
    assert "undo" not in state
    assert not secret_undo_path().exists()
    assert not store_transaction.journal_root().exists()


# --------------------------------------------------------------------------- pending secret-undo cleanup


@pytest.mark.parametrize(
    ("pending", "undo"),
    [
        ({"reference": "settings-v1", "attempts": 0}, {"kind": "settings", "secrets_undo_ref": "settings-v1"}),
        ({"reference": "settings-v1", "attempts": 3}, None),
    ],
    ids=["still-owned-by-live-undo", "retries-exhausted"],
)
def test_pending_secret_undo_cleanup_is_left_alone(client, pending, undo):
    save_secret_undo({"telegram": {"token": "old"}})
    extra = {"secret_undo_cleanup_pending": pending}
    if undo:
        extra["undo"] = {**undo, "ts": iso_now()}
    _seed(**extra)

    assert client.get("/healthz").status_code == 200

    assert load_state()["secret_undo_cleanup_pending"] == pending
    assert secret_undo_path().exists()


def test_pending_secret_undo_cleanup_failure_counts_attempts(client):
    _seed(secret_undo_cleanup_pending={"reference": "unknown-ref", "attempts": "garbage"})

    assert client.get("/healthz").status_code == 200

    pending = load_state()["secret_undo_cleanup_pending"]
    assert pending["attempts"] == 1
    assert pending["last_error"]
    assert pending["ts"]
    assert _events("secret_undo_cleanup_retry_fail")[-1]["status"] == "pending"


def test_pending_secret_undo_cleanup_succeeds(client):
    save_secret_undo({"telegram": {"token": "old"}})
    _seed(secret_undo_cleanup_pending={"reference": "settings-v1", "attempts": 1})

    assert client.get("/healthz").status_code == 200

    assert "secret_undo_cleanup_pending" not in load_state()
    assert not secret_undo_path().exists()
    assert _events("secret_undo_cleanup_retry")[-1]["status"] == "succeeded"
