"""Bounded, encrypted metadata for live topics, never proof of a fresh revision."""

from __future__ import annotations

import base64
import hashlib
import json
import os
import re
import stat
from pathlib import Path
from typing import Any

from cryptography.fernet import InvalidToken

from tow.content import MAX_RECORD_BYTES
from tow.errors import TowError
from tow.guess import canon_watch_url
from tow.paths import data_dir
from tow.store import atomic_write_bytes, load_state, master_fernet, persistence_lock, save_state
from tow.torrent import MAX_TORRENT_BYTES, parse_magnet_hashes, parse_torrent_metadata

MAX_BYTES = 256 * 1024 * 1024
MAX_RECORDS = 2048
_NAME = re.compile(r"[0-9a-f]{64}\.bin")


def _folder(*, create: bool = False) -> Path:
    folder = data_dir(create=create) / "torrent-metadata"
    if folder.is_symlink() or folder.is_junction():
        raise TowError("content.unavailable")
    if create:
        folder.mkdir(mode=0o700, exist_ok=True)
    return folder


def _path(url: str, *, create: bool = False) -> Path:
    return _folder(create=create) / (hashlib.sha256(url.encode()).hexdigest() + ".bin")


def _used(url: str) -> bool:
    return bool(_matching(load_state(quarantine=False), url))


def _matching(state: dict[str, Any], url: str) -> list[dict[str, Any]]:
    return [topic for topic in state.get("topics", []) if canon_watch_url(str(topic.get("url") or "")) == url]


def read(url: str) -> bytes | None:
    """Read a live topic's saved snapshot without network access."""
    url = canon_watch_url(url)
    with persistence_lock():
        _folder()  # refuse redirected storage even for a cold lookup
        topics = _matching(load_state(quarantine=False), url)
        if not topics:
            return None
        if any(topic.get("metadata_cache_unavailable") is True for topic in topics):
            raise TowError("content.cache_invalid")
        return _read_record(url)


def _read_record(url: str) -> bytes | None:
    with persistence_lock():
        path = _path(url)
        try:
            info = path.lstat()
        except FileNotFoundError:
            return None
        if not stat.S_ISREG(info.st_mode) or path.is_junction() or info.st_size > MAX_RECORD_BYTES:
            raise TowError("content.unavailable")
        with path.open("rb") as stream:
            opened = os.fstat(stream.fileno())
            if not stat.S_ISREG(opened.st_mode) or (opened.st_dev, opened.st_ino) != (info.st_dev, info.st_ino):
                raise TowError("content.unavailable")
            encrypted = stream.read(MAX_RECORD_BYTES + 1)
        if len(encrypted) != info.st_size:
            raise TowError("content.unavailable")
        try:
            record = json.loads(master_fernet().decrypt(encrypted))
            if not isinstance(record, dict) or record.get("url") != url:
                raise ValueError("cache binding")
            blob = base64.b64decode(record["blob"], validate=True)
            if len(blob) > MAX_TORRENT_BYTES:
                raise ValueError("cache size")
            parse_torrent_metadata(blob)
            return blob
        except InvalidToken, TowError, ValueError, KeyError, TypeError:
            raise TowError("content.cache_invalid") from None


def remember(blob: bytes, url: str) -> None:
    """Do not let a failed replacement disguise a proven changed revision as current."""
    metadata = parse_torrent_metadata(blob)
    url = canon_watch_url(url)
    with persistence_lock():
        state = load_state(quarantine=False)
        topics = _matching(state, url)
        if not topics:
            return
        try:
            old = _read_record(url)
        except TowError as exc:
            if exc.code != "content.cache_invalid":
                raise
            old = None
        if old == blob:
            _mark_unavailable(state, topics, False)
            return
        try:
            _remember(blob, url)
        except TowError, OSError, RuntimeError, ValueError:
            if old is not None and parse_torrent_metadata(old).infohash != metadata.infohash:
                _mark_unavailable(state, topics, True)
            raise
        _mark_unavailable(state, topics, False)


def _mark_unavailable(state: dict[str, Any], topics: list[dict[str, Any]], unavailable: bool) -> None:
    changed = False
    for topic in topics:
        if unavailable and topic.get("metadata_cache_unavailable") is not True:
            topic["metadata_cache_unavailable"] = True
            changed = True
        elif not unavailable and "metadata_cache_unavailable" in topic:
            topic.pop("metadata_cache_unavailable")
            changed = True
    if changed:
        save_state(state)


def _remember(blob: bytes, url: str) -> None:
    """Atomic storage for an already validated live URL."""
    with persistence_lock():
        record = json.dumps({"url": url, "blob": base64.b64encode(blob).decode("ascii")}).encode()
        encrypted = master_fernet().encrypt(record)
        if len(encrypted) > MAX_RECORD_BYTES:
            raise TowError("content.too_large")
        path = _path(url, create=True)
        total = count = 0
        for item in path.parent.iterdir():
            info = item.lstat()
            if re.fullmatch(r"\.[0-9a-f]{64}\.bin\.[a-z0-9_]{8}\.tmp", item.name) and stat.S_ISREG(info.st_mode):
                item.unlink()  # orphaned atomic writes; every writer holds this lock
                continue
            if not _NAME.fullmatch(item.name) or not stat.S_ISREG(info.st_mode) or item.is_junction():
                raise TowError("content.unavailable")
            if item != path:
                total += info.st_size
                count += 1
        if total + len(encrypted) > MAX_BYTES or count + 1 > MAX_RECORDS:
            raise TowError("content.full")
        atomic_write_bytes(path, encrypted)


def forget_if_unused(url: str) -> None:
    """Delete only our metadata after the last observation of this URL is removed."""
    url = canon_watch_url(url)
    with persistence_lock():
        if _used(url):
            return
        path = _path(url)
        try:
            info = path.lstat()
        except FileNotFoundError:
            return
        if not stat.S_ISREG(info.st_mode) or path.is_junction():
            raise TowError("content.unavailable")
        path.unlink()


def observe_magnet(url: str, magnet: str) -> None:
    """A proven different tracker identity makes the old contents ineligible for reuse."""
    identities = parse_magnet_hashes(magnet)
    if identities is None:
        return
    with persistence_lock():
        url = canon_watch_url(url)
        state = load_state(quarantine=False)
        topics = _matching(state, url)
        if not topics:
            return
        blob = _read_record(url)
        if blob is None:
            return
        metadata = parse_torrent_metadata(blob)
        v1, v2 = identities
        if (v1 and metadata.hash_v1 not in v1) or (v2 and metadata.hash_v2 not in v2):
            try:
                _path(url).unlink()
            except OSError:
                _mark_unavailable(state, topics, True)
                raise
        _mark_unavailable(state, topics, False)
