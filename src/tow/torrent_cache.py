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
from tow.paths import data_dir, state_path
from tow.store import (
    FileStamp,
    atomic_write_bytes,
    file_stamp,
    load_state,
    master_fernet,
    persistence_lock,
    save_state,
)
from tow.torrent import MAX_TORRENT_BYTES, parse_magnet_hashes, parse_torrent_metadata

MAX_BYTES = 256 * 1024 * 1024
MAX_RECORDS = 2048
_NAME = re.compile(r"[0-9a-f]{64}\.bin")
_ORPHAN = re.compile(r"\.[0-9a-f]{64}\.bin\.[a-z0-9_]{8}\.tmp")


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
    return url in _watched()


# Every watched URL, and whether a topic of it carries the "metadata unavailable" mark, by the
# state file it was read from: a check asks once per topic, and reading the whole state for each
# (and normalising every URL in it) cost 2000 x 2000 URLs at 2000 topics. Every user of it holds
# the data lock, as every writer of the state does.
_watched_index: tuple[FileStamp, dict[str, bool]] | None = None


def _watched() -> dict[str, bool]:
    global _watched_index
    stamp = file_stamp(state_path())
    if stamp is not None and _watched_index is not None and _watched_index[0] == stamp:
        return _watched_index[1]
    urls: dict[str, bool] = {}
    for topic in load_state(quarantine=False).get("topics", []):
        url = canon_watch_url(str(topic.get("url") or ""))
        urls[url] = urls.get(url, False) or topic.get("metadata_cache_unavailable") is True
    if stamp is not None and file_stamp(state_path()) == stamp:
        _watched_index = (stamp, urls)
    return urls


def _matching(state: dict[str, Any], url: str) -> list[dict[str, Any]]:
    return [topic for topic in state.get("topics", []) if canon_watch_url(str(topic.get("url") or "")) == url]


def read(url: str) -> bytes | None:
    """Read a live topic's saved snapshot without network access."""
    url = canon_watch_url(url)
    with persistence_lock():
        _folder()  # refuse redirected storage even for a cold lookup
        watched = _watched()
        if url not in watched:
            return None
        if watched[url]:
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
            encrypted = stream.read(info.st_size + 1)  # one byte more shows a file that grew
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
    parse_torrent_metadata(blob)
    url = canon_watch_url(url)
    with persistence_lock():
        watched = _watched()
        if url not in watched:
            return
        try:
            try:
                old = _read_record(url)
            except TowError as exc:
                if exc.code != "content.cache_invalid":
                    raise
                old = None
            if old != blob:
                _remember(blob, url)
        except TowError, OSError, RuntimeError, ValueError:
            # Unreadable storage cannot prove that the retained copy matches this
            # evidence. Persist the refusal before later access can recover.
            _mark_unavailable(url, True)
            raise
        if watched[url]:
            _mark_unavailable(url, False)


def _mark_unavailable(url: str, unavailable: bool) -> None:
    state = load_state(quarantine=False)
    changed = False
    for topic in _matching(state, url):
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
        # One directory listing with the entries' own types and sizes (on Windows no call per
        # file; a lstat and a junction test per file took ten times as long). The folder's
        # modification time cannot stand in for a listing: on NTFS it often stays the same
        # across files created in the same clock tick.
        with os.scandir(path.parent) as entries:
            for entry in entries:
                info = entry.stat(follow_symlinks=False)
                if _ORPHAN.fullmatch(entry.name) and stat.S_ISREG(info.st_mode):
                    os.unlink(entry.path)  # orphaned atomic writes; every writer holds this lock
                    continue
                if not _NAME.fullmatch(entry.name) or not stat.S_ISREG(info.st_mode) or entry.is_junction():
                    raise TowError("content.unavailable")
                if entry.name != path.name:
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
        watched = _watched()
        if url not in watched:
            return
        try:
            blob = _read_record(url)
            if blob is not None:
                metadata = parse_torrent_metadata(blob)
                v1, v2 = identities
                if (v1 and metadata.hash_v1 not in v1) or (v2 and metadata.hash_v2 not in v2):
                    _path(url).unlink()
        except TowError, OSError, RuntimeError, ValueError:
            _mark_unavailable(url, True)
            raise
        if watched[url]:
            _mark_unavailable(url, False)
