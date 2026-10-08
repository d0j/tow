import base64
import json
from pathlib import Path

import pytest

from tow import i18n
from tow.paths import state_path
from tow.store import (
    SecretStoreError,
    encrypted_secrets_path,
    load_secret_undo,
    load_secrets,
    load_state,
    migrate_legacy_secrets,
    save_secret_undo,
    save_secrets,
    save_state,
    secret_store_status,
    secret_undo_path,
)


def _key() -> str:
    return base64.urlsafe_b64encode(b"test-only-tow-master-key-32bytes").decode()


def test_save_and_load_secrets_uses_authenticated_encrypted_file(tmp_path, monkeypatch):
    monkeypatch.setenv("TOW_MASTER_KEY", _key())
    payload = {"qbittorrent": {"username": "fixture-user", "password": "fixture-secret"}}

    save_secrets(payload)

    encrypted = encrypted_secrets_path()
    assert encrypted.is_file()
    assert not encrypted.with_name("secrets.json").exists()
    raw = encrypted.read_text(encoding="utf-8")
    assert "fixture-secret" not in raw
    assert '"password"' not in raw
    assert load_secrets() == payload
    assert secret_store_status()["storage"] == "encrypted"


def test_encrypted_secrets_fail_closed_without_or_with_wrong_key(monkeypatch):
    monkeypatch.setenv("TOW_MASTER_KEY", _key())
    save_secrets({"telegram": {"token": "fixture-token"}})

    monkeypatch.delenv("TOW_MASTER_KEY")
    monkeypatch.delenv("TOW_MASTER_KEY_FILE", raising=False)
    with pytest.raises(SecretStoreError, match="TOW_MASTER_KEY"):
        load_secrets()

    monkeypatch.setenv("TOW_MASTER_KEY", base64.urlsafe_b64encode(b"wrong-key-32bytes-for-test-only!").decode())
    with pytest.raises(SecretStoreError) as wrong:
        load_secrets()
    # Shown as it is by tow check, tow doctor and tow secrets status: the owner's words and
    # what to do, not "cannot decrypt TOW secrets".
    assert str(wrong.value) == i18n.translate("store.secrets_wrong_key", i18n.current())
    assert "tow keys adopt" in str(wrong.value)


def test_tow_check_says_a_secret_store_refusal_in_the_owners_language(monkeypatch, capsys):
    from tow import cli

    monkeypatch.setenv("TOW_MASTER_KEY", _key())
    legacy = encrypted_secrets_path().with_name("secrets.json")
    legacy.parent.mkdir(parents=True, exist_ok=True)
    legacy.write_text(json.dumps({"telegram": {"token": "legacy-token"}}), encoding="utf-8")
    monkeypatch.setattr(i18n, "_CURRENT", i18n.ContextVar("test_language", default=None))
    monkeypatch.setattr(cli, "_use_language", lambda _argv: i18n.use("ru"))

    assert cli.main(["check", "--json"]) == 3

    error = json.loads(capsys.readouterr().out)["error"]
    said = i18n.translate("store.migrate_needed", "ru", file="data/secrets.json", command="tow secrets migrate")
    assert error == said


def test_key_file_is_a_portable_key_source(tmp_path, monkeypatch):
    key_file = tmp_path / "portable-master.key"
    key_file.write_text(_key() + "\n", encoding="ascii")
    monkeypatch.delenv("TOW_MASTER_KEY")  # the env key wins over the file; this test reads the file
    monkeypatch.setenv("TOW_MASTER_KEY_FILE", str(key_file))

    save_secrets({"trackers": {"example": {"username": "u", "password": "p"}}})
    monkeypatch.delenv("TOW_MASTER_KEY_FILE")
    monkeypatch.setenv("TOW_MASTER_KEY", _key())
    assert load_secrets()["trackers"]["example"]["username"] == "u"


def test_legacy_plaintext_requires_explicit_migration(monkeypatch):
    legacy = encrypted_secrets_path().with_name("secrets.json")
    legacy.parent.mkdir(parents=True, exist_ok=True)
    legacy.write_text(json.dumps({"telegram": {"token": "legacy-token"}}), encoding="utf-8")

    with pytest.raises(SecretStoreError, match="migrate"):
        load_secrets()

    monkeypatch.setenv("TOW_MASTER_KEY", _key())
    assert migrate_legacy_secrets() is True
    assert not legacy.exists()
    assert load_secrets()["telegram"]["token"] == "legacy-token"


def test_failed_migration_keeps_legacy_plaintext(monkeypatch):
    legacy = encrypted_secrets_path().with_name("secrets.json")
    legacy.parent.mkdir(parents=True, exist_ok=True)
    legacy.write_text(json.dumps({"telegram": {"token": "legacy-token"}}), encoding="utf-8")
    monkeypatch.setenv("TOW_MASTER_KEY", "not-a-valid-fernet-key")

    with pytest.raises(SecretStoreError):
        migrate_legacy_secrets()
    assert legacy.exists()
    assert not encrypted_secrets_path().exists()


def test_settings_undo_snapshot_is_encrypted_and_not_state_shaped(monkeypatch):
    monkeypatch.setenv("TOW_MASTER_KEY", _key())
    payload = {"clients": {"main": {"password": "fixture-secret"}}}

    ref = save_secret_undo(payload)

    state = {"undo": {"kind": "settings", "secrets_undo_ref": ref}}
    raw_state = json.dumps(state)
    assert "fixture-secret" not in raw_state
    assert "password" not in raw_state
    assert load_secret_undo(ref) == payload


def _undo_now():
    """POST /undo from this computer, as the "Undo" button sends it."""
    from fastapi.testclient import TestClient

    from tow.web import app

    return TestClient(app, headers={"Origin": "http://127.0.0.1"}).post("/undo", follow_redirects=False)


def settings_undo(secrets_before: dict, old_sec: int) -> None:
    """A settings change's undo, written the way the settings routes write it."""
    from tow import store_transaction
    from tow.web.routes_settings import settings_undo as stamp

    state = load_state()
    with store_transaction.transaction() as txn:
        stamp(txn, state, secrets_before, old_sec)
        txn.save_state(state)


def test_web_settings_undo_keeps_secrets_out_of_state(monkeypatch):
    monkeypatch.setenv("TOW_MASTER_KEY", _key())

    payload = {"clients": {"main": {"password": "fixture-secret"}}, "telegram": {"token": "fixture-token"}}
    settings_undo(payload, 3600)

    undo = load_state()["undo"]
    assert undo["kind"] == "settings"
    assert undo["secrets_undo_ref"] == "settings-v1"
    assert "secrets" not in undo
    assert "password" not in json.dumps(undo)
    assert "token" not in json.dumps(undo)
    assert load_secret_undo(undo["secrets_undo_ref"]) == payload


def test_web_undo_restores_encrypted_snapshot_and_consumes_it(monkeypatch):
    monkeypatch.setenv("TOW_MASTER_KEY", _key())

    old = {"telegram": {"token": "old-token"}}
    new = {"telegram": {"token": "new-token"}}
    settings_undo(old, 0)
    save_secrets(new)

    response = _undo_now()

    assert response.status_code == 303
    assert load_secrets() == old
    assert load_state().get("undo") is None
    assert not secret_undo_path().exists()


def test_web_undo_keeps_reference_when_master_key_is_missing(monkeypatch):
    monkeypatch.setenv("TOW_MASTER_KEY", _key())

    settings_undo({"telegram": {"token": "old-token"}}, 0)
    monkeypatch.delenv("TOW_MASTER_KEY")

    response = _undo_now()

    assert response.status_code == 303
    assert load_state()["undo"]["secrets_undo_ref"] == "settings-v1"
    assert secret_undo_path().exists()


def test_load_state_drops_legacy_secret_undo_before_next_write():
    path = state_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_text(
        json.dumps(
            {"topics": [], "mirrors": {}, "undo": {"kind": "settings", "secrets": {"password": "fixture-secret"}}}
        ),
        encoding="utf-8",
    )

    state = load_state()

    assert "undo" not in state
    save_state(state)
    assert "fixture-secret" not in path.read_text(encoding="utf-8")


# --- a write is kept only once it reads back (mutation survivors) ----------------------------


def _legacy_file(data: dict) -> Path:
    legacy = encrypted_secrets_path().with_name("secrets.json")
    legacy.parent.mkdir(parents=True, exist_ok=True)
    legacy.write_text(json.dumps(data), encoding="utf-8")
    return legacy


def _reads_back_other(monkeypatch) -> None:
    from tow import store

    monkeypatch.setattr(store, "_encrypted_payload", lambda _path, _format: {"telegram": {"token": "something else"}})


def test_a_secrets_save_that_reads_back_different_fails_and_keeps_the_old_file(monkeypatch):
    monkeypatch.setenv("TOW_MASTER_KEY", _key())
    legacy = _legacy_file({"telegram": {"token": "legacy-token"}})
    _reads_back_other(monkeypatch)

    with pytest.raises(SecretStoreError) as error:
        save_secrets({"telegram": {"token": "new-token"}})
    assert str(error.value) == i18n.translate("store.readback_mismatch", i18n.current())
    assert legacy.exists()


def test_a_migration_that_reads_back_different_fails_and_keeps_the_plaintext(monkeypatch):
    monkeypatch.setenv("TOW_MASTER_KEY", _key())
    legacy = _legacy_file({"telegram": {"token": "legacy-token"}})
    _reads_back_other(monkeypatch)

    with pytest.raises(SecretStoreError) as error:
        migrate_legacy_secrets()
    assert str(error.value) == i18n.translate("store.readback_mismatch", i18n.current())
    assert legacy.exists()


def test_undecodable_secrets_bytes_are_a_damaged_store_not_a_crash(monkeypatch):
    from tow.store import decrypt_secret_undo_bytes, decrypt_secrets_bytes

    monkeypatch.setenv("TOW_MASTER_KEY", _key())
    for read in (decrypt_secrets_bytes, decrypt_secret_undo_bytes):
        with pytest.raises(SecretStoreError) as error:
            read(b"\xff\xfe{not utf-8")
        assert str(error.value) == i18n.translate("store.secrets_damaged", i18n.current())


def test_a_half_written_key_file_is_removed(tmp_path, monkeypatch):
    import os

    from tow import store

    def full_disk(_fd):
        raise OSError(28, "No space left on device")

    monkeypatch.setattr(os, "fsync", full_disk)
    path = tmp_path / "keys" / "master.key"
    with pytest.raises(SecretStoreError) as error:
        store._write_new_key_file(path, b"k" * 44)
    assert str(error.value) == i18n.translate("cli.keys.error.write_failed", i18n.current())
    assert not path.exists()  # never a half key that looks like another one next time
