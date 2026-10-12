"""The outer envelope of a bundle: PBKDF2-HMAC-SHA256 from the passphrase, then Fernet.

``KDF_ITERATIONS`` is read here when a bundle is sealed (tests lower it in this module)."""

from __future__ import annotations

import base64
import binascii
import os
from typing import Any

from tow.bundle.errors import ExportImportError
from tow.bundle.io import FORMAT

KDF_NAME = "pbkdf2-hmac-sha256"
KDF_ITERATIONS = 600_000
KDF_SALT_BYTES = 16


def _derive_key(passphrase: str, salt: bytes, iterations: int) -> bytes:
    if not isinstance(passphrase, str) or not passphrase:
        raise ExportImportError("export passphrase must not be empty")
    try:
        from cryptography.hazmat.primitives import hashes
        from cryptography.hazmat.primitives.kdf.pbkdf2 import PBKDF2HMAC

        return PBKDF2HMAC(
            algorithm=hashes.SHA256(),
            length=32,
            salt=salt,
            iterations=iterations,
        ).derive(passphrase.encode("utf-8"))
    except (ImportError, TypeError, ValueError, UnicodeError) as exc:
        raise ExportImportError("cannot derive export bundle key") from exc


def _encrypt_outer(payload: bytes, passphrase: str, salt: bytes | None = None) -> str:
    if salt is None:
        salt = os.urandom(KDF_SALT_BYTES)
    try:
        from cryptography.fernet import Fernet

        key = base64.urlsafe_b64encode(_derive_key(passphrase, salt, KDF_ITERATIONS))
        return Fernet(key).encrypt(payload).decode("ascii")
    except ExportImportError:
        raise
    except (ImportError, TypeError, ValueError, UnicodeError) as exc:
        raise ExportImportError("cannot encrypt export bundle") from exc


def seal_outer(payload: bytes, passphrase: str) -> dict[str, Any]:
    """The envelope of an export: a fresh salt, this KDF and its cost, the encrypted payload."""
    salt = os.urandom(KDF_SALT_BYTES)
    return {
        "format": FORMAT,
        "cipher": "fernet",
        "kdf": {
            "name": KDF_NAME,
            "iterations": KDF_ITERATIONS,
            "salt": base64.urlsafe_b64encode(salt).decode("ascii"),
        },
        "payload": _encrypt_outer(payload, passphrase, salt),
    }


def decrypt_outer(outer: dict[str, Any], passphrase: str) -> bytes:
    if not isinstance(outer, dict) or outer.get("format") != FORMAT:
        raise ExportImportError("unsupported export bundle format", reason="not_tow")
    if outer.get("cipher") != "fernet":
        raise ExportImportError("unsupported export bundle cipher", reason="not_tow")
    kdf = outer.get("kdf")
    if not isinstance(kdf, dict) or kdf.get("name") != KDF_NAME:
        raise ExportImportError("unsupported export bundle KDF", reason="not_tow")
    try:
        iterations = int(kdf.get("iterations") or 0)  # 0 is rejected by the range check below
        salt = base64.urlsafe_b64decode(str(kdf.get("salt") or "").encode("ascii"))
    except (TypeError, ValueError, UnicodeError, binascii.Error) as exc:
        raise ExportImportError("malformed export bundle KDF") from exc
    if not 100_000 <= iterations <= 5_000_000 or not 8 <= len(salt) <= 64:
        raise ExportImportError("invalid export bundle KDF parameters")
    token = outer.get("payload")
    if not isinstance(token, str) or not token:
        raise ExportImportError("malformed export bundle payload")
    try:
        from cryptography.fernet import Fernet, InvalidToken

        key = base64.urlsafe_b64encode(_derive_key(passphrase, salt, iterations))
        return Fernet(key).decrypt(token.encode("ascii"))
    except ExportImportError:
        raise
    except (InvalidToken, TypeError, ValueError, UnicodeError, binascii.Error) as exc:
        raise ExportImportError(
            "cannot decrypt export bundle; wrong passphrase or corrupted bundle", reason="other_key"
        ) from exc
