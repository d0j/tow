"""The transactional add/selection contract, shared by every client except qBittorrent.

A client module implements a few primitives (find, add stopped, set wanted files, stop,
start, set labels, move) and inherits the guarantees TOW gives for every client:

- a torrent is added STOPPED and marked "tow" + "tow-pending";
- the file selection is applied exactly and read back before anything starts;
- success is only what the client confirms by read-back; on failure TOW stops what it added;
- TOW only ever stops or reconfigures torrents marked as its own.

``inspect_torrent`` returns the same shape as the qBittorrent adapter (states in qBittorrent
words: stoppedDL/stoppedUP, downloading, uploading, queuedDL/queuedUP, checkingDL, moving,
error), so the rest of TOW does not care which client it talks to.
"""

from __future__ import annotations

import logging
import time
from collections.abc import Sequence
from typing import Any, ClassVar

from tow.clients import files
from tow.errors import TowError
from tow.folders import paths_equal
from tow.torrent import TorrentFile, parse_torrent_metadata

_LOG = logging.getLogger("tow.clients")

OWNER = "tow"
PENDING = "tow-pending"
STOPPED_PREFIXES = ("stopped", "paused")
UNSAFE_STATES = frozenset({"error", "missingfiles", "unknown"})


class ClientError(TowError, RuntimeError):
    """A plain-language client failure (shown to the owner, never contains a password).

    Its code is a key of the language files: ``client.managed.*`` for the shared contract,
    ``client.<kind>.*`` for one client; ``_fail`` puts the client's name in front. Its class is
    ``qbit`` (a torrent-client failure) unless ``tow.errors.CLASSES`` says otherwise."""

    default_class = "qbit"


class ManagedClient:
    title = "client"
    kind = "client"
    client_kind = "client"
    client_id = "default"
    add_category = ""
    add_tags: Sequence[str] = ()
    read_only = False  # a dry run: a client with a mode that changes nothing honours it
    capabilities: ClassVar[dict[str, bool]] = {
        "inspect": True,
        "list_files": True,
        "completion_time": True,
        "add": True,
        "stopped_add": True,
        "file_selection": True,
        "priority_readback": True,
        "start_stop": True,
        "magnet_metadata": False,
    }
    # Read-back polling: how many times and how often (tests set the pause to zero).
    POLLS = 50
    PAUSE = 0.1

    # --- primitives every client implements ---------------------------------------------------

    def ping(self) -> str:
        raise NotImplementedError

    def inspect_torrent(self, infohash: str) -> dict[str, Any] | None:
        """{hash, infohash_v1, infohash_v2, progress, downloaded, added_on, completion_on,
        save_path, content_path, state, tags, files: [{index, name, size, progress, priority}]}"""
        raise NotImplementedError

    def _add_stopped(self, content: bytes, save_path: str, labels: list[str]) -> None:
        raise NotImplementedError

    def _set_wanted(self, infohash: str, wanted: set[int], all_ids: list[int]) -> None:
        """Client file ids in ``wanted`` download, every other id in ``all_ids`` is skipped."""
        raise NotImplementedError

    def _stop(self, infohash: str) -> None:
        raise NotImplementedError

    def _start(self, infohash: str) -> None:
        raise NotImplementedError

    def _set_labels(self, infohash: str, labels: list[str]) -> None:
        raise NotImplementedError

    def _move(self, infohash: str, save_path: str) -> None:
        raise NotImplementedError

    # --- shared contract ----------------------------------------------------------------------

    def has_hash(self, infohash: str) -> bool:
        return self.inspect_torrent(infohash) is not None

    def _sleep(self) -> None:
        if self.PAUSE:
            time.sleep(self.PAUSE)

    def _fail(self, code: str, /, **params: Any) -> ClientError:
        return ClientError(code, prefix=self.title, **params)

    @staticmethod
    def _tags(info: dict[str, Any] | None) -> set[str]:
        return {str(tag).strip().casefold() for tag in (info or {}).get("tags") or []}

    @staticmethod
    def _stopped(info: dict[str, Any] | None) -> bool:
        return str((info or {}).get("state") or "").casefold().startswith(STOPPED_PREFIXES)

    def _labels_for_add(self) -> list[str]:
        extra = [str(tag).strip() for tag in self.add_tags or () if str(tag).strip()]
        category = str(self.add_category or "").strip()
        labels = [OWNER, PENDING, *([category] if category else []), *extra]
        return list(dict.fromkeys(label for label in labels))

    def _wait(self, infohash: str, done: Any, error: str) -> dict[str, Any]:
        """Poll until ``done(info)``; ``error`` (a catalog key) is the reason when it never comes."""
        last: dict[str, Any] | None = None
        for attempt in range(self.POLLS):
            last = self.inspect_torrent(infohash)
            if last is not None:
                state = str(last.get("state") or "").casefold()
                if state in UNSAFE_STATES:
                    raise self._fail("client.managed.error_state", state=state)
                if done(last):
                    return last
            if attempt + 1 < self.POLLS:
                self._sleep()
        raise self._fail(error)

    def _visible_alias(self, aliases: list[str]) -> str:
        for attempt in range(self.POLLS):
            for alias in aliases:
                if self.inspect_torrent(alias) is not None:
                    return alias
            if attempt + 1 < self.POLLS:
                self._sleep()
        return aliases[0]  # _wait_owned reports "not visible" with the usual wording

    def _wait_owned(self, infohash: str) -> dict[str, Any]:
        seen = False

        def owned(info: dict[str, Any]) -> bool:
            nonlocal seen
            seen = True
            return {OWNER, PENDING}.issubset(self._tags(info))

        try:
            return self._wait(infohash, owned, "client.managed.not_visible")
        except ClientError:
            if not seen:
                raise self._fail("client.managed.not_visible") from None
            raise self._fail("client.managed.no_owner_mark") from None

    def _wait_stopped(self, infohash: str) -> dict[str, Any]:
        return self._wait(infohash, self._stopped, "client.managed.stop_unconfirmed")

    def _wait_started(self, infohash: str) -> dict[str, Any]:
        def started(info: dict[str, Any]) -> bool:
            if not str(info.get("state") or ""):
                return False
            return not self._stopped(info) or float(info.get("progress") or 0) >= 1

        return self._wait(infohash, started, "client.managed.start_unconfirmed")

    def _inspect_with_files(self, infohash: str) -> dict[str, Any]:
        return self._wait(
            infohash,
            lambda info: bool(info.get("files")),
            "client.managed.no_files",
        )

    @classmethod
    def _padding_like(cls, value: object) -> bool:
        return files.padding_like(value)

    def _map_files(
        self, source_files: tuple[TorrentFile, ...], client_files: list[dict[str, Any]], root_name: str
    ) -> dict[int, int]:
        """Torrent file index -> client file id, by path and size (never by position alone)."""
        return files.map_files(
            source_files,
            client_files,
            root_name,
            fail=lambda path: self._fail("client.managed.file_unmatched", path=path),
        )

    def _apply_selection(
        self, infohash: str, source_files: tuple[TorrentFile, ...], selected: set[int], root_name: str
    ) -> dict[str, Any]:
        inspected = self._inspect_with_files(infohash)
        rows = list(inspected.get("files") or [])
        mapping = self._map_files(source_files, rows, root_name)
        if not selected or any(index not in mapping for index in selected):
            raise self._fail("client.managed.bad_selection")
        all_ids = sorted(int(row["index"]) for row in rows if isinstance(row.get("index"), int))
        wanted = {mapping[index] for index in selected}
        self._set_wanted(infohash, wanted, all_ids)
        verified = self._inspect_with_files(infohash)
        priorities = {
            int(row["index"]): int(row.get("priority") or 0)
            for row in verified.get("files") or []
            if isinstance(row.get("index"), int)
        }
        for client_id in all_ids:
            name = next((row.get("name") for row in rows if row.get("index") == client_id), "")
            if client_id not in wanted and self._padding_like(name):
                continue  # padding files are never downloaded; their flag does not matter
            if (priorities.get(client_id, 0) > 0) != (client_id in wanted):
                raise self._fail("client.managed.wrong_selection")
        return verified

    def _clear_pending(self, infohash: str) -> dict[str, Any]:
        info = self.inspect_torrent(infohash) or {}
        labels = [str(tag) for tag in info.get("tags") or [] if str(tag).casefold() != PENDING]
        if OWNER not in {label.casefold() for label in labels}:
            labels.insert(0, OWNER)
        self._set_labels(infohash, labels)
        return self._wait(
            infohash,
            lambda current: OWNER in self._tags(current) and PENDING not in self._tags(current),
            "client.managed.pending_not_cleared",
        )

    def add_torrent_selected(
        self,
        content: bytes,
        save_path: str | None,
        infohash: str,
        selected_indices: list[int] | tuple[int, ...],
    ) -> dict[str, Any]:
        metadata = parse_torrent_metadata(content)
        if metadata.client_hash.casefold() != str(infohash).casefold():
            raise self._fail("client.managed.hash_changed_add")
        destination = str(save_path or "").strip()
        if not destination:
            raise self._fail("client.managed.no_folder")
        selected = {int(index) for index in selected_indices}
        valid = {row.index for row in metadata.files if not row.is_pad}
        if not selected or not selected.issubset(valid):
            raise self._fail("client.managed.bad_selection")
        aliases = list(
            dict.fromkeys(
                value.upper()
                for value in (infohash, metadata.hash_v1, metadata.hash_v2[:40] if metadata.hash_v2 else None)
                if value
            )
        )
        if any(self.inspect_torrent(alias) is not None for alias in aliases):
            raise self._fail("client.managed.already_there")
        self._add_stopped(content, destination, self._labels_for_add())
        # A hybrid (v1+v2) torrent is listed by most clients under its v1 hash: use what the
        # client actually shows from here on (the check accepts any of the torrent's hashes).
        infohash = self._visible_alias(aliases)
        release_requested = False
        try:
            added = self._wait_owned(infohash)
            if not paths_equal(str(added.get("save_path") or ""), destination):
                raise self._fail("client.managed.wrong_folder")
            if not self._stopped(added):
                self._stop(infohash)
            self._wait_stopped(infohash)
            self._apply_selection(infohash, metadata.files, selected, metadata.name)
            self._start(infohash)
            self._wait_started(infohash)
            release_requested = True
            return self._clear_pending(infohash)
        except Exception:
            if not release_requested:
                try:
                    self._stop(infohash)
                except Exception as cleanup_error:  # noqa: BLE001 - the original failure is re-raised; this must not hide it
                    _LOG.warning("cleanup after a failed client change did not finish: %s", cleanup_error)
            raise

    def configure_torrent_selection(
        self,
        content: bytes,
        infohash: str,
        selected_indices: list[int] | tuple[int, ...],
        *,
        ensure_started: bool = False,
    ) -> dict[str, Any]:
        metadata = parse_torrent_metadata(content)
        valid_hashes = {metadata.client_hash.casefold()}
        if metadata.hash_v1:
            valid_hashes.add(metadata.hash_v1.casefold())
        if str(infohash).casefold() not in valid_hashes:
            raise self._fail("client.managed.hash_changed_selection")
        before = self._inspect_with_files(infohash)
        if OWNER not in self._tags(before):
            raise self._fail("client.managed.not_owned")
        pending = PENDING in self._tags(before)
        previous = {
            int(row["index"]): int(row.get("priority") or 0)
            for row in before.get("files") or []
            if isinstance(row.get("index"), int)
        }
        was_stopped = self._stopped(before)
        release_requested = False
        try:
            self._stop(infohash)
            self._wait_stopped(infohash)
            verified = self._apply_selection(
                infohash, metadata.files, {int(index) for index in selected_indices}, metadata.name
            )
            if pending or not was_stopped or ensure_started:
                self._start(infohash)
                verified = self._wait_started(infohash)
            if pending:
                release_requested = True
                verified = self._clear_pending(infohash)
            return verified
        except Exception:
            if release_requested:
                raise
            try:
                self._stop(infohash)
                self._set_wanted(infohash, {i for i, p in previous.items() if p > 0}, sorted(previous))
                if not was_stopped:
                    self._start(infohash)
            except Exception as cleanup_error:  # noqa: BLE001 - the original failure is re-raised; this must not hide it
                _LOG.warning("cleanup after a failed client change did not finish: %s", cleanup_error)
            raise

    def stop_owned_torrent(self, infohash: str) -> dict[str, Any]:
        info = self.inspect_torrent(infohash)
        if info is None:
            raise self._fail("client.managed.missing")
        if OWNER not in self._tags(info):
            raise self._fail("client.managed.not_owned_stop")
        if self._stopped(info):
            return info
        self._stop(infohash)
        return self._wait_stopped(infohash)

    def set_location(self, infohash: str, save_path: str) -> str:
        destination = (save_path or "").strip()
        if not destination:
            raise self._fail("client.managed.no_folder")
        self._move(infohash, destination)
        return "ok"

    def materialize_magnet(self, magnet_url: str, save_path: str | None, infohash: str) -> bytes:
        raise self._fail("client.managed.no_magnet")
