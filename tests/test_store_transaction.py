"""One journaled transaction for config, state, secrets and the secret undo snapshot.

A crash is staged with the transaction's test seam (``_after_write``): a BaseException after
the n-th store write stops the request the way a killed process would (nothing restored in
this process); the next process to take the data lock must find every store as it was.
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from tow import journal, site_journal, store_transaction
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
    cfg["trackers"] = {"rutor": {"title": "rutor", "url_regex": r"rutor/(\d+)", "fetch_hosts": ["http://rutor"]}}
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
    monkeypatch.setattr("tow.web.site_form._resolve_addresses", lambda _name: ["93.184.216.34"])
    _seed_site()
    before = _store_bytes()
    _crash_after(monkeypatch, writes)

    with pytest.raises(Crash):
        routes_sites.sites_save(**_SITE_FORM)

    monkeypatch.setattr(store_transaction, "_after_write", None)
    _next_process_takes_the_lock()
    assert _store_bytes() == before


def _unfinished_journal():
    _seed_site()
    original = _store_bytes()
    with persistence_lock():
        root = store_transaction.begin_unlocked()
        config_path().write_bytes(b"changed: true\n")
    return root, original


def test_recovery_never_rereads_a_backup_after_it_has_been_verified(monkeypatch):
    root, original = _unfinished_journal()
    real_write = journal.atomic_write_bytes

    def change_later_backup(path, content):
        if path == config_path():
            (root / "state.bin").write_bytes(b"changed after verification")
        return real_write(path, content)

    monkeypatch.setattr(journal, "atomic_write_bytes", change_later_backup)
    _next_process_takes_the_lock()
    assert _store_bytes() == original
    assert not root.exists()


def test_recovery_preflights_all_store_paths_before_the_first_write():
    root, _ = _unfinished_journal()
    before = config_path().read_bytes()
    state_path().unlink()
    state_path().mkdir()
    with pytest.raises(RuntimeError, match="unsafe"):
        _next_process_takes_the_lock()
    assert config_path().read_bytes() == before
    assert state_path().is_dir()
    assert (root / "MANIFEST.json").exists()


@pytest.mark.parametrize("status", ["prepared", "committed", None])
def test_foreign_files_are_never_removed_with_a_journal(status):
    root, _ = _unfinished_journal()
    marker = root / "MANIFEST.json"
    if status is None:
        marker.unlink()
    else:
        manifest = json.loads(marker.read_bytes())
        manifest["status"] = status
        marker.write_text(json.dumps(manifest), encoding="utf-8")
    (root / "foreign.txt").write_bytes(b"not ours")
    before = _store_bytes()
    with pytest.raises(RuntimeError, match="unexpected"):
        _next_process_takes_the_lock()
    assert (root / "foreign.txt").read_bytes() == b"not ours"
    assert _store_bytes() == before


def test_crash_before_the_marker_only_removes_owned_preparation_files():
    _seed_site()
    original = _store_bytes()
    root = site_journal.journal_root()
    root.mkdir()
    (root / "config.bin").write_bytes(b"partial preparation")
    (root / ".state.bin.fixture.tmp").write_bytes(b"partial write")
    _next_process_takes_the_lock()
    assert _store_bytes() == original
    assert not root.exists()


@pytest.mark.parametrize(
    "mutate",
    [
        lambda m: m["targets"][0].update(key=[]),
        lambda m: m["targets"][0].update(key={}),
        lambda m: m["targets"][0].update(exists="false"),
        lambda m: m["targets"][0].update(sha256=[]),
        lambda m: m.update(targets=[None, *m["targets"][1:]]),
    ],
)
def test_malformed_committed_journal_is_not_silently_deleted(mutate):
    root, _ = _unfinished_journal()
    marker = root / "MANIFEST.json"
    manifest = json.loads(marker.read_bytes())
    manifest["status"] = "committed"
    mutate(manifest)
    marker.write_text(json.dumps(manifest), encoding="utf-8")
    before = _store_bytes()
    with pytest.raises(RuntimeError):
        _next_process_takes_the_lock()
    assert _store_bytes() == before
    assert marker.exists()


@pytest.mark.parametrize("raw", [b"[" * 20000 + b"]" * 20000, b'{"n":' + b"1" * 5000 + b"}"], ids=["deep", "int"])
def test_site_journal_parser_errors_fail_closed(raw):
    root, _ = _unfinished_journal()
    (root / "MANIFEST.json").write_bytes(raw)
    before = _store_bytes()
    with pytest.raises(RuntimeError, match="unreadable"):
        _next_process_takes_the_lock()
    assert _store_bytes() == before


def test_interrupted_cleanup_does_not_restore_over_later_edits(monkeypatch):
    root, original = _unfinished_journal()
    real_unlink = Path.unlink

    def fail_backup_unlink(path, *args, **kwargs):
        if path.name == "state.bin":
            raise PermissionError("locked")
        return real_unlink(path, *args, **kwargs)

    with monkeypatch.context() as patched:
        patched.setattr(Path, "unlink", fail_backup_unlink)
        with pytest.raises(RuntimeError):
            _next_process_takes_the_lock()
    assert _store_bytes() == original
    assert not (root / "MANIFEST.json").exists()
    config_path().write_bytes(b"newer: true\n")
    _next_process_takes_the_lock()
    assert config_path().read_bytes() == b"newer: true\n"
    assert not root.exists()


@pytest.mark.parametrize("name", [site_journal.DIR_NAME, "state.bin", "state.json"])
def test_site_recovery_refuses_reparse_paths_before_any_write(monkeypatch, name):
    import stat
    from types import SimpleNamespace

    root, _ = _unfinished_journal()
    before = _store_bytes()
    real_lstat = Path.lstat

    def reparse(path, *args, **kwargs):
        if path.name == name:
            return SimpleNamespace(st_mode=stat.S_IFREG, st_file_attributes=stat.FILE_ATTRIBUTE_REPARSE_POINT)
        return real_lstat(path, *args, **kwargs)

    monkeypatch.setattr(Path, "lstat", reparse)
    with pytest.raises(RuntimeError, match=r"unsafe|unexpected"):
        _next_process_takes_the_lock()
    assert _store_bytes() == before
    assert (root / "MANIFEST.json").exists()


def test_site_backup_write_is_verified_before_a_transaction_can_start(monkeypatch):
    _seed_site()
    before = _store_bytes()
    from tow import store

    real_write = store.atomic_write_bytes

    def bad_write(path, content):
        return real_write(path, b"bad copy" if path.name == "state.bin" else content)

    monkeypatch.setattr(store, "atomic_write_bytes", bad_write)
    with pytest.raises(RuntimeError, match="backup read-back failed"), store_transaction.transaction():
        pytest.fail("the transaction must not start")
    assert _store_bytes() == before
    assert not site_journal.journal_root().exists()


def test_failed_site_restore_keeps_its_journal_and_can_be_retried(monkeypatch):
    root, original = _unfinished_journal()
    real_write = journal.atomic_write_bytes

    def failing(path, content):
        if path == state_path():
            raise PermissionError("locked")
        return real_write(path, content)

    with monkeypatch.context() as patched:
        patched.setattr(journal, "atomic_write_bytes", failing)
        with pytest.raises(RuntimeError, match="recovery write failed"):
            _next_process_takes_the_lock()
    assert (root / "MANIFEST.json").exists()
    _next_process_takes_the_lock()
    assert _store_bytes() == original
    assert not root.exists()


def test_a_transaction_begun_beside_an_unfinished_one_restores_the_stores_first():
    """Inside the data lock no recovery hook runs: the new transaction itself restores what the
    unfinished one left, before it copies anything."""
    _seed_site()
    original = _store_bytes()
    with persistence_lock():
        root = store_transaction.begin_unlocked()
        save_state({"topics": [{"id": "half-written"}]})  # the unfinished transaction's write
        with store_transaction.transaction():
            assert _store_bytes() == original
        assert _store_bytes() == original
    assert not root.exists()


def test_recover_inside_the_data_lock_restores_an_unfinished_transaction():
    _seed_site()
    original = _store_bytes()
    with persistence_lock():
        root = store_transaction.begin_unlocked()
        config_path().write_bytes(b"changed: true\n")  # the unfinished transaction's write
        store_transaction.recover()  # the lock is held already: no hook ran, recover itself must
        assert _store_bytes() == original
    assert not root.exists()
