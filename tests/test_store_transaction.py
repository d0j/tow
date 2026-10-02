"""One journaled transaction for config, state, secrets and the secret undo snapshot.

A crash is staged with the transaction's test seam (``_after_write``): a BaseException after
the n-th store write stops the request the way a killed process would (nothing restored in
this process); the next process to take the data lock must find every store as it was.
"""

from __future__ import annotations

import pytest

from tow import site_journal, store_transaction
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
from tow.web import routes_sites


class Crash(BaseException):
    """The process dies here (not an Exception: nothing in TOW may catch it)."""


def _store_bytes() -> dict[str, bytes | None]:
    paths = {
        "config": config_path(),
        "state": state_path(),
        "secrets": encrypted_secrets_path(),
        "secret_undo": secret_undo_path(),
    }
    return {name: path.read_bytes() if path.is_file() else None for name, path in paths.items()}


def _crash_after(monkeypatch, writes: int) -> list[str]:
    seen: list[str] = []

    def after_write(name: str) -> None:
        seen.append(name)
        if len(seen) == writes:
            raise Crash(name)

    monkeypatch.setattr(store_transaction, "_after_write", after_write)
    return seen


def _next_process_takes_the_lock() -> None:
    """What any TOW process does first: the recovery hook runs at the outermost lock."""
    with persistence_lock():
        pass


def _seed_site() -> None:
    cfg = load_config()
    cfg["trackers"] = {"rutor": {"title": "rutor", "url_regex": "rutor", "fetch_hosts": ["http://rutor"]}}
    save_config(cfg)
    save_state({"topics": [], "mirrors": {"rutor": {"active": "http://rutor"}}})
    save_secrets({"trackers": {"rutor": {"username": "fixture-user", "password": "fixture-secret"}}})
    save_secret_undo({"older": {"token": "older-undo"}})


def test_commit_writes_every_store_and_leaves_no_journal():
    save_secrets({"a": {"x": 1}})

    store_transaction.commit(config={**load_config(), "bind": "0.0.0.0"}, state={"topics": []}, secrets={"b": {}})

    assert load_config()["bind"] == "0.0.0.0"
    assert load_secrets() == {"b": {}}
    assert not site_journal.journal_root().exists()


def test_a_failure_inside_restores_every_store_byte_for_byte():
    save_secrets({"a": {"x": 1}})
    save_secret_undo({"a": {"x": 0}})
    before = _store_bytes()

    def half_written():
        with store_transaction.transaction() as txn:
            txn.save_secrets({"a": {"x": 2}})
            txn.save_secret_undo({"new": {}})
            txn.save_config({**load_config(), "bind": "0.0.0.0"})
            txn.save_state({"topics": [{"id": "half"}]})
            raise OSError("disk full")

    with pytest.raises(store_transaction.TransactionError):
        half_written()

    assert _store_bytes() == before
    assert not site_journal.journal_root().exists()


def test_a_restore_that_fails_too_says_so_and_keeps_the_journal(monkeypatch):
    def broken(_root, _targets):
        raise RuntimeError("journal unreadable")

    def failing():
        with store_transaction.transaction() as txn:
            txn.save_state({"topics": []})
            monkeypatch.setattr(site_journal, "recover_unlocked", broken)
            raise OSError("disk full")

    with pytest.raises(store_transaction.RollbackError):
        failing()

    assert site_journal.journal_root().exists()


def test_a_committed_transaction_is_not_reported_as_failed_when_its_journal_lingers(monkeypatch):
    def cannot_remove(_root):
        raise OSError("journal folder in use")

    with monkeypatch.context() as patched:
        patched.setattr(site_journal, "remove_journal", cannot_remove)
        store_transaction.commit(state={"topics": [{"id": "kept"}]})  # no error: it did commit

    _next_process_takes_the_lock()  # the committed journal is only cleared
    assert [topic["id"] for topic in load_state()["topics"]] == ["kept"]
    assert not site_journal.journal_root().exists()


@pytest.mark.parametrize("writes", [1, 2, 3, 4])
def test_a_crash_in_the_middle_of_a_site_delete_leaves_the_old_set(monkeypatch, writes):
    _seed_site()
    before = _store_bytes()
    seen = _crash_after(monkeypatch, writes)

    with pytest.raises(Crash):
        routes_sites.sites_delete("rutor")

    assert len(seen) == writes
    assert site_journal.journal_root().exists()  # the process died with the journal open
    monkeypatch.setattr(store_transaction, "_after_write", None)
    _next_process_takes_the_lock()
    assert _store_bytes() == before
    assert not site_journal.journal_root().exists()


def test_a_site_delete_that_runs_through_writes_all_four_stores(monkeypatch):
    _seed_site()
    seen = _crash_after(monkeypatch, 99)

    routes_sites.sites_delete("rutor")

    assert sorted(seen) == ["config", "secret_undo", "secrets", "state"]
    assert "rutor" not in load_config()["trackers"]
    assert "rutor" not in load_secrets()["trackers"]
    assert load_state()["undo"]["kind"] == "site"


_SITE_FORM = {
    "name": "rutor",
    "fetch_hosts": "http://rutor",
    "login_hosts": None,
    "login_hosts_present": "",
    "url_regex": "",
    "download_path": "",
    "login_path": "",
    "topic_path": "",
    "page_download": "",
    "download_href_regex": "",
    "new_name": "rutor2",
    "username": "other",
    "password": "other-pw",
}


@pytest.mark.parametrize("writes", [1, 2, 3, 4])
def test_a_crash_in_the_middle_of_a_site_save_leaves_the_old_set(monkeypatch, writes):
    _seed_site()
    before = _store_bytes()
    _crash_after(monkeypatch, writes)

    with pytest.raises(Crash):
        routes_sites.sites_save(**_SITE_FORM)

    monkeypatch.setattr(store_transaction, "_after_write", None)
    _next_process_takes_the_lock()
    assert _store_bytes() == before
