"""The encrypted secrets and the settings undo snapshot: Fernet envelopes under the master key.

Also the two ``tow keys`` operations that must look at those envelopes first:
``generate_master_key`` (never beside existing secrets) and ``adopt_master_key`` (only a key that
opens them).
"""

from __future__ import annotations

from contextlib import suppress
from pathlib import Path
from typing import Any

from tow.errors import Msg
from tow.paths import key_file, secrets_path
from tow.storage.atomic import atomic_write_bytes, restrict_file
from tow.storage.errors import SecretStoreError
from tow.storage.jsonio import decode_json_bytes, encode_json
from tow.storage.keys import (
    MasterKeyError,
    key_file_to_adopt,
    key_source,
    master_fernet,
    read_key_file,
    write_new_key_file,
)
from tow.storage.locks import persistence_lock

_SECRETS_FORMAT = "tow-secrets/v1"
_UNDO_FORMAT = "tow-secrets-undo/v1"
_SETTINGS_UNDO_REF = "settings-v1"


def encrypted_secrets_path() -> Path:
    return secrets_path().with_name("secrets.enc")


def secret_undo_path() -> Path:
    return secrets_path().with_name("secrets-undo.enc")


def _json_bytes(data: Any) -> bytes:
    try:
        encoded = encode_json(data, compact=True)
        if decode_json_bytes(encoded) != data:
            raise ValueError("secret JSON would change the payload")
        return encoded
    except (TypeError, ValueError, UnicodeError, RecursionError) as exc:
        raise SecretStoreError("TOW secret payload is not serializable") from exc


def _write_bytes_atomic(path: Path, content: bytes) -> None:
    try:
        atomic_write_bytes(path, content)
        restrict_file(path)
    except OSError as exc:
        raise SecretStoreError(Msg("store.secrets_write_failed")) from exc


def _encrypted_payload(path: Path, expected_format: str) -> dict[str, Any]:
    try:
        text = path.read_text(encoding="utf-8")
    except (OSError, UnicodeError) as exc:
        raise SecretStoreError(Msg("store.secrets_damaged")) from exc
    return _decrypted_envelope(text, expected_format)


def decrypt_secrets_bytes(content: bytes) -> dict[str, Any]:
    """The secrets of a secrets.enc file's bytes (a night copy's, before it is written back)."""
    return _decrypt_envelope_bytes(content, _SECRETS_FORMAT)


def decrypt_secret_undo_bytes(content: bytes) -> dict[str, Any]:
    """Validate encrypted settings undo without writing or quarantining any file."""
    return _secret_undo_data(_decrypt_envelope_bytes(content, _UNDO_FORMAT))


def _decrypt_envelope_bytes(content: bytes, expected_format: str) -> dict[str, Any]:
    try:
        text = content.decode("utf-8")
    except UnicodeError as exc:
        raise SecretStoreError(Msg("store.secrets_damaged")) from exc
    return _decrypted_envelope(text, expected_format)


def encrypt_secrets_bytes(data: dict[str, Any]) -> bytes:
    """secrets.enc bytes for ``data``, exactly as ``save_secrets`` writes them."""
    if not isinstance(data, dict):
        raise SecretStoreError("TOW secrets must be an object")
    token = master_fernet().encrypt(_json_bytes(data)).decode("ascii")
    return _json_bytes({"format": _SECRETS_FORMAT, "cipher": "fernet", "token": token}) + b"\n"


def _decrypted_envelope(text: str, expected_format: str, fernet: Any = None) -> dict[str, Any]:
    try:
        envelope = decode_json_bytes(text.encode("utf-8"))
    except (UnicodeError, ValueError, RecursionError) as exc:
        raise SecretStoreError(Msg("store.secrets_damaged")) from exc
    if not isinstance(envelope, dict) or envelope.get("format") != expected_format:
        raise SecretStoreError(Msg("store.secrets_unsupported"))
    if envelope.get("cipher") != "fernet":
        raise SecretStoreError(Msg("store.secrets_unsupported"))
    token = envelope.get("token")
    if not isinstance(token, str) or not token:
        raise SecretStoreError(Msg("store.secrets_damaged"))
    try:
        from cryptography.fernet import InvalidToken
    except ImportError as exc:
        raise SecretStoreError(Msg("store.no_cryptography")) from exc
    try:
        decoded = (fernet or master_fernet()).decrypt(token.encode("ascii"))
        payload = decode_json_bytes(decoded)
    except (InvalidToken, UnicodeError, ValueError, TypeError, RecursionError) as exc:
        raise SecretStoreError(Msg("store.secrets_wrong_key")) from exc
    if not isinstance(payload, dict):
        raise SecretStoreError(Msg("store.secrets_damaged"))
    return payload


def _write_encrypted(path: Path, data: dict[str, Any], envelope_format: str) -> None:
    token = master_fernet().encrypt(_json_bytes(data)).decode("ascii")
    envelope = {"format": envelope_format, "cipher": "fernet", "token": token}
    _write_bytes_atomic(path, _json_bytes(envelope) + b"\n")


def _read_legacy() -> dict[str, Any]:
    try:
        data = decode_json_bytes(secrets_path().read_bytes())
    except (OSError, UnicodeError, ValueError, RecursionError) as exc:
        raise SecretStoreError(
            Msg("store.legacy_unreadable", file="data/secrets.json", command="tow secrets migrate")
        ) from exc
    if not isinstance(data, dict):
        raise SecretStoreError(Msg("store.legacy_unreadable", file="data/secrets.json", command="tow secrets migrate"))
    return data


def secret_store_status() -> dict[str, str]:
    encrypted = encrypted_secrets_path().is_file()
    legacy = secrets_path().is_file()
    if encrypted and legacy:
        storage = "blocked_legacy_plaintext"
    elif encrypted:
        storage = "encrypted"
    elif legacy:
        storage = "legacy_plaintext_migration_required"
    else:
        storage = "uninitialized"
    return {"storage": storage, "key_source": key_source()}


def load_secrets() -> dict[str, Any]:
    encrypted = encrypted_secrets_path()
    legacy = secrets_path()
    if encrypted.is_file():
        if legacy.is_file():
            raise SecretStoreError(Msg("store.legacy_left", file="data/secrets.json", encrypted="secrets.enc"))
        return _encrypted_payload(encrypted, _SECRETS_FORMAT)
    if legacy.is_file():
        raise SecretStoreError(Msg("store.migrate_needed", file="data/secrets.json", command="tow secrets migrate"))
    return {}


def save_secrets(data: dict[str, Any]) -> None:
    if not isinstance(data, dict):
        raise SecretStoreError("TOW secrets must be an object")
    encrypted = encrypted_secrets_path()
    _write_encrypted(encrypted, data, _SECRETS_FORMAT)
    if _encrypted_payload(encrypted, _SECRETS_FORMAT) != data:
        raise SecretStoreError(Msg("store.readback_mismatch"))
    legacy = secrets_path()
    if legacy.is_file():
        try:
            legacy.unlink()
        except OSError as exc:
            raise SecretStoreError(
                Msg("store.legacy_remains", file="data/secrets.json", encrypted="secrets.enc")
            ) from exc


def migrate_legacy_secrets() -> bool:
    legacy = secrets_path()
    encrypted = encrypted_secrets_path()
    if not legacy.is_file():
        return False
    if encrypted.is_file():
        raise SecretStoreError(Msg("store.legacy_left", file="data/secrets.json", encrypted="secrets.enc"))
    data = _read_legacy()
    _write_encrypted(encrypted, data, _SECRETS_FORMAT)
    if _encrypted_payload(encrypted, _SECRETS_FORMAT) != data:
        raise SecretStoreError(Msg("store.readback_mismatch"))
    try:
        legacy.unlink()
    except OSError as exc:
        raise SecretStoreError(Msg("store.legacy_remains", file="data/secrets.json", encrypted="secrets.enc")) from exc
    return True


def generate_master_key(path: Path | None = None) -> Path:
    """A new master key in ``path`` (by default the install's keys/master.key); returns the path.

    The default place is refused while encrypted secrets exist: they belong to the key in use,
    and a new key there would take over once TOW_MASTER_KEY_FILE is gone (``tow keys adopt``
    moves that key in instead).
    """
    try:
        from cryptography.fernet import Fernet
    except ImportError as exc:
        raise SecretStoreError(Msg("store.no_cryptography")) from exc
    if path is None:
        if encrypted_secrets_path().is_file():
            raise MasterKeyError("encrypted secrets exist: adopt their key instead", kind="secrets_exist")
        path = key_file()
    path = Path(path)
    if path.exists():
        raise MasterKeyError("TOW master key file already exists", kind="exists")
    write_new_key_file(path, Fernet.generate_key())
    return path


def _proven_secret_files(key: bytes) -> int:
    """How many encrypted stores ``key`` opens; MasterKeyError when it is not their key."""
    try:
        from cryptography.fernet import Fernet

        fernet = Fernet(key)
    except ImportError as exc:
        raise SecretStoreError(Msg("store.no_cryptography")) from exc
    except (TypeError, ValueError) as exc:
        raise MasterKeyError("not a TOW master key", kind="invalid") from exc
    checked = 0
    for path, envelope_format in ((encrypted_secrets_path(), _SECRETS_FORMAT), (secret_undo_path(), _UNDO_FORMAT)):
        if not path.is_file():
            continue
        try:
            _decrypted_envelope(path.read_text(encoding="utf-8"), envelope_format, fernet)
        except (OSError, UnicodeError, SecretStoreError) as exc:
            raise MasterKeyError("the key does not open the current secrets", kind="wrong_key") from exc
        checked += 1
    return checked


def adopt_master_key(source: Path | None = None) -> dict[str, Any]:
    """Copy an existing key file into the install (keys/master.key) once it is proven to open the
    current secrets. ``source`` defaults to TOW_MASTER_KEY_FILE. The key itself is never returned,
    printed or logged; a different key already in keys/ is never replaced.
    """
    source = Path(source) if source is not None else key_file_to_adopt()
    if source is None:
        raise MasterKeyError("no key file to adopt", kind="no_source")
    target = key_file()
    try:
        key = read_key_file(source)
    except SecretStoreError as exc:
        raise MasterKeyError("the key file cannot be read", kind="unreadable") from exc
    with persistence_lock():  # nobody re-encrypts the secrets meanwhile
        checked = _proven_secret_files(key)
        if target.is_file():
            try:
                same = read_key_file(target) == key
            except SecretStoreError:
                same = False
            if not same:
                raise MasterKeyError("keys/master.key holds a different key", kind="different")
            return {"ok": True, "key_file": str(target), "secrets_checked": checked, "already": True}
        try:
            write_new_key_file(target, key)
            written = read_key_file(target)
        except SecretStoreError as exc:
            raise MasterKeyError("the key could not be written", kind="write_failed") from exc
        if written != key:
            with suppress(OSError):
                target.unlink()
            raise MasterKeyError("the key read back differs", kind="write_failed")
    return {"ok": True, "key_file": str(target), "secrets_checked": checked, "already": False}


def save_secret_undo(data: dict[str, Any]) -> str:
    if not isinstance(data, dict):
        raise SecretStoreError("TOW secret undo payload must be an object")
    snapshot = {"secrets": data}
    path = secret_undo_path()
    _write_encrypted(path, snapshot, _UNDO_FORMAT)
    if _encrypted_payload(path, _UNDO_FORMAT) != snapshot:
        raise SecretStoreError("encrypted TOW undo read-back mismatch")
    return _SETTINGS_UNDO_REF


def load_secret_undo(reference: str) -> dict[str, Any]:
    if reference != _SETTINGS_UNDO_REF:
        raise SecretStoreError("unknown TOW secret undo reference")
    return _secret_undo_data(_encrypted_payload(secret_undo_path(), _UNDO_FORMAT))


def _secret_undo_data(payload: dict[str, Any]) -> dict[str, Any]:
    data = payload.get("secrets")
    if not isinstance(data, dict):
        raise SecretStoreError("encrypted TOW undo payload is malformed")
    return data


def delete_secret_undo(reference: str) -> None:
    if reference != _SETTINGS_UNDO_REF:
        raise SecretStoreError("unknown TOW secret undo reference")
    try:
        secret_undo_path().unlink()
    except FileNotFoundError:
        pass
    except OSError as exc:
        raise SecretStoreError("cannot remove TOW secret undo snapshot") from exc
