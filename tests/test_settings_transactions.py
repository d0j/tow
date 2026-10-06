"""Every settings save that writes more than one store is one transaction: a crash after any of
its writes leaves the old set of config, state, secrets and undo snapshot (never a mix).

The crash is staged with the store transaction's seam (``_after_write`` raising a BaseException,
as a killed process would stop); the routes are called as functions so nothing catches it.
"""

from __future__ import annotations

from types import SimpleNamespace

import pytest

from tow import store_transaction
from tow.auth import lan_password_record
from tow.clock import iso_now
from tow.config import load_config, save_config
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
from tow.web import routes_password, routes_settings, routes_sites

PASSWORD = "correct-horse-battery"
LOCAL = SimpleNamespace(client=SimpleNamespace(host="127.0.0.1"), url=SimpleNamespace(scheme="http"))
QBIT = {"qbittorrent": {"host": "127.0.0.1", "port": 8080, "username": "admin", "password": "pw"}}


@pytest.fixture(autouse=True)
def _public_test_hosts(monkeypatch):
    monkeypatch.setattr("tow.web.site_form._resolve_addresses", lambda _name: ["93.184.216.34"])


class Crash(BaseException):
    """The process dies here."""


def _store_bytes() -> dict[str, bytes | None]:
    paths = (config_path(), state_path(), encrypted_secrets_path(), secret_undo_path())
    return {path.name: path.read_bytes() if path.is_file() else None for path in paths}


def _older_settings_undo() -> None:
    """An undo with its own snapshot already there (so the save also replaces it)."""
    state = load_state()
    state["undo"] = {"kind": "settings", "secrets_undo_ref": save_secret_undo({"old": {}}), "ts": iso_now()}
    save_state(state)


def _client_add():
    save_secrets(QBIT)
    _older_settings_undo()
    from tow.web.routes_settings import settings_client_add

    return lambda: settings_client_add(kind="transmission")


def _client_login():
    save_secrets(QBIT)
    return lambda: routes_settings.settings_client(
        client_id="", kind="qbittorrent", host="nas", port="8081", username="admin", password="new-pw"
    )


def _access():
    save_secrets({"lan_auth": lan_password_record(PASSWORD)})
    _older_settings_undo()
    return lambda: routes_settings.settings_access(allow_lan="1")


def _password():
    save_secrets({"telegram": {"token": "kept"}})
    return lambda: routes_password.settings_password(
        LOCAL, current_password="", lan_password=PASSWORD, lan_password2=PASSWORD, hint=""
    )


def _interval():
    save_secrets({"telegram": {"token": "kept"}})
    return lambda: routes_settings.settings_interval(interval_min="120", flash_ttl_min="2")


def _first_start():
    cfg = load_config()
    cfg.pop("setup_done", None)
    save_config(cfg)
    save_secrets({"telegram": {"token": "kept"}})
    return lambda: routes_password.setup_save(
        LOCAL, action="save", lan_password=PASSWORD, lan_password2=PASSWORD, hint="", allow_lan="1"
    )


def _site_add():
    _older_settings_undo()
    form = {
        "name": "newsite",
        "url_regex": r"newsite/(\d+)",
        "fetch_hosts": "https://new.example",
        "download_path": "/download/{id}",
        "from_url": "",
        "username": "user",
        "password": "site-pw",
        "login_path": "",
        "page_download": "",
        "topic_path": "",
        "download_href_regex": "",
    }
    return lambda: routes_sites.sites_new(**form)


def _site_login():
    cfg = load_config()
    cfg["trackers"] = {"rutor": {"title": "rutor", "url_regex": "rutor"}}
    save_config(cfg)
    _older_settings_undo()
    return lambda: routes_sites.sites_login(name="rutor", username="user", password="site-pw")


# save -> (set up and return it, how many stores it writes)
SAVES = {
    "client add": (_client_add, 4),
    "client login": (_client_login, 3),
    "network access": (_access, 3),
    "password": (_password, 3),
    "check interval": (_interval, 4),
    "first start": (_first_start, 2),
    "site add": (_site_add, 4),
    "site login": (_site_login, 3),
}
CASES = [(name, writes) for name, (_prepare, total) in SAVES.items() for writes in range(1, total + 1)]


@pytest.mark.parametrize(("name", "writes"), CASES, ids=[f"{name}-{writes}" for name, writes in CASES])
def test_a_crash_after_any_write_of_a_settings_save_leaves_the_old_set(monkeypatch, name, writes):
    save = SAVES[name][0]()
    before = _store_bytes()
    seen: list[str] = []

    def crash(store_name):
        seen.append(store_name)
        if len(seen) == writes:
            raise Crash(store_name)

    monkeypatch.setattr(store_transaction, "_after_write", crash)
    with pytest.raises(Crash):
        save()
    monkeypatch.setattr(store_transaction, "_after_write", None)
    with persistence_lock():  # the next process: the recovery hook runs first
        pass

    assert _store_bytes() == before


@pytest.mark.parametrize("name", list(SAVES))
def test_each_settings_save_writes_its_stores_in_one_transaction(monkeypatch, name):
    prepare, total = SAVES[name]
    save = prepare()
    seen: list[str] = []
    monkeypatch.setattr(store_transaction, "_after_write", seen.append)

    save()

    assert len(seen) == total, seen
    assert not store_transaction.journal_root().exists()


def test_a_password_save_that_fails_keeps_the_old_password_and_sessions(monkeypatch):
    from tow.auth import issue_session, lan_password_session_key, session_is_valid

    record = lan_password_record("old-horse-battery")
    save_secrets({"lan_auth": record})
    device = issue_session(lan_password_session_key(record))

    def disk_full(_state):
        raise OSError("disk full")

    monkeypatch.setattr("tow.store.save_state", disk_full)
    response = routes_password.settings_password(
        LOCAL, current_password="", lan_password=PASSWORD, lan_password2=PASSWORD, hint=""
    )

    assert response.status_code == 303
    assert load_secrets()["lan_auth"] == record
    assert session_is_valid(device, lan_password_session_key(record))  # nobody was signed out
