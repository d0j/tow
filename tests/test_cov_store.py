"""Behaviour and failure contracts of the persistence layer (store) and the check journal.

Covers: atomic writes that never leave temp files or half-written stores, the
cross-process persistence lock (contention, reentrancy, inherited state, recovery
hooks), encrypted secrets and their master-key sources, legacy plaintext migration,
the encrypted settings-undo snapshot, quarantine of corrupt stores, and the
crash-safe check transaction (recovery at every phase, fail-closed on anything
it cannot prove safe).
"""

from __future__ import annotations

import errno
import hashlib
import json
import os
import sys
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import pytest
from cryptography.fernet import Fernet

from tow import check_transaction, journal, store
from tow.check_transaction import CheckTransactionError, check_store_transaction, recover_check_transaction
from tow.errors import Msg
from tow.paths import data_dir, download_history_path, secrets_path, state_path
from tow.store import (
    SecretStoreError,
    StoreCorruptionError,
    atomic_write_text,
    delete_secret_undo,
    derive_local_secret,
    encrypted_secrets_path,
    generate_master_key,
    load_download_history,
    load_json,
    load_secret_undo,
    load_secrets,
    load_state,
    migrate_legacy_secrets,
    persistence_lock,
    save_download_history,
    save_json,
    save_secret_undo,
    save_secrets,
    save_state,
    secret_store_status,
    secret_undo_path,
)

# --------------------------------------------------------------------------- helpers


@pytest.fixture(autouse=True)
def _no_ambient_master_key(monkeypatch):
    """A master key from the developer's environment must never reach these tests."""
    monkeypatch.delenv("TOW_MASTER_KEY", raising=False)
    monkeypatch.delenv("TOW_MASTER_KEY_FILE", raising=False)


@pytest.fixture
def key(monkeypatch) -> str:
    value = Fernet.generate_key().decode("ascii")
    monkeypatch.setenv("TOW_MASTER_KEY", value)
    return value


@contextmanager
def _refused(code: str) -> Iterator[None]:
    """A secret-store refusal saying the catalog text ``code`` (in whatever language)."""
    with pytest.raises(SecretStoreError) as raised:
        yield
    message = raised.value.args[0]
    assert isinstance(message, Msg), message
    assert message.code == code


def _temp_leftovers(directory: Path) -> list[str]:
    return sorted(p.name for p in directory.iterdir() if p.name.endswith(".tmp"))


def _write_envelope(path: Path, payload, key: str, *, fmt: str = "tow-secrets/v1") -> None:
    token = Fernet(key.encode("ascii")).encrypt(json.dumps(payload).encode("utf-8")).decode("ascii")
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(json.dumps({"format": fmt, "cipher": "fernet", "token": token}), encoding="utf-8")


def _write_legacy(payload) -> Path:
    legacy = secrets_path()
    legacy.write_text(json.dumps(payload), encoding="utf-8")
    return legacy


def _symlink(target: Path, link: Path, *, directory: bool = False) -> None:
    try:
        os.symlink(target, link, target_is_directory=directory)
    except (OSError, NotImplementedError) as exc:  # no symlink privilege on this host
        pytest.skip(f"symlinks unavailable: {exc}")


def _fail_for(name: str, original, exc: BaseException):
    """Wrap a Path method so it raises ``exc`` for one file name only."""

    def wrapper(self, *args, **kwargs):
        if self.name == name:
            raise exc
        return original(self, *args, **kwargs)

    return wrapper


# --------------------------------------------------------------------------- JSON stores


def test_quarantine_failure_keeps_the_corrupt_file_in_place(monkeypatch):
    path = state_path()
    path.write_text("{corrupt", encoding="utf-8")
    monkeypatch.setattr(Path, "replace", _fail_for("state.json", Path.replace, PermissionError("in use")))

    with pytest.raises(StoreCorruptionError, match="unreadable") as excinfo:
        load_state()

    assert isinstance(excinfo.value.__cause__, PermissionError)
    assert path.read_text(encoding="utf-8") == "{corrupt"
    assert not any(p.name.startswith("state.json.corrupt-") for p in path.parent.iterdir())


def test_missing_store_in_missing_directory_returns_default(tmp_path):
    default = {"topics": []}
    assert load_json(tmp_path / "absent-dir" / "state.json", default) is default


def test_quarantined_history_is_never_replaced_by_an_empty_default():
    path = download_history_path()
    path.write_bytes(b"\xff\xfe not utf-8")

    with pytest.raises(StoreCorruptionError, match="unreadable"):
        load_download_history()
    assert not path.exists()
    with pytest.raises(StoreCorruptionError, match="quarantined"):
        load_download_history()


def test_download_history_with_wrong_shape_fails_closed_instead_of_becoming_empty():
    download_history_path().write_text("[1, 2, 3]", encoding="utf-8")
    with pytest.raises(StoreCorruptionError, match="unreadable"):
        load_download_history()
    assert not download_history_path().exists()
    copies = list(download_history_path().parent.glob("download_history.json.corrupt-*"))
    assert len(copies) == 1
    assert copies[0].read_text(encoding="utf-8") == "[1, 2, 3]"

    download_history_path().write_text('{"topics": {"t": {}}}', encoding="utf-8")
    assert load_download_history() == {"schema_version": 1, "topics": {"t": {}}}


@pytest.mark.parametrize("value", [[], None, True, {"topics": {}}, {"topics": [None]}, {"mirrors": []}])
def test_invalid_state_container_never_reaches_callers(value):
    state_path().write_text(json.dumps(value), encoding="utf-8")
    before = state_path().read_bytes()
    with pytest.raises(StoreCorruptionError, match="unreadable"):
        load_state(quarantine=False)
    assert state_path().read_bytes() == before
    with pytest.raises(StoreCorruptionError, match="unreadable"):
        load_state()
    assert not state_path().exists()


@pytest.mark.parametrize("value", [{"topics": []}, {"topics": {"t": None}}, {"topics": {"t": {"items": []}}}])
def test_invalid_history_container_never_reaches_callers(value):
    download_history_path().write_text(json.dumps(value), encoding="utf-8")
    before = download_history_path().read_bytes()
    with pytest.raises(StoreCorruptionError, match="unreadable"):
        load_download_history(quarantine=False)
    assert download_history_path().read_bytes() == before


@pytest.mark.parametrize("kind", ["state", "history"])
def test_invalid_store_structure_is_rejected_before_writing(kind):
    original = _seed()
    writer = save_state if kind == "state" else save_download_history
    payload = {"topics": {}} if kind == "state" else {"topics": []}
    with pytest.raises(StoreCorruptionError, match="malformed"):
        writer(payload)
    assert (state_path().read_bytes(), download_history_path().read_bytes()) == original


def test_save_json_rejects_unserializable_payload_without_touching_the_store():
    save_state({"topics": [{"id": "keep"}]})
    before = state_path().read_bytes()

    with pytest.raises(StoreCorruptionError, match="not serializable"):
        save_json(state_path(), {"topics": [object()]})

    assert state_path().read_bytes() == before
    assert _temp_leftovers(state_path().parent) == []


def test_atomic_write_text_rejects_non_utf8_text_and_writes_nothing(tmp_path):
    target = tmp_path / "note.txt"
    with pytest.raises(StoreCorruptionError, match="not UTF-8"):
        atomic_write_text(target, "lone surrogate \ud800")
    assert not target.exists()
    assert _temp_leftovers(tmp_path) == []


def test_save_state_refuses_plaintext_secret_undo():
    save_state({"topics": [{"id": "keep"}]})
    before = state_path().read_bytes()

    with pytest.raises(SecretStoreError, match="plaintext secret undo"):
        save_state({"topics": [], "undo": {"kind": "settings", "secrets": {"password": "fixture"}}})

    assert state_path().read_bytes() == before


# --------------------------------------------------------------------------- atomic writes


def test_replace_gives_up_after_retries_and_leaves_store_and_no_temp_file(monkeypatch):
    save_state({"topics": [{"id": "old"}]})
    before = state_path().read_bytes()
    sleeps: list[float] = []

    def always_busy(_src, _dst):
        raise PermissionError("sharing violation")

    monkeypatch.setattr(store.os, "replace", always_busy)
    monkeypatch.setattr(store.time, "sleep", sleeps.append)

    with pytest.raises(PermissionError):
        save_state({"topics": [{"id": "new"}]})

    assert len(sleeps) == 9  # 10 attempts, back-off between them
    assert sleeps == sorted(sleeps)
    assert max(sleeps) == 1.0
    assert 4.0 < sum(sleeps) < 5.0  # a reader holding the file a few seconds is waited for
    assert state_path().read_bytes() == before
    assert _temp_leftovers(state_path().parent) == []


def test_failed_temp_cleanup_does_not_mask_the_original_write_error(monkeypatch, tmp_path):
    target = tmp_path / "store.json"

    def replace_fails(_src, _dst):
        raise OSError(errno.EIO, "disk vanished")

    original_unlink = Path.unlink

    def unlink_fails_for_temp(self, *args, **kwargs):
        if self.name.endswith(".tmp"):
            raise PermissionError("antivirus holds the temp file")
        return original_unlink(self, *args, **kwargs)

    monkeypatch.setattr(store.os, "replace", replace_fails)
    monkeypatch.setattr(Path, "unlink", unlink_fails_for_temp)

    with pytest.raises(OSError, match="disk vanished") as excinfo:
        store.atomic_write_bytes(target, b"payload")

    assert not isinstance(excinfo.value, PermissionError)
    assert not target.exists()


def test_atomic_write_fsyncs_and_closes_the_directory_when_it_can_be_opened(monkeypatch, tmp_path):
    target = tmp_path / "store.json"
    stand_in = tmp_path / "directory-stand-in"
    stand_in.write_bytes(b"")
    real_open, real_fsync, real_close = os.open, os.fsync, os.close
    directory_fds: list[int] = []
    fsynced: list[int] = []
    closed: list[int] = []

    def fake_open(path, flags, *args, **kwargs):
        if Path(path) == tmp_path:
            fd = real_open(stand_in, os.O_RDWR)  # Windows cannot fsync a read-only descriptor
            directory_fds.append(fd)
            return fd
        return real_open(path, flags, *args, **kwargs)

    def record_fsync(fd):
        fsynced.append(fd)
        return real_fsync(fd)

    def record_close(fd):
        closed.append(fd)
        return real_close(fd)

    monkeypatch.setattr(store.os, "open", fake_open)
    monkeypatch.setattr(store.os, "fsync", record_fsync)
    monkeypatch.setattr(store.os, "close", record_close)

    store.atomic_write_bytes(target, b"durable")

    assert target.read_bytes() == b"durable"
    assert len(directory_fds) == 1
    assert directory_fds[0] in fsynced
    assert directory_fds[0] in closed


# --------------------------------------------------------------------------- persistence lock


@pytest.mark.skipif(sys.platform != "win32", reason="msvcrt byte-range lock")
def test_lock_waits_while_another_process_holds_it(monkeypatch):
    import msvcrt

    real_locking = msvcrt.locking
    attempts: list[int] = []
    sleeps: list[float] = []

    def contended(fd, mode, nbytes):
        if mode == msvcrt.LK_NBLCK:
            attempts.append(mode)
            if len(attempts) < 3:
                raise OSError(errno.EACCES, "locked by another process")
        return real_locking(fd, mode, nbytes)

    monkeypatch.setattr(msvcrt, "locking", contended)
    monkeypatch.setattr(store.time, "sleep", sleeps.append)

    with persistence_lock():
        save_json(data_dir() / "under-lock.json", {"ok": True})

    assert len(attempts) == 3
    assert sleeps == [0.05, 0.05]
    assert load_json(data_dir() / "under-lock.json", {}) == {"ok": True}


@pytest.mark.skipif(sys.platform != "win32", reason="msvcrt byte-range lock")
def test_unexpected_lock_error_propagates_and_leaves_lock_usable(monkeypatch):
    import msvcrt

    real_locking = msvcrt.locking
    failures = {"left": 1}
    sleeps: list[float] = []

    def broken_once(fd, mode, nbytes):
        if mode == msvcrt.LK_NBLCK and failures["left"]:
            failures["left"] -= 1
            raise OSError(errno.EBADF, "bad descriptor")
        return real_locking(fd, mode, nbytes)

    monkeypatch.setattr(msvcrt, "locking", broken_once)
    monkeypatch.setattr(store.time, "sleep", sleeps.append)

    with pytest.raises(OSError, match="bad descriptor"), persistence_lock():
        pytest.fail("body must not run without the lock")

    assert sleeps == []
    assert getattr(store._PROCESS_LOCK_STATE, "depth", 0) == 0
    with persistence_lock():
        save_state({"topics": [{"id": "after"}]})
    assert load_state()["topics"] == [{"id": "after"}]


def test_lock_state_inherited_from_another_process_is_discarded():
    class InheritedHandle:
        closed = False

        def close(self):
            self.closed = True

    inherited = InheritedHandle()
    lock_state = store._PROCESS_LOCK_STATE
    lock_state.depth = 1
    lock_state.owner_pid = os.getpid() + 1  # as if copied into a forked child
    lock_state.handle = inherited
    try:
        with persistence_lock():
            assert lock_state.owner_pid == os.getpid()
            assert lock_state.handle is not inherited
            save_state({"topics": [{"id": "child"}]})
    finally:
        lock_state.depth = 0
        lock_state.owner_pid = None
        lock_state.handle = None

    assert inherited.closed
    assert load_state()["topics"] == [{"id": "child"}]


def test_recovery_hooks_run_once_per_outermost_lock_and_never_recursively(monkeypatch):
    calls: list[str] = []

    def hook():
        calls.append("hook")
        store._run_recovery_hooks()  # a hook that re-enters recovery must not recurse

    monkeypatch.setattr(store, "_recovery_steps", lambda: [hook])

    with persistence_lock(), persistence_lock():
        pass
    assert calls == ["hook"]

    with persistence_lock():
        pass
    assert calls == ["hook", "hook"]


def test_recovery_never_depends_on_what_the_process_imported(monkeypatch):
    # A check process imported tow.bundle only for its hook and the CLI tow.check_transaction:
    # the store now finds every recovery step itself, when it takes the lock.
    import types

    ran: list[str] = []
    for module, name, _marker in store.RECOVERY_STEPS:
        stand_in = types.ModuleType(module)
        setattr(stand_in, name, lambda module=module: ran.append(module))
        monkeypatch.setitem(sys.modules, module, stand_in)

    with persistence_lock():
        pass
    always = [module for module, _name, marker in store.RECOVERY_STEPS if marker is None]
    assert ran == always  # the night-restore code only when its marker is there

    ran.clear()
    (data_dir() / ".tow-night-restore.json").write_text("{}", encoding="utf-8")
    with persistence_lock():
        pass
    assert ran == [module for module, _name, _marker in store.RECOVERY_STEPS]


def test_every_recovery_step_exists():
    import importlib

    # The store imports each step itself; nothing registers one on import (there is no way to).
    for module, name, _marker in store.RECOVERY_STEPS:
        assert callable(getattr(importlib.import_module(module), name)), (module, name)


def test_the_temporaries_of_a_killed_write_are_removed_when_the_lock_is_taken():
    """A writer killed between its temporary file and the replace left it there forever (the
    state's is a full copy of the state)."""
    from tow.paths import config_path

    data, config_folder = data_dir(), config_path().parent
    stale = [
        data / ".state.json.ab12cd_3.tmp",
        data / ".download_history.json.x9y8z7w6.tmp",
        data / ".secrets.enc.k2j3h4g5.tmp",
        data / ".secrets-undo.enc.q1w2e3r4.tmp",
        config_folder / f".{config_path().name}.a1b2c3d4.tmp",
    ]
    kept = [
        data / ".state.json.fresh123.tmp",  # young: may still be written
        data / ".state.json.AB12CD34.tmp",  # not what tempfile makes
        data / ".state.json.ab12cd34.tmp.bak",
        data / "state.json.ab12cd34.tmp",
        data / ".restore-point-status.json.ab12cd34.tmp",  # not a store
        data / ".state.json..tmp",
    ]
    old = os.path.getmtime(data) - 3600
    for path in stale + kept:
        path.write_bytes(b"{}")
        if path.name != ".state.json.fresh123.tmp":
            os.utime(path, (old, old))
    folder = data / ".state.json.dir12345.tmp"
    folder.mkdir()
    os.utime(folder, (old, old))

    with persistence_lock():
        pass

    assert [path.name for path in stale if path.exists()] == []
    assert [path.name for path in kept if not path.exists()] == []
    assert folder.is_dir()


def test_failing_recovery_hook_blocks_the_writer_but_releases_the_lock(monkeypatch):
    def broken():
        raise RuntimeError("journal unrecoverable")

    monkeypatch.setattr(store, "_recovery_steps", lambda: [broken])
    with pytest.raises(RuntimeError, match="journal unrecoverable"):
        save_state({"topics": [{"id": "must-not-land"}]})
    assert not state_path().exists()

    calls: list[int] = []
    monkeypatch.setattr(store, "_recovery_steps", lambda: [lambda: calls.append(1)])
    save_state({"topics": [{"id": "later"}]})
    assert calls == [1]
    assert load_state()["topics"] == [{"id": "later"}]


# --------------------------------------------------------------------------- master key


def test_secret_store_status_reports_every_storage_and_key_source(monkeypatch, key):
    monkeypatch.delenv("TOW_MASTER_KEY")
    assert secret_store_status() == {"storage": "uninitialized", "key_source": "missing"}

    _write_legacy({"telegram": {"token": "legacy"}})
    monkeypatch.setenv("TOW_MASTER_KEY_FILE", "master.key")
    assert secret_store_status() == {"storage": "legacy_plaintext_migration_required", "key_source": "file"}

    _write_envelope(encrypted_secrets_path(), {"x": 1}, key)
    assert secret_store_status()["storage"] == "blocked_legacy_plaintext"
    with _refused("store.legacy_left"):
        load_secrets()

    monkeypatch.setenv("TOW_MASTER_KEY", key)
    assert secret_store_status()["key_source"] == "env"  # env wins over the file


def test_relative_key_file_resolves_inside_tow_home(monkeypatch):
    generate_master_key(data_dir() / "master.key")
    monkeypatch.setenv("TOW_MASTER_KEY_FILE", "master.key")

    save_secrets({"telegram": {"token": "fixture"}})

    assert load_secrets() == {"telegram": {"token": "fixture"}}
    assert secret_store_status() == {"storage": "encrypted", "key_source": "file"}


@pytest.mark.parametrize(
    ("content", "message"),
    [(None, "store.key_unreadable"), (b"  \n", "store.key_empty")],
)
def test_unusable_key_file_fails_closed(monkeypatch, tmp_path, content, message):
    key_file = tmp_path / "master.key"
    if content is not None:
        key_file.write_bytes(content)
    monkeypatch.setenv("TOW_MASTER_KEY_FILE", str(key_file))

    with _refused(message):
        save_secrets({"telegram": {"token": "fixture"}})
    assert not encrypted_secrets_path().exists()


@pytest.mark.parametrize("bad_key", ["not-a-fernet-key", "ключ-не-ascii"])
def test_invalid_master_key_is_rejected_before_any_write(monkeypatch, bad_key):
    monkeypatch.setenv("TOW_MASTER_KEY", bad_key)
    with _refused("store.key_invalid"):
        save_secrets({"a": 1})
    with _refused("store.key_invalid"):
        derive_local_secret("session")
    assert not encrypted_secrets_path().exists()


def test_derive_local_secret_is_stable_domain_separated_and_key_bound(monkeypatch, key):
    first = derive_local_secret("session")

    assert first == derive_local_secret("session")
    assert first != derive_local_secret("csrf")
    assert key not in first
    assert len(first) == 44

    monkeypatch.setenv("TOW_MASTER_KEY", Fernet.generate_key().decode("ascii"))
    assert derive_local_secret("session") != first


@pytest.mark.parametrize("purpose", ["", "x" * 129, "сессия", None, 7])
def test_derive_local_secret_rejects_bad_purpose(key, purpose):
    with pytest.raises(SecretStoreError, match="invalid local secret purpose"):
        derive_local_secret(purpose)


def test_derive_local_secret_requires_a_master_key():
    with pytest.raises(SecretStoreError, match="TOW_MASTER_KEY"):
        derive_local_secret("session")


def test_generate_master_key_creates_usable_key_and_never_overwrites(monkeypatch, tmp_path):
    path = tmp_path / "keys" / "master.key"
    generate_master_key(path)
    original = path.read_bytes()
    Fernet(original.strip())  # a valid Fernet key

    with pytest.raises(SecretStoreError, match="already exists"):
        generate_master_key(path)
    assert path.read_bytes() == original

    monkeypatch.setenv("TOW_MASTER_KEY_FILE", str(path))
    save_secrets({"a": 1})
    assert load_secrets() == {"a": 1}


def test_generate_master_key_reports_os_errors(monkeypatch, tmp_path):
    import tow.store

    path = tmp_path / "master.key"
    real_open = tow.store.os.open

    def denied(name, *args, **kwargs):
        if str(name).endswith("master.key"):
            raise PermissionError("denied")
        return real_open(name, *args, **kwargs)

    monkeypatch.setattr(tow.store.os, "open", denied)  # the key file is created with os.open (0600)

    with _refused("cli.keys.error.write_failed"):
        generate_master_key(path)
    assert not path.exists()


def test_missing_cryptography_fails_closed(monkeypatch, key):
    save_secrets({"a": 1})
    before = encrypted_secrets_path().read_bytes()
    monkeypatch.setitem(sys.modules, "cryptography.fernet", None)

    with _refused("store.no_cryptography"):
        load_secrets()
    with _refused("store.no_cryptography"):
        save_secrets({"a": 2})
    with _refused("store.no_cryptography"):
        generate_master_key(data_dir() / "new.key")

    assert encrypted_secrets_path().read_bytes() == before
    assert not (data_dir() / "new.key").exists()


# --------------------------------------------------------------------------- encrypted secrets


@pytest.mark.parametrize(
    ("envelope", "message"),
    [
        ("{not json", "store.secrets_damaged"),
        ("[]", "store.secrets_unsupported"),
        ('{"format": "tow-secrets-undo/v1", "cipher": "fernet", "token": "x"}', "store.secrets_unsupported"),
        ('{"format": "tow-secrets/v1", "cipher": "aes", "token": "x"}', "store.secrets_unsupported"),
        ('{"format": "tow-secrets/v1", "cipher": "fernet", "token": ""}', "store.secrets_damaged"),
        ('{"format": "tow-secrets/v1", "cipher": "fernet", "token": 5}', "store.secrets_damaged"),
        ('{"format": "tow-secrets/v1", "cipher": "fernet", "token": "garbage"}', "store.secrets_wrong_key"),
    ],
)
def test_malformed_encrypted_envelope_fails_closed(key, envelope, message):
    encrypted_secrets_path().write_text(envelope, encoding="utf-8")
    with _refused(message):
        load_secrets()
    assert encrypted_secrets_path().read_text(encoding="utf-8") == envelope


def test_decrypted_payload_must_be_an_object(key):
    _write_envelope(encrypted_secrets_path(), ["not", "an", "object"], key)
    with _refused("store.secrets_damaged"):
        load_secrets()


def test_save_secrets_rejects_non_objects_and_unserializable_values(key):
    with pytest.raises(SecretStoreError, match="must be an object"):
        save_secrets(["a"])
    with pytest.raises(SecretStoreError, match="not serializable"):
        save_secrets({"cookie": object()})
    assert not encrypted_secrets_path().exists()


def test_save_secrets_rejects_payload_that_does_not_read_back_identically(key):
    legacy = _write_legacy({"telegram": {"token": "legacy"}})

    with pytest.raises(SecretStoreError, match="not serializable"):
        save_secrets({"ids": (1, 2)})  # a tuple comes back as a list

    assert legacy.exists()  # plaintext is only removed after a verified write
    assert not encrypted_secrets_path().exists()  # invalid input never replaces either store


def test_secret_write_failure_keeps_previous_secrets_and_no_temp_file(monkeypatch, key):
    save_secrets({"telegram": {"token": "old"}})

    def replace_fails(_src, _dst):
        raise OSError(errno.ENOSPC, "disk full")

    real_replace = store.os.replace
    monkeypatch.setattr(store.os, "replace", replace_fails)
    with _refused("store.secrets_write_failed"):
        save_secrets({"telegram": {"token": "new"}})
    monkeypatch.setattr(store.os, "replace", real_replace)

    assert load_secrets() == {"telegram": {"token": "old"}}
    assert _temp_leftovers(encrypted_secrets_path().parent) == []


def test_save_secrets_removes_legacy_plaintext_after_verified_write(key):
    legacy = _write_legacy({"telegram": {"token": "legacy"}})

    save_secrets({"telegram": {"token": "fresh"}})

    assert not legacy.exists()
    assert load_secrets() == {"telegram": {"token": "fresh"}}
    assert "fresh" not in encrypted_secrets_path().read_text(encoding="utf-8")


def test_save_secrets_reports_legacy_plaintext_it_could_not_remove(monkeypatch, key):
    legacy = _write_legacy({"telegram": {"token": "legacy"}})
    monkeypatch.setattr(Path, "unlink", _fail_for("secrets.json", Path.unlink, PermissionError("locked")))

    with _refused("store.legacy_remains"):
        save_secrets({"telegram": {"token": "fresh"}})

    assert legacy.exists()
    assert secret_store_status()["storage"] == "blocked_legacy_plaintext"


# --------------------------------------------------------------------------- legacy migration


def test_migrate_without_legacy_file_is_a_no_op(key):
    assert migrate_legacy_secrets() is False
    assert not encrypted_secrets_path().exists()


def test_migrate_refuses_when_both_stores_exist(key):
    save_secrets({"telegram": {"token": "encrypted"}})
    legacy = _write_legacy({"telegram": {"token": "legacy"}})
    encrypted_before = encrypted_secrets_path().read_bytes()

    with _refused("store.legacy_left"):
        migrate_legacy_secrets()

    assert legacy.exists()
    assert encrypted_secrets_path().read_bytes() == encrypted_before


@pytest.mark.parametrize(
    ("legacy_text", "message"),
    [("{broken", "store.legacy_unreadable"), ('["list"]', "store.legacy_unreadable")],
)
def test_migrate_rejects_bad_legacy_plaintext_and_keeps_it(key, legacy_text, message):
    secrets_path().write_text(legacy_text, encoding="utf-8")

    with _refused(message):
        migrate_legacy_secrets()

    assert secrets_path().read_text(encoding="utf-8") == legacy_text
    assert not encrypted_secrets_path().exists()


def test_migrate_that_cannot_remove_plaintext_fails_closed(monkeypatch, key):
    legacy = _write_legacy({"telegram": {"token": "legacy"}})
    monkeypatch.setattr(Path, "unlink", _fail_for("secrets.json", Path.unlink, PermissionError("locked")))

    with _refused("store.legacy_remains"):
        migrate_legacy_secrets()

    assert legacy.exists()
    with _refused("store.legacy_left"):
        load_secrets()


# --------------------------------------------------------------------------- secret undo snapshot


def test_secret_undo_round_trip_and_idempotent_delete(key):
    ref = save_secret_undo({"clients": {"main": {"password": "fixture"}}})

    assert "fixture" not in secret_undo_path().read_text(encoding="utf-8")
    assert load_secret_undo(ref) == {"clients": {"main": {"password": "fixture"}}}

    delete_secret_undo(ref)
    assert not secret_undo_path().exists()
    delete_secret_undo(ref)  # already gone: not an error
    with _refused("store.secrets_damaged"):
        load_secret_undo(ref)


def test_secret_undo_rejects_unknown_references_and_bad_payloads(key):
    ref = save_secret_undo({"a": 1})

    for call in (load_secret_undo, delete_secret_undo):
        with pytest.raises(SecretStoreError, match="unknown TOW secret undo reference"):
            call("settings-v0")
    with pytest.raises(SecretStoreError, match="must be an object"):
        save_secret_undo("not a dict")
    before = secret_undo_path().read_bytes()
    with pytest.raises(SecretStoreError, match="not serializable"):
        save_secret_undo({"ids": (1,)})
    assert secret_undo_path().read_bytes() == before
    assert ref == "settings-v1"


def test_secret_undo_with_malformed_inner_payload_fails_closed(key):
    _write_envelope(secret_undo_path(), {"secrets": "not-an-object"}, key, fmt="tow-secrets-undo/v1")
    with pytest.raises(SecretStoreError, match="undo payload is malformed"):
        load_secret_undo("settings-v1")


def test_secrets_envelope_is_not_accepted_as_undo_snapshot(key):
    _write_envelope(secret_undo_path(), {"secrets": {}}, key, fmt="tow-secrets/v1")
    with _refused("store.secrets_unsupported"):
        load_secret_undo("settings-v1")


def test_secret_undo_delete_failure_is_reported_and_snapshot_kept(monkeypatch, key):
    ref = save_secret_undo({"a": 1})
    monkeypatch.setattr(Path, "unlink", _fail_for("secrets-undo.enc", Path.unlink, PermissionError("locked")))

    with pytest.raises(SecretStoreError, match="cannot remove TOW secret undo snapshot"):
        delete_secret_undo(ref)
    assert secret_undo_path().exists()


# --------------------------------------------------------------------------- check transaction

_OLD_STATE = {"topics": [{"id": "topic-1", "hash": "OLD"}]}
_NEW_STATE = {"topics": [{"id": "topic-1", "hash": "NEW"}]}
_OLD_HISTORY = {"schema_version": 1, "topics": {}}
_NEW_HISTORY = {"schema_version": 1, "topics": {"topic-1": {"items": {"new": {}}}}}


def _tx_root() -> Path:
    return data_dir() / ".tow-check-transaction"


def _seed() -> tuple[bytes, bytes]:
    save_state(_OLD_STATE)
    save_download_history(_OLD_HISTORY)
    return state_path().read_bytes(), download_history_path().read_bytes()


def _crash(phase: str = "prepared") -> check_transaction.CheckTransaction:
    """Run a check under the lock and 'die' at ``phase``, leaving the journal behind."""
    with persistence_lock():
        transaction = check_transaction._begin_locked()
        save_download_history(_NEW_HISTORY)
        if phase in {"history_committed", "committed"}:
            transaction.mark_history_committed()
        save_state(_NEW_STATE)
        if phase == "committed":
            transaction.mark_committed()
    return transaction


def _edit_marker(mutate) -> None:
    marker = _tx_root() / "TRANSACTION.json"
    manifest = json.loads(marker.read_text(encoding="utf-8"))
    mutate(manifest)
    marker.write_text(json.dumps(manifest), encoding="utf-8")


def test_check_store_transaction_commits_both_stores_and_cleans_up():
    _seed()
    with check_store_transaction() as transaction:
        save_download_history(_NEW_HISTORY)
        transaction.mark_history_committed()
        save_state(_NEW_STATE)
        transaction.mark_committed()

    assert load_state()["topics"] == _NEW_STATE["topics"]
    assert load_download_history() == _NEW_HISTORY
    assert not transaction.cleanup_pending
    assert not _tx_root().exists()


def test_check_store_transaction_rolls_back_both_stores_on_error():
    state_before, history_before = _seed()

    def failing_check():
        with check_store_transaction() as transaction:
            save_download_history(_NEW_HISTORY)
            transaction.mark_history_committed()
            save_state(_NEW_STATE)
            raise ValueError("boom")

    with pytest.raises(ValueError, match="boom"):
        failing_check()

    assert state_path().read_bytes() == state_before
    assert download_history_path().read_bytes() == history_before
    assert not _tx_root().exists()


@pytest.mark.parametrize("phase", ["prepared", "history_committed", "committed"])
def test_recovery_after_crash_at_each_phase(phase):
    state_before, history_before = _seed()
    _crash(phase)
    new_state, new_history = state_path().read_bytes(), download_history_path().read_bytes()

    recover_check_transaction()

    if phase == "committed":
        assert (state_path().read_bytes(), download_history_path().read_bytes()) == (new_state, new_history)
    else:
        assert state_path().read_bytes() == state_before
        assert download_history_path().read_bytes() == history_before
    assert not _tx_root().exists()


def test_recovery_removes_stores_that_did_not_exist_before_the_check():
    assert not state_path().exists()
    assert not download_history_path().exists()
    transaction = _crash("history_committed")
    assert transaction.manifest["targets"]["state"]["before_exists"] is False

    recover_check_transaction()

    assert not state_path().exists()
    assert not download_history_path().exists()
    assert load_state() == {"topics": [], "mirrors": {}}
    assert not _tx_root().exists()


def test_next_writer_recovers_the_journal_before_writing():
    state_before, _ = _seed()
    _crash("prepared")

    save_download_history(_OLD_HISTORY)  # any write takes the lock -> recovery hook

    assert state_path().read_bytes() == state_before
    assert not _tx_root().exists()


def test_journal_without_marker_is_discarded_without_restoring():
    """A crash before the marker was written: the stores were never touched by that check."""
    state_before, _ = _seed()
    save_state(_NEW_STATE)
    state_now = state_path().read_bytes()
    root = _tx_root()
    root.mkdir()
    (root / "state.before").write_bytes(state_before)
    (root / ".state.before.abc123.tmp").write_bytes(b"partial")

    recover_check_transaction()

    assert state_path().read_bytes() == state_now
    assert not root.exists()


@pytest.mark.parametrize(
    ("mutate", "message"),
    [
        (lambda m: m.update(format="other/v9"), "unsupported check transaction marker"),
        (lambda m: m.update(status="half-done"), "malformed check transaction marker"),
        (lambda m: m["targets"].pop("history"), "malformed check transaction marker"),
        (lambda m: m["targets"]["state"].update(path="elsewhere.json"), "target binding mismatch"),
        (lambda m: m["targets"]["state"].update(backup="../state.json"), "backup binding mismatch"),
        (
            lambda m: m["targets"]["history"].update(before_exists=False, backup=None, before_sha256="ab"),
            "malformed check transaction snapshot fingerprint",
        ),
        (lambda m: m.update(status="committed"), "lacks read-back"),
        (
            lambda m: m.update(status="committed", after={"state": {"exists": True}, "history": {}}),
            "malformed committed read-back",
        ),
    ],
)
def test_tampered_marker_fails_closed_and_blocks_writers(mutate, message):
    _seed()
    _crash("prepared")
    _edit_marker(mutate)
    state_now = state_path().read_bytes()

    with pytest.raises(CheckTransactionError, match=message):
        recover_check_transaction()
    with pytest.raises(CheckTransactionError, match=message):
        save_state(_OLD_STATE)  # every writer is blocked until a human resolves it

    assert state_path().read_bytes() == state_now
    assert (_tx_root() / "TRANSACTION.json").exists()


def test_unreadable_marker_fails_closed():
    _seed()
    _crash("prepared")
    (_tx_root() / "TRANSACTION.json").write_bytes(b"{not json")

    with pytest.raises(CheckTransactionError, match="marker is unreadable"):
        recover_check_transaction()
    assert _tx_root().exists()


def test_missing_backup_fails_closed():
    _seed()
    _crash("prepared")
    (_tx_root() / "state.before").unlink()

    with pytest.raises(CheckTransactionError, match="backup is missing"):
        recover_check_transaction()
    assert load_state(quarantine=False)["topics"] == _NEW_STATE["topics"]


@pytest.mark.parametrize("backup", ["state.before", "download_history.before"])
def test_tampered_backup_is_refused_before_either_store_changes(backup):
    _seed()
    _crash("prepared")
    before = (state_path().read_bytes(), download_history_path().read_bytes())
    (_tx_root() / backup).write_bytes(b'{"topics": [], "mirrors": {}}\n')

    with pytest.raises(CheckTransactionError, match="backup checksum mismatch"):
        recover_check_transaction()
    assert (state_path().read_bytes(), download_history_path().read_bytes()) == before
    assert (_tx_root() / "TRANSACTION.json").exists()


@pytest.mark.parametrize(
    "mutate",
    [
        lambda m: m.update(status=[]),
        lambda m: m.update(status={}),
        lambda m: m["targets"]["state"].pop("before_exists"),
        lambda m: m["targets"]["state"].update(before_exists="false"),
        lambda m: m["targets"]["state"].update(before_exists=1),
        lambda m: m["targets"]["history"].update(before_sha256=None),
        lambda m: m["targets"]["history"].update(before_sha256="z" * 64),
        lambda m: m["targets"]["history"].update(before_sha256=[]),
        lambda m: m["after"]["state"].update(exists="false"),
        lambda m: m["after"]["history"].update(sha256="invalid"),
        lambda m: m["after"]["history"].update(exists=False),
    ],
)
def test_invalid_marker_types_leave_both_stores_and_journal_unchanged(mutate):
    _seed()
    _crash("committed")
    _edit_marker(mutate)
    before = (state_path().read_bytes(), download_history_path().read_bytes())
    marker = (_tx_root() / "TRANSACTION.json").read_bytes()
    with pytest.raises(CheckTransactionError):
        recover_check_transaction()
    assert (state_path().read_bytes(), download_history_path().read_bytes()) == before
    assert (_tx_root() / "TRANSACTION.json").read_bytes() == marker


@pytest.mark.parametrize(
    "raw", [b"[" * 20000 + b"]" * 20000, b'{"number":' + b"1" * 5000 + b"}"], ids=["deep", "large-number"]
)
def test_marker_parser_limits_are_reported_as_recovery_errors(raw):
    _seed()
    _crash("prepared")
    (_tx_root() / "TRANSACTION.json").write_bytes(raw)
    before = (state_path().read_bytes(), download_history_path().read_bytes())
    with pytest.raises(CheckTransactionError, match="marker is unreadable"):
        recover_check_transaction()
    assert (state_path().read_bytes(), download_history_path().read_bytes()) == before


@pytest.mark.parametrize("name", ["state.before", "TRANSACTION.json", ".state.before.test.tmp"])
def test_journal_directories_with_allowed_names_are_not_deleted(name):
    root = _tx_root()
    root.mkdir()
    (root / name).mkdir()
    with pytest.raises(CheckTransactionError, match="unexpected entry"):
        recover_check_transaction()
    assert (root / name).is_dir()


def test_recovery_preflights_all_targets_before_restoring_the_first():
    _seed()
    _crash("prepared")
    state_before = state_path().read_bytes()
    download_history_path().unlink()
    download_history_path().mkdir()
    with pytest.raises(CheckTransactionError, match="not a regular file"):
        recover_check_transaction()
    assert state_path().read_bytes() == state_before
    assert download_history_path().is_dir()
    assert (_tx_root() / "TRANSACTION.json").exists()


def test_failed_journal_reservation_never_deletes_another_directory(monkeypatch):
    _seed()
    real_mkdir = Path.mkdir
    root = _tx_root()

    def racing_mkdir(path, *args, **kwargs):
        if path == root:
            real_mkdir(path)
            (path / "foreign.txt").write_bytes(b"not ours")
            raise FileExistsError("reserved by another writer")
        return real_mkdir(path, *args, **kwargs)

    monkeypatch.setattr(Path, "mkdir", racing_mkdir)
    with pytest.raises(CheckTransactionError, match="cannot prepare"), check_store_transaction():
        pytest.fail("the check must not start")
    assert (_tx_root() / "foreign.txt").read_bytes() == b"not ours"


def test_recovery_uses_verified_bytes_even_if_a_backup_changes_later(monkeypatch):
    original = _seed()
    _crash("prepared")
    real_write = journal.atomic_write_bytes

    def changing_backup(path, content):
        if path == state_path():
            (_tx_root() / "download_history.before").write_bytes(b"corrupted after preflight")
        return real_write(path, content)

    monkeypatch.setattr(journal, "atomic_write_bytes", changing_backup)
    recover_check_transaction()
    assert (state_path().read_bytes(), download_history_path().read_bytes()) == original
    assert not _tx_root().exists()


def test_failed_second_restore_can_be_retried_with_the_complete_journal(monkeypatch):
    original = _seed()
    _crash("prepared")
    real_write = journal.atomic_write_bytes
    with monkeypatch.context() as patched:
        patched.setattr(
            journal,
            "atomic_write_bytes",
            _fail_for("download_history.json", real_write, PermissionError("locked")),
        )
        with pytest.raises(CheckTransactionError, match="cannot restore check store"):
            recover_check_transaction()
        assert (_tx_root() / "TRANSACTION.json").exists()
    recover_check_transaction()
    assert (state_path().read_bytes(), download_history_path().read_bytes()) == original
    assert not _tx_root().exists()


def test_bad_restore_write_readback_keeps_the_journal(monkeypatch):
    _seed()
    _crash("prepared")
    real_write = journal.atomic_write_bytes

    def corrupt_write(path, content):
        return real_write(path, b"bad write" if path == state_path() else content)

    monkeypatch.setattr(journal, "atomic_write_bytes", corrupt_write)
    with pytest.raises(CheckTransactionError, match="restore read-back mismatch"):
        recover_check_transaction()
    assert (_tx_root() / "TRANSACTION.json").exists()


def test_bad_backup_write_is_rejected_before_the_check_starts(monkeypatch):
    original = _seed()
    real_write = check_transaction.atomic_write_bytes

    def corrupt_write(path, content):
        return real_write(path, b"bad copy" if path.name == "download_history.before" else content)

    monkeypatch.setattr(check_transaction, "atomic_write_bytes", corrupt_write)
    with pytest.raises(CheckTransactionError, match="backup checksum mismatch"), check_store_transaction():
        pytest.fail("the check must not start")
    assert (state_path().read_bytes(), download_history_path().read_bytes()) == original
    assert not _tx_root().exists()


@pytest.mark.parametrize("name", [".tow-check-transaction", "state.before", "state.json"])
def test_reparse_paths_are_refused_without_store_changes(monkeypatch, name):
    import stat
    from types import SimpleNamespace

    _seed()
    _crash("prepared")
    before = (state_path().read_bytes(), download_history_path().read_bytes())
    real_lstat = Path.lstat

    def reparse(path, *args, **kwargs):
        if path.name == name:
            return SimpleNamespace(st_mode=stat.S_IFREG, st_file_attributes=stat.FILE_ATTRIBUTE_REPARSE_POINT)
        return real_lstat(path, *args, **kwargs)

    monkeypatch.setattr(Path, "lstat", reparse)
    with pytest.raises(CheckTransactionError):
        recover_check_transaction()
    assert (state_path().read_bytes(), download_history_path().read_bytes()) == before


def test_unreadable_backup_fails_closed(monkeypatch):
    _seed()
    _crash("prepared")
    monkeypatch.setattr(Path, "read_bytes", _fail_for("state.before", Path.read_bytes, PermissionError("locked")))

    with pytest.raises(CheckTransactionError, match=r"cannot read check store: state\.before"):
        recover_check_transaction()
    assert _tx_root().exists()


def test_new_store_that_cannot_be_removed_fails_closed(monkeypatch):
    _crash("prepared")  # neither store existed before the check
    monkeypatch.setattr(Path, "unlink", _fail_for("download_history.json", Path.unlink, PermissionError("locked")))

    with pytest.raises(CheckTransactionError, match=r"cannot remove new check store: download_history\.json"):
        recover_check_transaction()
    assert _tx_root().exists()


def test_transaction_directory_that_is_a_file_fails_closed():
    _tx_root().write_bytes(b"not a directory")
    with pytest.raises(CheckTransactionError, match="directory is invalid"):
        recover_check_transaction()
    assert _tx_root().read_bytes() == b"not a directory"


def test_transaction_directory_that_cannot_be_listed_fails_closed(monkeypatch):
    _seed()
    _crash("prepared")
    monkeypatch.setattr(Path, "iterdir", _fail_for(".tow-check-transaction", Path.iterdir, PermissionError("denied")))
    with pytest.raises(CheckTransactionError, match="cannot inspect"):
        recover_check_transaction()


def test_symlinked_transaction_directory_is_refused(tmp_path):
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    (elsewhere / "TRANSACTION.json").write_text("{}", encoding="utf-8")
    _symlink(elsewhere, _tx_root(), directory=True)

    with pytest.raises(CheckTransactionError, match="must not be a symlink"):
        recover_check_transaction()
    assert (elsewhere / "TRANSACTION.json").read_text(encoding="utf-8") == "{}"


def test_symlink_inside_transaction_directory_is_refused(tmp_path):
    _seed()
    _crash("prepared")
    outside = tmp_path / "outside.json"
    outside.write_text("{}", encoding="utf-8")
    (_tx_root() / "state.before").unlink()
    _symlink(outside, _tx_root() / "state.before")

    with pytest.raises(CheckTransactionError, match="unexpected entry"):
        recover_check_transaction()
    assert outside.read_text(encoding="utf-8") == "{}"


def test_symlinked_store_is_refused_before_the_transaction_starts(tmp_path):
    save_download_history(_OLD_HISTORY)
    real_state = tmp_path / "real-state.json"
    real_state.write_text(json.dumps(_OLD_STATE), encoding="utf-8")
    _symlink(real_state, state_path())

    with pytest.raises(CheckTransactionError, match=r"must not be a symlink: state\.json"), check_store_transaction():
        pytest.fail("the check must not start")

    assert not _tx_root().exists()
    assert json.loads(real_state.read_text(encoding="utf-8")) == _OLD_STATE


def test_store_that_is_a_directory_is_refused():
    state_path().mkdir()

    with pytest.raises(CheckTransactionError, match=r"not a regular file: state\.json"), check_store_transaction():
        pytest.fail("the check must not start")
    assert not _tx_root().exists()


def test_unreadable_store_is_refused(monkeypatch):
    _seed()
    monkeypatch.setattr(Path, "read_bytes", _fail_for("state.json", Path.read_bytes, PermissionError("locked")))

    with pytest.raises(CheckTransactionError, match=r"cannot read check store: state\.json"), check_store_transaction():
        pytest.fail("the check must not start")
    assert not _tx_root().exists()


def test_backup_write_failure_aborts_preparation_cleanly(monkeypatch):
    state_before, history_before = _seed()
    real_write = check_transaction.atomic_write_bytes

    def failing(path, content):
        if path.name == "download_history.before":
            raise OSError(errno.ENOSPC, "disk full")
        return real_write(path, content)

    monkeypatch.setattr(check_transaction, "atomic_write_bytes", failing)

    with pytest.raises(CheckTransactionError, match="cannot prepare") as excinfo, check_store_transaction():
        pytest.fail("the check must not start")

    assert isinstance(excinfo.value.__cause__, OSError)
    assert not _tx_root().exists()
    assert state_path().read_bytes() == state_before
    assert download_history_path().read_bytes() == history_before


def test_unserializable_marker_update_keeps_the_previous_durable_marker():
    state_before, _ = _seed()
    with persistence_lock():
        transaction = check_transaction._begin_locked()
        save_state(_NEW_STATE)
        transaction.manifest["note"] = object()
        with pytest.raises(CheckTransactionError, match="not serializable"):
            transaction.mark_history_committed()

    marker = json.loads((_tx_root() / "TRANSACTION.json").read_text(encoding="utf-8"))
    assert marker["status"] == "prepared"
    recover_check_transaction()
    assert state_path().read_bytes() == state_before


def test_beginning_inside_the_lock_recovers_a_leftover_journal_first():
    state_before, _ = _seed()
    with persistence_lock():
        check_transaction._begin_locked()
        save_state(_NEW_STATE)  # first check "dies" here, still under the same lock
        second = check_transaction._begin_locked()

        assert state_path().read_bytes() == state_before
        assert second.manifest["status"] == "prepared"
        assert second.manifest["targets"]["state"]["before_sha256"] == hashlib.sha256(state_before).hexdigest()
        assert second.finish() is True
    assert not _tx_root().exists()


def test_failed_cleanup_is_reported_and_finished_by_the_next_lock(monkeypatch):
    _seed()
    real_rmdir = Path.rmdir
    monkeypatch.setattr(Path, "rmdir", _fail_for(".tow-check-transaction", real_rmdir, PermissionError("antivirus")))
    with check_store_transaction() as transaction:
        save_state(_NEW_STATE)
        transaction.mark_committed()

    assert transaction.cleanup_pending is True
    assert _tx_root().exists()
    with pytest.raises(CheckTransactionError, match="cannot remove check transaction directory"):
        recover_check_transaction()

    monkeypatch.setattr(Path, "rmdir", real_rmdir)
    save_download_history(_NEW_HISTORY)  # the next writer finishes the cleanup first

    assert not _tx_root().exists()
    assert load_state()["topics"] == _NEW_STATE["topics"]


def test_failed_rollback_raises_a_chained_error_and_keeps_the_journal():
    _seed()

    def check_that_crashes_after_losing_its_backup():
        with check_store_transaction() as transaction:
            save_state(_NEW_STATE)
            (transaction.root / "state.before").unlink()
            raise ValueError("check crashed")

    with pytest.raises(CheckTransactionError, match="recovery failed") as excinfo:
        check_that_crashes_after_losing_its_backup()

    cause = excinfo.value.__cause__
    assert isinstance(cause, CheckTransactionError)
    assert "cannot restore check store: state.json" in str(cause)
    assert (_tx_root() / "TRANSACTION.json").exists()
