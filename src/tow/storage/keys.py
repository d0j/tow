"""The master key: where it comes from, its Fernet, the secrets derived from it, a new key file.

``tow.storage.secrets`` encrypts with it; creating and adopting a key live there too, because
both must look at the encrypted secrets first.
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import os
from contextlib import suppress
from pathlib import Path
from typing import TYPE_CHECKING

from tow.errors import Msg, TowError
from tow.paths import explicit_key_file, key_file, legacy_key_file
from tow.storage.atomic import restrict_file
from tow.storage.errors import SecretStoreError

if TYPE_CHECKING:
    from cryptography.fernet import Fernet


class MissingMasterKeyError(TowError, SecretStoreError):
    """No master key at all: said in the owner's words, with the command that fixes it."""


class InvalidMasterKeyPathError(TowError, SecretStoreError):
    """A relative master-key path must not leave the data folder."""


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


def key_file_to_adopt() -> Path | None:
    """The key file ``adopt_master_key`` takes when it is given none: TOW_MASTER_KEY_FILE, else a
    legacy data/master.key."""
    return _explicit_key_file() or _legacy_key_file()


def master_key_file() -> Path | None:
    """The key file in use: TOW_MASTER_KEY_FILE, else the install's keys/master.key, else a
    legacy data/master.key; None when the key comes from TOW_MASTER_KEY (tests, CI) or there is
    none."""
    if os.environ.get("TOW_MASTER_KEY", "").strip():
        return None
    return _key_file_in_use()


# Every word key_source() says: where the master key in use comes from.
KEY_SOURCES: tuple[str, ...] = ("env", "file", "install", "legacy", "missing")


def key_source() -> str:
    if os.environ.get("TOW_MASTER_KEY", "").strip():
        return "env"
    if _explicit_key_file() is not None:
        return "file"
    if _install_key_file() is not None:
        return "install"
    if _legacy_key_file() is not None:
        return "legacy"
    return "missing"


def read_key_file(path: Path) -> bytes:
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
    return read_key_file(path)


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


def _key_folder(path: Path) -> None:
    """Create the folder of a key file; a folder TOW creates is readable by this user only
    (POSIX 0700; Windows: this account, SYSTEM and Administrators, nothing inherited). An
    existing folder - $HOME for `--key-file ~/tow.key` - keeps its permissions. A new folder
    that other accounts can still open (the change failed) is named on stderr, as a start
    names keys/ (``protect_install_folders``); the key is still made."""
    if path.parent.is_dir():
        return
    import sys

    from tow.i18n import t
    from tow.platform import private_folders

    path.parent.mkdir(parents=True, exist_ok=True)
    for folder in private_folders([path.parent], created=True):
        print(t("cli.folder_shared", path=str(folder)), file=sys.stderr)


_NEW_KEY_FLAGS = os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0) | getattr(os, "O_NOFOLLOW", 0)


def write_new_key_file(path: Path, key: bytes) -> None:
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
    restrict_file(path)
    _sync_folder(path.parent)


def _sync_folder(folder: Path) -> None:
    """The new key's folder entry is on the disk too: a power cut soon after the first setup could
    lose keys/master.key while secrets.enc (whose folder is flushed) survived, and the secrets
    could not be opened any more. Where the system can (POSIX; on Windows a folder cannot be
    opened so, and NTFS journals its entries)."""
    try:
        handle = os.open(folder, os.O_RDONLY)
    except OSError:
        return
    try:
        with suppress(OSError):
            os.fsync(handle)
    finally:
        os.close(handle)


# Why a master key could not be created or adopted (catalog keys cli.keys.error.<kind>).
MASTER_KEY_KINDS: tuple[str, ...] = ("secrets_exist", "exists", "invalid", "wrong_key", "no_source", "unreadable")
MASTER_KEY_KINDS += ("different", "write_failed")


class MasterKeyError(SecretStoreError):
    """A master key could not be created or adopted; ``kind`` (one of MASTER_KEY_KINDS) says why."""

    def __init__(self, message: str, *, kind: str) -> None:
        super().__init__(message)
        self.kind = kind
