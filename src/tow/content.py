"""Bounded, encrypted preparation snapshots. No media or client mutations."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import stat
import threading
import time
import uuid
from collections import OrderedDict
from pathlib import Path
from typing import Any

from cryptography.fernet import InvalidToken

from tow.errors import TowError
from tow.paths import tmp_dir
from tow.platform import child_path
from tow.selection import normalize_policy
from tow.store import atomic_create_bytes, master_fernet, persistence_lock
from tow.torrent import MAX_TORRENT_BYTES, TorrentMetadata, parse_torrent_metadata

TTL = 15 * 60
MAX_CACHE_BYTES = 64 * 1024 * 1024
MAX_CACHE_RECORDS = 128
MAX_RECORD_BYTES = 60 * 1024 * 1024


def _folder(*, create: bool = True) -> Path:
    folder = tmp_dir(create=create) / "content"
    if folder.is_symlink() or folder.is_junction():
        raise TowError("content.unavailable")
    if create:
        folder.mkdir(mode=0o700, exist_ok=True)
    return folder


def prepare(blob: bytes, url: str, client_id: str, *, from_site: bool = False) -> dict[str, Any]:
    """``from_site``: the site provided these bytes (its download, its saved copy, or a magnet
    preview verified against its magnet). A local file only previews the contents."""
    parse_torrent_metadata(blob)
    record = json.dumps(
        {
            "url": url,
            "client_id": client_id,
            "source": "site" if from_site else "local",
            "blob": base64.b64encode(blob).decode("ascii"),
        }
    ).encode()
    encrypted = master_fernet().encrypt(record)
    if len(encrypted) > MAX_RECORD_BYTES:
        raise TowError("content.too_large")
    token = uuid.uuid4().hex
    with persistence_lock():
        folder = _folder()
        size = 0
        count = 0
        for path in folder.iterdir():
            info = path.lstat()
            if re.fullmatch(r"\.[0-9a-f]{32}\.bin\.[a-z0-9_]{8}\.tmp", path.name) and stat.S_ISREG(info.st_mode):
                # atomic_create_bytes may have been interrupted. The same process lock
                # protects every preparation write, so this cannot be an active writer.
                path.unlink()
                continue
            if not re.fullmatch(r"[0-9a-f]{32}\.bin", path.name) or not stat.S_ISREG(info.st_mode):
                raise TowError("content.unavailable")
            # Pending adds can refetch only the same proven revision after cache expiry.
            if time.time() - info.st_mtime > TTL:
                path.unlink()
            else:
                size += info.st_size
                count += 1
        if size + len(encrypted) > MAX_CACHE_BYTES or count >= MAX_CACHE_RECORDS:
            raise TowError("content.full")
        atomic_create_bytes(folder / f"{token}.bin", encrypted)
    return describe(blob, token)


def describe(blob: bytes, token: str) -> dict[str, Any]:
    """Public file identities, with byte counts encoded losslessly for JavaScript."""
    metadata = parse_torrent_metadata(blob)
    return {
        "token": token,
        "hash": metadata.infohash,
        "name": metadata.name,
        "digest": hashlib.sha256(blob).hexdigest(),
        "expires_in": TTL,
        "files": [
            {"id": row.index, "path": row.path, "size": str(row.size)} for row in metadata.files if not row.is_pad
        ],
    }


def read(token: str, url: str, client_id: str) -> bytes:
    return _read(token, url, client_id)[0]


# The parsed torrent of the last few records: every rule change of the content picker resolves
# against the same prepared torrent, and decrypting and parsing it (up to 60 MB) each time cost
# a second or more. An entry ends a little before its record does (TTL from its creation).
_PARSED: OrderedDict[str, tuple[str, str, float, TorrentMetadata]] = OrderedDict()
_PARSED_MAX = 8
_PARSED_LOCK = threading.Lock()


def metadata(token: str, url: str, client_id: str) -> TorrentMetadata:
    """The prepared torrent of ``token``, parsed (``read`` and ``parse_torrent_metadata``)."""
    now = time.time()
    with _PARSED_LOCK:
        for key in [key for key, entry in _PARSED.items() if entry[2] <= now]:
            del _PARSED[key]
        hit = _PARSED.get(token)
        if hit is not None:
            if (hit[0], hit[1]) != (url, client_id):
                raise TowError("content.changed")
            _PARSED.move_to_end(token)
            return hit[3]
    _blob, _from_site, created, parsed = _read_record(token, url, client_id)
    with _PARSED_LOCK:
        _PARSED[token] = (url, client_id, created + TTL - 5, parsed)
        while len(_PARSED) > _PARSED_MAX:
            _PARSED.popitem(last=False)
    return parsed


def site_revision(token: str, url: str, client_id: str) -> bytes | None:
    """A new topic's first revision, when the site itself provided the prepared bytes.

    None for a local file: it was never compared with the topic, so the check obtains the
    revision from the site and refuses it unless it is the prepared one (``content_hash``).
    """
    blob, from_site = _read(token, url, client_id)
    return blob if from_site else None


def _read(token: str, url: str, client_id: str) -> tuple[bytes, bool]:
    blob, from_site, _created, _parsed = _read_record(token, url, client_id)
    return blob, from_site


def _read_record(token: str, url: str, client_id: str) -> tuple[bytes, bool, float, TorrentMetadata]:
    """(the torrent, whether the site provided it, when the record was written, the torrent
    parsed - which also proves it is one)."""
    if not re.fullmatch(r"[0-9a-f]{32}", token):
        raise TowError("content.expired")
    try:
        path = child_path(_folder(create=False), f"{token}.bin")
        info = path.lstat()
        if not stat.S_ISREG(info.st_mode) or info.st_size > MAX_RECORD_BYTES:
            raise TowError("content.unavailable")
        with path.open("rb") as stream:
            opened = os.fstat(stream.fileno())
            if not stat.S_ISREG(opened.st_mode) or (opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino):
                raise TowError("content.unavailable")
            encrypted = stream.read(MAX_RECORD_BYTES + 1)
        if len(encrypted) != info.st_size:
            raise TowError("content.unavailable")
        record = json.loads(master_fernet().decrypt(encrypted, ttl=TTL))
        if not isinstance(record, dict):
            raise TowError("content.unavailable")
        if record.get("url") != url or record.get("client_id") != client_id:
            raise TowError("content.changed")
        blob = base64.b64decode(record["blob"], validate=True)
        if len(blob) > MAX_TORRENT_BYTES:
            raise TowError("content.too_large")
        return blob, record.get("source") == "site", info.st_mtime, parse_torrent_metadata(blob)
    except (OSError, InvalidToken, ValueError, KeyError, TypeError) as exc:
        raise TowError("content.expired") from exc


def selection(metadata: TorrentMetadata, indices: object, tracking_mode: str) -> dict[str, Any]:
    """The browser's chosen ids of a prepared torrent as literal paths and sizes."""
    if not isinstance(indices, list) or not indices or any(type(index) is not int for index in indices):
        raise TowError("selection.exact_invalid")
    wanted = set(indices)
    if len(wanted) != len(indices):
        raise TowError("selection.exact_invalid")
    rows = [row for row in metadata.files if not row.is_pad and row.index in wanted]
    if len(rows) != len(wanted):
        raise TowError("selection.exact_invalid")
    return normalize_policy(
        "exact",
        tracking_mode=tracking_mode,
        source_hash=metadata.infohash,
        files=[{"path": row.path, "size": row.size} for row in rows],
    )
