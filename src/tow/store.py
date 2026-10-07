from __future__ import annotations

import base64
import hashlib
import hmac
import importlib
import json
import math
import os
import pickle
import tempfile
import threading
import time
from collections.abc import Callable, Iterator, Mapping
from contextlib import contextmanager, suppress
from pathlib import Path
from typing import TYPE_CHECKING, Any, BinaryIO

from tow.errors import Msg, TowError

if TYPE_CHECKING:
    from cryptography.fernet import Fernet
from tow.paths import data_dir, download_history_path, explicit_key_file, key_file, legacy_key_file, secrets_path
from tow.paths import state_path as state_path  # noqa: PLC0414 - re-exported: the web package imports it from here
from tow.platform import current as current_platform
from tow.platform import locks


class SecretStoreError(RuntimeError):
    """Raised when TOW cannot safely read or write encrypted secrets."""


class MissingMasterKeyError(TowError, SecretStoreError):
    """No master key at all: said in the owner's words, with the command that fixes it."""


class InvalidMasterKeyPathError(TowError, SecretStoreError):
    """A relative master-key path must not leave the data folder."""


class StoreCorruptionError(RuntimeError):
    """Raised when a persisted JSON store is corrupt and was quarantined."""


_SECRETS_FORMAT = "tow-secrets/v1"
_UNDO_FORMAT = "tow-secrets-undo/v1"
_SETTINGS_UNDO_REF = "settings-v1"


_WRITE_LOCK = threading.RLock()
_PROCESS_LOCK_STATE = threading.local()
_LOCK_FILE_NAME = ".tow-persistence.lock"


class StoreReadError(StoreCorruptionError):
    """A store could not be read right now (I/O error); the file itself is left alone."""


class StateVersionError(TowError, StoreCorruptionError):
    """state.json has a data version this TOW does not know (``store.*``): it is neither read
    nor written, so a newer TOW's data is never corrupted by an older one."""


# The data version of state.json this TOW writes. A TOW that finds a newer one refuses to
# start; an older one (or none: TOW 1.18 and before) is brought up to date in _migrate_state.
STATE_SCHEMA_VERSION = 2


def _quarantine(path: Path) -> Path:
    stamp = time.strftime("%Y%m%d-%H%M%S")
    quarantine = path.with_name(f"{path.name}.corrupt-{stamp}-{os.getpid()}-{threading.get_ident()}")
    try:
        path.replace(quarantine)
    except OSError as exc:
        raise StoreCorruptionError(f"persisted JSON is unreadable: {path.name}") from exc
    return quarantine


def _has_quarantined_copy(path: Path) -> bool:
    prefix = f"{path.name}.corrupt-"
    try:
        return any(entry.name.startswith(prefix) for entry in path.parent.iterdir())
    except OSError:
        return False


def _finite_json_float(value: str) -> float:
    number = float(value)
    if not math.isfinite(number):
        raise ValueError("non-finite JSON number")
    return number


JSON_MAX_DEPTH = 128


def _check_json_depth(value: Any) -> None:
    # Native parser/encoder stack limits differ by OS. Keep the store contract
    # explicit, and walk level by level so this check has no recursion limit.
    # Only containers are visited: a value inside a non-empty container at the
    # limit is one level too deep, whatever it is.
    level = [value] if isinstance(value, (dict, list, tuple)) else []
    depth = 0
    while level:
        deeper: list[Any] = []
        for item in level:
            children = item.values() if isinstance(item, dict) else item
            if not children:
                continue
            if depth >= JSON_MAX_DEPTH:
                raise ValueError("JSON nesting exceeds the store limit")
            deeper.extend([child for child in children if isinstance(child, (dict, list, tuple))])
        level = deeper
        depth += 1


def decode_json_bytes(raw: bytes) -> Any:
    """Decode a store or recovery journal with finite numbers and bounded nesting."""
    value = json.loads(raw.decode("utf-8"), parse_float=_finite_json_float, parse_constant=_finite_json_float)
    _check_json_depth(value)
    return value


def _validated_json(raw: bytes, validate: Callable[[Any], None] | None) -> Any:
    value = decode_json_bytes(raw)
    if validate is not None:
        validate(value)
    return value


def load_json(
    path: Path, default: Any, *, quarantine: bool = True, validate: Callable[[Any], None] | None = None
) -> Any:
    if not path.is_file():
        if _has_quarantined_copy(path):
            # Never fall back to an empty default after quarantine: the next commit
            # would silently persist "no topics" over the user's data.
            raise StoreCorruptionError(
                f"{path.name} was quarantined as corrupt; restore it from a restore point or backup"
            )
        return default
    try:
        raw = path.read_bytes()
    except OSError as exc:
        # Transient (sharing violation, antivirus, permissions): not corruption.
        raise StoreReadError(f"persisted JSON cannot be read now: {path.name}") from exc
    try:
        return _validated_json(raw, validate)
    except (UnicodeError, ValueError, RecursionError) as exc:
        if quarantine:
            # A lock-free reader may have caught a moment a writer replaced the file: look
            # again under the lock and set aside only a file that is still unreadable.
            with persistence_lock():
                try:
                    reread = path.read_bytes()
                except OSError as read_exc:
                    # A transient lock is not evidence of corruption. In particular,
                    # never quarantine a good replacement we could not read yet.
                    raise StoreReadError(f"persisted JSON cannot be read now: {path.name}") from read_exc
                try:
                    return _validated_json(reread, validate)
                except UnicodeError, ValueError, RecursionError:
                    if path.is_file():
                        _quarantine(path)
        raise StoreCorruptionError(f"persisted JSON is unreadable: {path.name}") from exc


def init_lock_file(handle: BinaryIO) -> None:
    """Give a new lock file its single lock byte.

    Two processes can open a brand-new lock file at once; if the other one has
    already written and locked byte 0, our write hits the locked region
    (PermissionError on Windows). The byte exists either way, so ignore that.
    """
    handle.seek(0, os.SEEK_END)
    if handle.tell() == 0:
        try:
            handle.write(b"\0")
            handle.flush()
        except OSError:
            pass
    handle.seek(0)


# Every journal of an interrupted multi-file write, recovered by whichever process takes the
# data lock first (outermost level) - before ANY process writes again, and whatever that process
# imported: each step is imported here when it runs, never registered by an import elsewhere.
# (module, function, marker): with a marker the module is imported only when data/<marker> exists.
RECOVERY_STEPS: tuple[tuple[str, str, str | None], ...] = (
    # site settings: config + state + secrets of one web transaction
    ("tow.site_journal", "recover_site_journal", None),
    # a check's commit of state, history and secrets
    ("tow.check_transaction", "recover_locked", None),
    # an import of a portable bundle
    ("tow.bundle", "recover_interrupted_import", None),
    # a restore from a night copy (a check never needs the night-copy code otherwise)
    ("tow.snapshots", "recover_interrupted_restore", ".tow-night-restore.json"),
)


def _recovery_steps() -> list[Callable[[], None]]:
    steps: list[Callable[[], None]] = []
    for module, name, marker in RECOVERY_STEPS:
        if marker is not None:
            try:
                (data_dir() / marker).lstat()
            except FileNotFoundError:
                continue
            except OSError:
                pass  # an unreadable marker is not absent; let its recovery hook refuse safely
        steps.append(getattr(importlib.import_module(module), name))
    return steps


def _run_recovery_hooks() -> None:
    if getattr(_PROCESS_LOCK_STATE, "recovering", False):
        return
    _PROCESS_LOCK_STATE.recovering = True
    try:
        for step in _recovery_steps():
            step()
    finally:
        _PROCESS_LOCK_STATE.recovering = False


_CHECK_RUN_LOCK = threading.Lock()
_CHECK_RUN_LOCK_NAME = ".tow-check-run.lock"


class CheckBusyError(RuntimeError):
    """Another applying check is running and the caller asked not to wait for it."""


@contextmanager
def check_run_lock(*, wait: bool = True) -> Iterator[None]:
    """One applying check at a time (scheduled, progress, web) across TOW processes.

    Separate from ``persistence_lock``: a check holds this one for its whole run, network
    included, but takes the persistence lock only to read and to commit - the web UI, the
    watchdog and the night copy are never held up by tracker or client network time.
    ``wait=False`` (a web request) raises ``CheckBusyError`` at once instead of waiting
    minutes for a running check.
    """
    if not _CHECK_RUN_LOCK.acquire(blocking=wait):
        raise CheckBusyError("a check is already running")
    try:
        lock_path = data_dir() / _CHECK_RUN_LOCK_NAME
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        handle = lock_path.open("a+b")
        try:
            init_lock_file(handle)
            if not locks.lock(handle, wait=wait):
                raise CheckBusyError("a check is already running")
            try:
                yield
            finally:
                locks.unlock(handle)
        finally:
            handle.close()
    finally:
        _CHECK_RUN_LOCK.release()


@contextmanager
def persistence_lock() -> Iterator[None]:
    """Serialize persistence across threads and TOW processes, reentrantly."""
    with _WRITE_LOCK:
        depth = int(getattr(_PROCESS_LOCK_STATE, "depth", 0))
        owner_pid = getattr(_PROCESS_LOCK_STATE, "owner_pid", None)
        current_pid = os.getpid()
        if depth and owner_pid == current_pid:
            _PROCESS_LOCK_STATE.depth = depth + 1
            try:
                yield
            finally:
                _PROCESS_LOCK_STATE.depth -= 1
            return
        if depth:
            inherited_handle = getattr(_PROCESS_LOCK_STATE, "handle", None)
            _PROCESS_LOCK_STATE.depth = 0
            _PROCESS_LOCK_STATE.owner_pid = None
            _PROCESS_LOCK_STATE.handle = None
            if inherited_handle is not None:
                inherited_handle.close()

        lock_path = data_dir() / _LOCK_FILE_NAME
        lock_path.parent.mkdir(parents=True, exist_ok=True)
        handle = lock_path.open("a+b")
        try:
            init_lock_file(handle)
            locks.lock(handle)
            _PROCESS_LOCK_STATE.depth = 1
            _PROCESS_LOCK_STATE.owner_pid = current_pid
            _PROCESS_LOCK_STATE.handle = handle
            try:
                _run_recovery_hooks()
                yield
            finally:
                _PROCESS_LOCK_STATE.depth = 0
                _PROCESS_LOCK_STATE.owner_pid = None
                _PROCESS_LOCK_STATE.handle = None
                locks.unlock(handle)
        finally:
            handle.close()


@contextmanager
def _writer_lock(path: Path) -> Iterator[None]:
    del path
    with persistence_lock():
        yield


def _replace_with_retry(source: Path, target: Path, *, attempts: int = 8) -> None:
    """os.replace, retrying briefly while a reader has the target open (Windows sharing violation)."""
    for attempt in range(attempts):
        try:
            os.replace(source, target)
            return
        except PermissionError:
            if attempt == attempts - 1:
                raise
            time.sleep(0.02 * 2**attempt)  # 20 ms ... ~2.5 s in total


_write_generation = 0


def write_generation() -> int:
    """Counts the files this process has written: a per-request cache (tow.web) that saw another
    number reads again, so a page rendered after a save in the same request shows the save."""
    return _write_generation


def atomic_write_bytes(path: Path, content: bytes) -> None:
    _atomic_write_bytes(path, content, overwrite=True)


def atomic_create_bytes(path: Path, content: bytes) -> None:
    """Publish complete bytes only if the name is free, never clobber another writer."""
    _atomic_write_bytes(path, content, overwrite=False)


def _atomic_write_bytes(path: Path, content: bytes, *, overwrite: bool) -> None:
    global _write_generation
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary_name = None
    with _writer_lock(path):
        try:
            with tempfile.NamedTemporaryFile(
                mode="wb",
                prefix=f".{path.name}.",
                suffix=".tmp",
                dir=path.parent,
                delete=False,
            ) as handle:
                temporary_name = Path(handle.name)
                handle.write(content)
                handle.flush()
                os.fsync(handle.fileno())
            if overwrite:
                _replace_with_retry(temporary_name, path)
            else:
                current_platform().publish_exclusive(temporary_name, path)
            _write_generation += 1  # after the replace, under the writer lock (one writer at a time)
            try:
                directory_fd = os.open(path.parent, os.O_RDONLY)
            except OSError:
                directory_fd = None
            if directory_fd is not None:
                try:
                    os.fsync(directory_fd)
                finally:
                    os.close(directory_fd)
        finally:
            if temporary_name is not None:
                try:
                    temporary_name.unlink()
                except FileNotFoundError:
                    pass
                except OSError:
                    pass


def save_json(path: Path, data: Any, *, compact: bool = False) -> None:
    try:
        _check_json_depth(data)
        if compact:
            content = (json.dumps(data, ensure_ascii=False, separators=(",", ":"), allow_nan=False) + "\n").encode(
                "utf-8"
            )
        else:
            content = (json.dumps(data, ensure_ascii=False, indent=2, allow_nan=False) + "\n").encode("utf-8")
    except (TypeError, ValueError, UnicodeError, RecursionError) as exc:
        raise StoreCorruptionError(f"JSON payload is not serializable: {path.name}") from exc
    atomic_write_bytes(path, content)


def atomic_write_text(path: Path, text: str) -> None:
    try:
        atomic_write_bytes(path, text.encode("utf-8"))
    except UnicodeError as exc:
        raise StoreCorruptionError(f"text payload is not UTF-8: {path.name}") from exc


def encrypted_secrets_path() -> Path:
    return secrets_path().with_name("secrets.enc")


def secret_undo_path() -> Path:
    return secrets_path().with_name("secrets-undo.enc")


def _explicit_key_file() -> Path | None:
    """TOW_MASTER_KEY_FILE (a relative path is inside the data folder), or None."""
    try:
        return explicit_key_file()
    except ValueError as exc:
        raise InvalidMasterKeyPathError("store.key_path_outside_data") from exc


def _install_key_file() -> Path | None:
    """``<root>/keys/master.key`` when it exists."""
    try:
        path = key_file()
    except RuntimeError:  # no install root known (an installed package without TOW_ROOT)
        return None
    return path if path.is_file() else None


def _legacy_key_file() -> Path | None:
    """``<data>/master.key`` of an install from before 1.18, when it exists.

    It counts only after TOW_MASTER_KEY_FILE and keys/master.key - what the Windows launcher
    did alone before 1.21; now every start (scripts/tow, autostart, a scheduled task, a bare
    ``python -m tow``) finds the same key.
    """
    try:
        path = legacy_key_file()
    except RuntimeError:
        return None
    return path if path.is_file() else None


def _key_file_in_use() -> Path | None:
    return _explicit_key_file() or _install_key_file() or _legacy_key_file()


def master_key_file() -> Path | None:
    """The key file in use: TOW_MASTER_KEY_FILE, else the install's keys/master.key, else a
    legacy data/master.key; None when the key comes from TOW_MASTER_KEY (tests, CI) or there is
    none."""
    if os.environ.get("TOW_MASTER_KEY", "").strip():
        return None
    return _key_file_in_use()


def _key_source() -> str:
    if os.environ.get("TOW_MASTER_KEY", "").strip():
        return "env"
    if _explicit_key_file() is not None:
        return "file"
    if _install_key_file() is not None:
        return "install"
    if _legacy_key_file() is not None:
        return "legacy"
    return "missing"


def _read_key_file(path: Path) -> bytes:
    try:
        key = path.read_bytes().strip()
    except (OSError, UnicodeError) as exc:
        raise SecretStoreError(Msg("store.key_unreadable")) from exc
    if not key:
        raise SecretStoreError(Msg("store.key_empty"))
    return key


def _master_key() -> bytes:
    """TOW_MASTER_KEY > TOW_MASTER_KEY_FILE > <root>/keys/master.key > <data>/master.key (legacy)."""
    value = os.environ.get("TOW_MASTER_KEY", "").strip()
    if value:
        return value.encode("ascii", errors="strict")
    path = _key_file_in_use()
    if path is None:
        raise MissingMasterKeyError("store.no_master_key")
    return _read_key_file(path)


def derive_local_secret(purpose: str) -> str:
    """Derive a domain-separated server-local secret without exposing the master key."""
    if not isinstance(purpose, str) or not purpose or len(purpose) > 128 or not purpose.isascii():
        raise SecretStoreError("invalid local secret purpose")
    # Validate the configured value as a Fernet key before using it as key material.
    master_fernet()
    digest = hmac.new(
        _master_key(),
        f"tow/local-secret/v1/{purpose}".encode("ascii"),
        hashlib.sha256,
    ).digest()
    return base64.urlsafe_b64encode(digest).decode("ascii")


def master_fernet() -> Fernet:
    try:
        from cryptography.fernet import Fernet
    except ImportError as exc:
        raise SecretStoreError(Msg("store.no_cryptography")) from exc
    try:
        return Fernet(_master_key())
    except (TypeError, ValueError, UnicodeError) as exc:
        raise SecretStoreError(Msg("store.key_invalid")) from exc


def _json_bytes(data: Any) -> bytes:
    try:
        _check_json_depth(data)
        encoded = json.dumps(data, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")
        if decode_json_bytes(encoded) != data:
            raise ValueError("secret JSON would change the payload")
        return encoded
    except (TypeError, ValueError, UnicodeError, RecursionError) as exc:
        raise SecretStoreError("TOW secret payload is not serializable") from exc


def _write_bytes_atomic(path: Path, content: bytes) -> None:
    try:
        atomic_write_bytes(path, content)
        _restrict_file(path)
    except OSError as exc:
        raise SecretStoreError(Msg("store.secrets_write_failed")) from exc


def _restrict_file(path: Path) -> None:
    with suppress(OSError):
        path.chmod(0o600)


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
    return {"storage": storage, "key_source": _key_source()}


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


def install_folders() -> list[Path]:
    """keys/ and data/: folders only this account (and on Windows SYSTEM and Administrators)
    may open - the master key opens every saved password, data/ holds the encrypted ones."""
    return [key_file().parent, data_dir(create=False)]


def protect_install_folders(*, quiet: bool = False) -> list[Path]:
    """At start: the install root, keys/ and data/ of this account become private when other
    accounts can get in (an older install, an install in a drive root on Windows).

    The root first (``tow.platform.private_root``): through it other accounts could change the
    code TOW runs and so read the master key. keys/ and data/ are closed on their own too, so
    they stay private when the root cannot be changed (another account owns it) or they live
    elsewhere. A folder that stays open (another account's, or the change failed) is named on
    stderr (not when ``quiet``: `tow permissions` says it itself) and returned; the start goes
    on. `tow doctor` reports it too, and `tow permissions fix` repairs another account's."""
    import sys

    from tow.i18n import t
    from tow.paths import root
    from tow.platform import private_folders, private_root

    try:
        install, folders = root(), install_folders()
    except OSError, RuntimeError:  # no install located: the command reports that itself
        return []
    root_open = private_root(install)
    if root_open and not quiet:
        print(t("cli.root_shared", path=str(install)), file=sys.stderr)
    still_open = private_folders(folders)
    for folder in still_open if not quiet else ():
        print(t("cli.folder_shared", path=str(folder)), file=sys.stderr)
    return [install, *still_open] if root_open else still_open


def _key_folder(path: Path) -> None:
    """Create the folder of a key file; a folder TOW creates is readable by this user only
    (POSIX 0700; Windows: this account, SYSTEM and Administrators, nothing inherited). An
    existing folder - $HOME for `--key-file ~/tow.key` - keeps its permissions."""
    if path.parent.is_dir():
        return
    from tow.platform import private_folders

    path.parent.mkdir(parents=True, exist_ok=True)
    private_folders([path.parent], created=True)


_NEW_KEY_FLAGS = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)


def _write_new_key_file(path: Path, key: bytes) -> None:
    """Create ``path`` holding ``key``; never replaces an existing file.

    The file is created readable by this user only (0600 on POSIX) in the same call that
    creates it: never a moment with the umask's permissions before a chmod.
    """
    _key_folder(path)
    try:
        handle = os.fdopen(os.open(path, _NEW_KEY_FLAGS, 0o600), "wb")
    except FileExistsError as exc:
        raise SecretStoreError(Msg("cli.keys.error.exists")) from exc
    except OSError as exc:
        raise SecretStoreError(Msg("cli.keys.error.write_failed")) from exc
    try:
        with handle:
            handle.write(key + b"\n")
            handle.flush()
            os.fsync(handle.fileno())
    except OSError as exc:
        with suppress(OSError):
            path.unlink()  # a half-written key must not look like a different one next time
        raise SecretStoreError(Msg("cli.keys.error.write_failed")) from exc
    _restrict_file(path)


# Why a master key could not be created or adopted (catalog keys cli.keys.error.<kind>).
MASTER_KEY_KINDS: tuple[str, ...] = ("secrets_exist", "exists", "invalid", "wrong_key", "no_source", "unreadable")
MASTER_KEY_KINDS += ("different", "write_failed")


class MasterKeyError(SecretStoreError):
    """A master key could not be created or adopted; ``kind`` (one of MASTER_KEY_KINDS) says why."""

    def __init__(self, message: str, *, kind: str) -> None:
        super().__init__(message)
        self.kind = kind


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
    _write_new_key_file(path, Fernet.generate_key())
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
    source = Path(source) if source is not None else (_explicit_key_file() or _legacy_key_file())
    if source is None:
        raise MasterKeyError("no key file to adopt", kind="no_source")
    target = key_file()
    try:
        key = _read_key_file(source)
    except SecretStoreError as exc:
        raise MasterKeyError("the key file cannot be read", kind="unreadable") from exc
    with persistence_lock():  # nobody re-encrypts the secrets meanwhile
        checked = _proven_secret_files(key)
        if target.is_file():
            try:
                same = _read_key_file(target) == key
            except SecretStoreError:
                same = False
            if not same:
                raise MasterKeyError("keys/master.key holds a different key", kind="different")
            return {"ok": True, "key_file": str(target), "secrets_checked": checked, "already": True}
        try:
            _write_new_key_file(target, key)
            written = _read_key_file(target)
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


def _file_stamp(path: Path) -> tuple[str, int, int, int] | None:
    try:
        stat = path.stat()
    except OSError:
        return None
    return (str(path), stat.st_mtime_ns, stat.st_size, stat.st_ino)


# The state is read by every page and every check: parse it again only when the file changed.
# Writers replace it atomically, so (mtime, size, file id) changes with every write (as for the
# config). Each caller gets its own copy, made from a pickle of the parsed data: about fifteen
# times faster than parsing the JSON again. The pickle never leaves this process.
_STATE_CACHE_LOCK = threading.Lock()
_state_cache: tuple[tuple[str, int, int, int], bytes] | None = None


def state_schema_version(data: Mapping[str, Any]) -> int:
    """The data version of a loaded state.json (0: written before versions existed); a version
    this TOW cannot use raises ``StateVersionError``."""
    version = data.get("schema_version", 0)
    if isinstance(version, bool) or not isinstance(version, int) or version < 0:
        raise StateVersionError("store.state_bad_version", found=str(version)[:20])
    if version > STATE_SCHEMA_VERSION:
        raise StateVersionError("store.state_newer", found=version, known=STATE_SCHEMA_VERSION)
    return version


def _migrate_state(data: dict[str, Any], version: int) -> dict[str, Any]:
    """Bring a state written with data ``version`` up to the current one: the only place that
    knows older layouts (each step from N to N + 1, in order, when the layout changes)."""
    if version < 1:
        pass  # 0 -> 1 (TOW 1.19): only the version field is new; the layout is the same.
    return data


def _validate_state_container(value: Any) -> None:
    mapping = isinstance(value, dict)
    if not mapping:
        raise ValueError("state JSON must be an object")
    # A future data version is not corruption: preserve it untouched and report
    # the version error before applying this version's container requirements.
    state_schema_version(value)
    topics = value.get("topics", [])
    valid = isinstance(topics, list) and all(isinstance(topic, dict) for topic in topics)
    if not valid or not isinstance(value.get("mirrors", {}), dict):
        raise ValueError("state JSON has malformed containers")
    from tow.topic_timers import interval_of

    for topic in topics:
        try:
            interval_of(topic)
        except TowError as exc:
            raise ValueError("invalid topic check interval") from exc
        selection = topic.get("selection")
        if isinstance(selection, dict) and selection.get("mode") == "exact":
            from tow.selection import policy_from_topic

            try:
                policy_from_topic(topic)
            except TowError as exc:
                raise ValueError("invalid exact file selection") from exc


def _validate_history_container(value: Any) -> None:
    mapping = isinstance(value, dict)
    if not mapping:
        raise ValueError("download history JSON must be an object")
    topics = value.get("topics", {})
    valid = isinstance(topics, dict) and all(isinstance(record, dict) for record in topics.values())
    if not valid:
        raise ValueError("download history JSON has malformed topics")
    for record in topics.values():
        items = record.get("items", {})
        valid = isinstance(items, dict) and all(isinstance(item, dict) for item in items.values())
        if not valid:
            raise ValueError("download history JSON has malformed items")


def validate_state_bytes(content: bytes) -> None:
    """Check the live-store read contract without migration, quarantine or other writes."""
    _validated_json(content, _validate_state_container)


def validate_download_history_bytes(content: bytes) -> None:
    """Check history by the same parser and containers as its live reader."""
    _validated_json(content, _validate_history_container)


def load_state(*, quarantine: bool = True) -> dict[str, Any]:
    global _state_cache
    path = state_path()
    stamp = _file_stamp(path)
    with _STATE_CACHE_LOCK:
        cached = _state_cache
    if stamp is not None and cached is not None and cached[0] == stamp:
        copy_: dict[str, Any] = pickle.loads(cached[1])
        return copy_
    data = load_json(path, {"topics": [], "mirrors": {}}, quarantine=quarantine, validate=_validate_state_container)
    version = state_schema_version(data)
    data.pop("schema_version", None)  # a file-format detail: callers see the topics, not it
    data = _migrate_state(data, version)
    data.setdefault("topics", [])
    data.setdefault("mirrors", {})
    undo = data.get("undo")
    if isinstance(undo, dict) and "secrets" in undo:
        data.pop("undo", None)
    if stamp is not None and _file_stamp(path) == stamp:  # not replaced while it was read
        with _STATE_CACHE_LOCK:
            _state_cache = (stamp, pickle.dumps(data, protocol=pickle.HIGHEST_PROTOCOL))
    return data


def check_state_version() -> None:
    """Refuse to start on a state.json written by a newer TOW (``StateVersionError`` with the
    reason in words); nothing is read further or written."""
    path = state_path()
    try:
        data = load_json(path, {}, quarantine=False) if path.is_file() else {}
    except StoreCorruptionError:
        return  # an unreadable file is handled where it is read (quarantine, restore point)
    if isinstance(data, dict):
        state_schema_version(data)


def save_state(data: dict[str, Any]) -> None:
    undo = data.get("undo")
    if isinstance(undo, dict) and "secrets" in undo:
        raise SecretStoreError("plaintext secret undo is not permitted")
    payload = {"schema_version": STATE_SCHEMA_VERSION, **{k: v for k, v in data.items() if k != "schema_version"}}
    try:
        _validate_state_container(payload)
    except ValueError as exc:
        raise StoreCorruptionError("state JSON payload has malformed containers") from exc
    save_json(state_path(), payload)


def load_download_history(*, quarantine: bool = True) -> dict[str, Any]:
    data: dict[str, Any] = load_json(
        download_history_path(),
        {"schema_version": 1, "topics": {}},
        quarantine=quarantine,
        validate=_validate_history_container,
    )
    data.setdefault("schema_version", 1)
    data.setdefault("topics", {})
    return data


def save_download_history(data: Mapping[str, Any]) -> None:
    try:
        _validate_history_container(data)
    except ValueError as exc:
        raise StoreCorruptionError("download history JSON payload has malformed containers") from exc
    # Machine data rewritten twice per apply-check: compact (~22% smaller than indent=2).
    save_json(download_history_path(), data, compact=True)
