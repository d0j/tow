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
import math
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


def completed_progress(value: object) -> bool:
    """Only a valid, complete fraction can justify a stopped torrent after start."""
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        return False
    try:
        progress = float(value)
    except TypeError, ValueError, OverflowError:
        return False
    return math.isfinite(progress) and progress == 1.0


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
        "metadata_preview": False,
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
        # A client's own answer can quote a magnet or announce address with the owner's passkey:
        # the error goes to the log, the Home row and the messengers.
        from tow.log import scrub_text

        clean = {key: scrub_text(value) if isinstance(value, str) else value for key, value in params.items()}
        return ClientError(code, prefix=self.title, **clean)

    @staticmethod
    def _tags(info: dict[str, Any] | None) -> set[str]:
        return {str(tag).strip().casefold() for tag in (info or {}).get("tags") or []}

    @staticmethod
    def _stopped(info: dict[str, Any] | None) -> bool:
        return str((info or {}).get("state") or "").casefold().startswith(STOPPED_PREFIXES)

    def _owner_tags(self, infohash: str) -> list[str] | None:
        """The torrent's tags, None when it is gone. A client overrides this with a query that
        skips the file list: it runs before every mutation, also of very large torrents."""
        info = self.inspect_torrent(infohash)
        return None if info is None else [str(tag) for tag in info.get("tags") or []]

    def _require_owned(self, infohash: str) -> list[str]:
        """Re-read the owner mark right before every mutation; returns the current tags."""
        tags = self._owner_tags(infohash) or []
        if OWNER not in self._tags({"tags": tags}):
            raise self._fail("client.managed.not_owned")
        return tags

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
        except ClientError as error:
            if error.code != "client.managed.not_visible":
                raise
            if not seen:
                raise self._fail("client.managed.not_visible") from None
            raise self._fail("client.managed.no_owner_mark") from None

    def _wait_stopped(self, infohash: str) -> dict[str, Any]:
        return self._wait(infohash, self._stopped, "client.managed.stop_unconfirmed")

    def _wait_started(self, infohash: str) -> dict[str, Any]:
        def started(info: dict[str, Any]) -> bool:
            if not str(info.get("state") or ""):
                return False
            return not self._stopped(info) or completed_progress(info.get("progress"))

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
        files.priorities(rows, fail=lambda: self._fail("client.managed.wrong_selection"))
        mapping = self._map_files(source_files, rows, root_name)
        mapped_ids = set(mapping.values())
        if not selected or any(index not in mapping for index in selected):
            raise self._fail("client.managed.bad_selection")
        all_ids = sorted(int(row["index"]) for row in rows if isinstance(row.get("index"), int))
        wanted = {mapping[index] for index in selected}
        self._require_owned(infohash)
        self._set_wanted(infohash, wanted, all_ids)
        verified = self._inspect_with_files(infohash)
        files.verify_selection(
            rows,
            list(verified.get("files") or []),
            wanted,
            fail=lambda: self._fail("client.managed.wrong_selection"),
            ignored={
                row["index"] for row in rows if row["index"] not in mapped_ids and self._padding_like(row.get("name"))
            },
        )
        self._require_owned(infohash)
        return verified

    def _clear_pending(self, infohash: str) -> dict[str, Any]:
        labels = [tag for tag in self._require_owned(infohash) if tag.strip().casefold() != PENDING]
        self._set_labels(infohash, labels)
        return self._wait(
            infohash,
            lambda current: OWNER in self._tags(current) and PENDING not in self._tags(current),
            "client.managed.pending_not_cleared",
        )

    def _prepare_add(self) -> None:
        """What must be in place before a torrent is added (Deluge: its labels). Default: nothing."""

    def _mark_after_failed_add(self, aliases: list[str], labels: list[str], error: Exception) -> None:
        """The add failed, maybe after the client took the torrent (its marking step failed or
        did not answer). A torrent there now is TOW's - it was not there a moment ago - and is
        stopped: it gets TOW's mark again, and the add goes on. One that cannot be marked is
        reported so (TOW could never manage it); with nothing added, the error stands."""
        if isinstance(error, ClientError) and error.code == "client.managed.already_there":
            raise error
        try:
            present = next((alias for alias in aliases if self._owner_tags(alias) is not None), None)
        except Exception:  # noqa: BLE001 - the client does not answer: the add's own error says more
            present = None
        if present is None:
            raise error
        try:
            if OWNER not in self._tags({"tags": self._owner_tags(present) or []}):
                self._set_labels(present, labels)
            marked = OWNER in self._tags({"tags": self._owner_tags(present) or []})
        except Exception:  # noqa: BLE001 - reported below as the torrent TOW cannot mark
            marked = False
        if not marked:
            raise self._fail("client.managed.added_unmarked") from error
        _LOG.warning("an add step failed (%s); the torrent is in the client, marked again", type(error).__name__)

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
        self._prepare_add()
        labels = self._labels_for_add()
        try:
            self._add_stopped(content, destination, labels)
        except Exception as error:  # noqa: BLE001 - re-raised unless the torrent is there and marked again
            self._mark_after_failed_add(aliases, labels, error)
        # A hybrid (v1+v2) torrent is listed by most clients under its v1 hash: use what the
        # client actually shows from here on (the check accepts any of the torrent's hashes).
        infohash = self._visible_alias(aliases)
        release_requested = False
        try:
            added = self._wait_owned(infohash)
            if not paths_equal(str(added.get("save_path") or ""), destination):
                raise self._fail("client.managed.wrong_folder")
            if not self._stopped(added):
                self._require_owned(infohash)
                self._stop(infohash)
            self._wait_stopped(infohash)
            self._apply_selection(infohash, metadata.files, selected, metadata.name)
            self._require_owned(infohash)
            self._start(infohash)
            self._wait_started(infohash)
            release_requested = True
            return self._clear_pending(infohash)
        except Exception:
            if not release_requested:
                try:
                    self._require_owned(infohash)
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
        previous = files.priorities(
            list(before.get("files") or []), fail=lambda: self._fail("client.managed.wrong_selection")
        )
        was_stopped = self._stopped(before)
        release_requested = False
        try:
            self._require_owned(infohash)
            self._stop(infohash)
            self._wait_stopped(infohash)
            verified = self._apply_selection(
                infohash, metadata.files, {int(index) for index in selected_indices}, metadata.name
            )
            if pending or not was_stopped or ensure_started:
                self._require_owned(infohash)
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
                self._require_owned(infohash)
                self._stop(infohash)
                self._wait_stopped(infohash)
                self._require_owned(infohash)
                self._set_wanted(infohash, {i for i, p in previous.items() if p > 0}, sorted(previous))
                restored = self._inspect_with_files(infohash)
                files.verify_selection(
                    list(before.get("files") or []),
                    list(restored.get("files") or []),
                    {i for i, p in previous.items() if p > 0},
                    fail=lambda: self._fail("client.managed.wrong_selection"),
                )
                if not was_stopped:
                    self._require_owned(infohash)
                    self._start(infohash)
                    self._wait_started(infohash)
            except Exception as cleanup_error:  # noqa: BLE001 - the original failure is re-raised; this must not hide it
                _LOG.warning("cleanup after a failed client change did not finish: %s", cleanup_error)
            raise

    def adopt_torrent(self, infohash: str) -> list[str]:
        """The owner's "adopt into TOW": TOW's mark on a torrent already in the client, and
        nothing else - its files, folder, file selection and state stay as they are. The mark is
        read back; returns the torrent's tags (labels) then."""
        tags = self._owner_tags(infohash)
        if tags is None:
            raise self._fail("client.managed.missing")
        if OWNER not in self._tags({"tags": tags}):
            self._set_labels(infohash, [*tags, OWNER])
            tags = self._owner_tags(infohash) or []
            if OWNER not in self._tags({"tags": tags}):
                raise self._fail("client.managed.adopt_unconfirmed")
        return tags

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
        self._require_owned(infohash)
        self._move(infohash, destination)
        return "ok"

    def materialize_magnet(self, magnet_url: str, save_path: str | None, infohash: str) -> bytes:
        raise self._fail("client.managed.no_magnet")

    def preview_magnet(self, magnet_url: str) -> bytes:
        raise self._fail("content.magnet_unsupported")
