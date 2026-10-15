"""The transactional add/selection contract, shared by every client.

A client module implements a few primitives (find, add stopped, set wanted files, stop,
start, set labels, move) and inherits the guarantees TOW gives for every client:

- a torrent is added STOPPED and marked "tow" + "tow-pending";
- the file selection is applied exactly and read back before anything starts;
- success is only what the client confirms by read-back; on failure TOW stops what it added;
- TOW only ever stops or reconfigures torrents marked as its own.

``inspect_torrent`` returns the same shape as the qBittorrent adapter (states in qBittorrent
words: stoppedDL/stoppedUP, downloading, uploading, queuedDL/queuedUP, checkingDL, moving,
error), so the rest of TOW does not care which client it talks to.

A client whose own ways differ overrides a step, never the guarantees: qBittorrent keeps its
own add (``_check_add`` and ``_finish_add`` around it), priority levels in
``_restore_selection`` and the read-back of padding in ``_ignored_padding``.
"""

from __future__ import annotations

import copy
import logging
import math
import time
from collections.abc import Callable, Sequence
from typing import Any, ClassVar

from tow.clients import files
from tow.errors import TowError
from tow.folders import paths_equal
from tow.torrent import TorrentFile, TorrentMetadata, parse_torrent_metadata

_LOG = logging.getLogger("tow.clients")

OWNER = "tow"
PENDING = "tow-pending"
STOPPED_PREFIXES = ("stopped", "paused")
UNSAFE_STATES = frozenset({"error", "missingfiles", "unknown"})
# Preparation a client does on its own before a torrent runs, besides checking its files:
# qBittorrent reserves the files' disk space first ("allocating").
PREPARING_STATES = frozenset({"allocating"})
# A receipt's phase: the add was handed over while the client checked the data it found, before
# its selection was applied (``_finish_initial_check`` applies it once the check ends).
AWAITING_INITIAL_CHECK = "awaiting_initial_check"


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


class AddMarkError(Exception):
    """The client confirmed a newly added identity, then its marking request failed."""

    def __init__(self, infohash: str, error: Exception) -> None:
        self.infohash = infohash.upper()
        self.error = error
        super().__init__(str(error))


class ClientVerificationPending(ClientError):
    """The client is checking files; only a validated receipt permits deferring cleanup.

    ``start_attempted``: the receipt's one start (``finish_verification``) was already given."""

    def __init__(self, infohash: str, *, prefix: str, receipt: dict[str, Any] | None = None) -> None:
        self.infohash = infohash.upper()
        self.receipt = copy.deepcopy(receipt)
        self.start_attempted = False
        super().__init__("client.managed.verification_pending", prefix=prefix)


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
    # A start can wait for a real disk check; ownership and selection keep the shorter budget.
    START_POLLS = 600
    START_WAIT_SEC = 60.0
    # How often a start the client dropped is given again before a stopped torrent is judged.
    START_REPEATS = 2
    # How long an add waits for its client's check of the data it found before it is handed over
    # unfinished (``_awaiting_initial_check``). Data already on disk is read at disk speed: the
    # check of a small or partly present torrent ends within a second or two, and 5 s leaves room
    # for a slow disk while a run of many adds never blocks for a minute each (the start wait); a
    # check still running then is finished by the next passes.
    INITIAL_CHECK_GRACE_SEC = 5.0

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

    def _check_state(self, info: dict[str, Any] | None) -> None:
        state = str((info or {}).get("state") or "").casefold()
        if state in UNSAFE_STATES:
            raise self._fail("client.managed.error_state", state=state)

    def _wait(
        self,
        infohash: str,
        done: Any,
        error: str,
        *,
        polls: int | None = None,
        timeout: float | None = None,
        read: Callable[[str], dict[str, Any] | None] | None = None,
    ) -> dict[str, Any]:
        """Poll until ``done(info)``; ``error`` (a catalog key) is the reason when it never comes."""
        last: dict[str, Any] | None = None
        attempts = self.POLLS if polls is None else polls
        deadline = time.monotonic() + timeout if timeout is not None else None
        for attempt in range(attempts):
            if deadline is not None and time.monotonic() >= deadline:
                break
            last = (read or self.inspect_torrent)(infohash)
            if deadline is not None and time.monotonic() >= deadline:
                break
            if last is not None:
                self._check_state(last)
                if done(last):
                    return last
            if attempt + 1 < attempts:
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

    def _wait_started(self, infohash: str, *, repeats: int | None = None) -> dict[str, Any]:
        """The start, confirmed. A torrent the client prepares (checks the files it found,
        reserves their space) has not started yet: Deluge drops a start given while it checks,
        and the torrent stays stopped afterwards, so a stopped torrent is given the start again
        (``repeats`` times, START_REPEATS unless the caller allows fewer). A complete torrent the
        client keeps stopped (a seeding limit already reached) counts once the repeats are spent;
        a preparation still running at the deadline is not a confirmed start: the add keeps its
        pending mark, and a later attempt can complete it once the client is done."""
        allowed = self.START_REPEATS if repeats is None else repeats
        given = 0
        checked = False
        last: dict[str, Any] | None = None

        def read(current_hash: str) -> dict[str, Any] | None:
            nonlocal last
            last = self.inspect_torrent(current_hash)
            return last

        def started(info: dict[str, Any]) -> bool:
            nonlocal given, checked, last
            last = info
            state = str(info.get("state") or "")
            if not state:
                return False
            if self._preparing(info, after_check=checked):
                checked = True
                return False
            if state.casefold() in {"moving", "metadl", "forcedmetadl"}:
                return False
            if not self._stopped(info):
                return self._running(info)
            if given < allowed:
                given += 1
                self._require_owned(infohash)
                self._start(infohash)
                return False
            return completed_progress(info.get("progress"))

        try:
            return self._wait(
                infohash,
                started,
                "client.managed.start_unconfirmed",
                polls=self.START_POLLS,
                timeout=self.START_WAIT_SEC,
                read=read,
            )
        except ClientError as error:
            if error.code == "client.managed.start_unconfirmed" and self._preparing(last, after_check=checked):
                raise ClientVerificationPending(infohash, prefix=self.title) from None
            raise

    def _preparing(self, info: dict[str, Any] | None, *, after_check: bool = False) -> bool:
        """The client prepares the torrent on its own - checks the files it found, or reserves
        their disk space - and has neither started nor refused it yet: waiting for that is never
        a failure and never a reason to stop it. ``after_check``: the torrent was seen checking
        in this transaction (a client that reports a check queue as a download queue says so)."""
        del after_check  # only a client whose queued state is ambiguous needs it (Deluge)
        state = str((info or {}).get("state") or "").casefold()
        return state.startswith("checking") or state in PREPARING_STATES

    def _verification_receipt(
        self, infohash: str, verified: dict[str, Any], metadata: TorrentMetadata, selected: set[int]
    ) -> dict[str, Any]:
        rows = list(verified.get("files") or [])
        mapping = self._map_files(metadata.files, rows, metadata.name)
        if not selected or not selected.issubset(mapping):
            raise self._fail("client.managed.bad_selection")
        return {
            "hash": infohash.upper(),
            "files": [{key: row.get(key) for key in ("index", "name", "size", "priority")} for row in rows],
            "wanted": sorted(mapping[index] for index in selected),
            "ignored": sorted(self._ignored_padding(rows, set(mapping.values()), metadata.files, selected)),
        }

    def _verify_receipt(
        self,
        infohash: str,
        destination: str,
        receipt: dict[str, Any],
        *,
        require_pending: bool,
        applied: bool = True,
    ) -> dict[str, Any]:
        """Fresh identity, ownership, folder and exact selection; never trust task progress.
        ``applied=False``: a selection not applied yet - the file identities, not their flags."""
        if not isinstance(receipt, dict):
            raise self._fail("client.managed.wrong_selection")
        rows, wanted, ignored = receipt.get("files"), receipt.get("wanted"), receipt.get("ignored", [])
        if (
            not isinstance(rows, list)
            or not rows
            or any(not isinstance(row, dict) for row in rows)
            or not isinstance(wanted, list)
            or not wanted
            or not isinstance(ignored, list)
            or any(type(index) is not int or index < 0 for index in [*wanted, *ignored])
        ):
            raise self._fail("client.managed.wrong_selection")
        priorities = files.priorities(rows, fail=lambda: self._fail("client.managed.wrong_selection"))
        padding = {row["index"] for row in rows if self._padding_like(row.get("name"))}
        if (
            not set(ignored).issubset(padding)
            or set(wanted).intersection(ignored)
            or not set(wanted).issubset(priorities)
        ):
            raise self._fail("client.managed.wrong_selection")
        info = self.inspect_torrent(infohash)
        if info is None:
            raise self._fail("client.managed.missing")
        identities = {str(info.get(key) or "").upper() for key in ("hash", "infohash_v1", "infohash_v2")}
        identities.update(value[:40] for value in list(identities) if len(value) == 64)
        if infohash.upper() not in identities or str(receipt.get("hash") or "").upper() != infohash.upper():
            raise self._fail("client.managed.hash_changed_selection")
        if OWNER not in self._tags(info) or (require_pending and PENDING not in self._tags(info)):
            raise self._fail("client.managed.not_owned")
        if not destination or not paths_equal(str(info.get("save_path") or ""), destination):
            raise self._fail("client.managed.wrong_folder")
        self._check_state(info)
        if not applied:
            files.verify_identities(
                rows, list(info.get("files") or []), fail=lambda: self._fail("client.managed.wrong_selection")
            )
            return info
        files.verify_selection(
            rows,
            list(info.get("files") or []),
            set(wanted),
            ignored=set(ignored),
            fail=lambda: self._fail("client.managed.wrong_selection"),
        )
        return info

    def finish_verification(
        self,
        infohash: str,
        destination: str,
        receipt: dict[str, Any],
        *,
        start_stopped: bool = False,
        on_start: Callable[[], None] | None = None,
    ) -> dict[str, Any]:
        """Finish a verified transaction without re-adding, stopping or resetting file selection.

        ``start_stopped``: the caller's one start of TOW's own unfinished transaction, given to a
        stopped torrent whether or not it is complete (the client may have dropped the start it
        got while it checked); at most once per receipt, without the start wait's repeats.
        Checking is a bounded observation, not a blocking wait on every scheduled pass.
        """
        issued = {"start": False}

        def before_start() -> None:
            if on_start is not None:
                on_start()
            issued["start"] = True

        try:
            return self._finish_verification(
                infohash, destination, receipt, start_stopped=start_stopped, on_start=before_start
            )
        except ClientVerificationPending as pending:
            pending.start_attempted = pending.start_attempted or issued["start"]
            raise
        except ClientError as error:
            if issued["start"] and error.code in {
                "client.managed.wrong_selection",
                "client.managed.wrong_folder",
                "client.managed.hash_changed_selection",
                "client.managed.error_state",
            }:
                self._stop_after_failure(infohash, error=error)
            raise  # label/transport uncertainty alone must not interrupt a healthy verification

    def _finish_verification(
        self,
        infohash: str,
        destination: str,
        receipt: dict[str, Any],
        *,
        start_stopped: bool,
        on_start: Callable[[], None],
    ) -> dict[str, Any]:
        if isinstance(receipt, dict) and receipt.get("phase") == AWAITING_INITIAL_CHECK:
            return self._finish_initial_check(infohash, destination, receipt, on_start=on_start)
        info = self._verify_receipt(infohash, destination, receipt, require_pending=False)
        if self._preparing(info, after_check=True):
            raise ClientVerificationPending(infohash, prefix=self.title, receipt=receipt)
        if self._stopped(info) and start_stopped:
            # Also a complete torrent: stopped+complete counts only once it was started (a reached
            # seeding limit may keep it stopped then).
            self._require_owned(infohash)
            on_start()
            self._start(infohash)
            try:
                self._wait_started(infohash, repeats=0)
            except ClientVerificationPending:
                info = self._verify_receipt(infohash, destination, receipt, require_pending=False)
                if self._preparing(info, after_check=True):
                    raise ClientVerificationPending(infohash, prefix=self.title, receipt=receipt) from None
            info = self._verify_receipt(infohash, destination, receipt, require_pending=False)
        self._assert_started(info)
        if PENDING in self._tags(info):
            self._clear_pending(infohash)
        # Clearing the transaction label must not mask an ownership, path or priority change.
        info = self._verify_receipt(infohash, destination, receipt, require_pending=False)
        if self._preparing(info, after_check=True):
            raise ClientVerificationPending(infohash, prefix=self.title, receipt=receipt)
        self._assert_started(info)
        return info

    def _defers_initial_check(self, info: dict[str, Any]) -> bool:
        """The client checks the data it found for a torrent added stopped, and stopping it would
        cancel that check (Transmission): the add is handed over before its selection instead of
        waiting for the check (``_awaiting_initial_check``). No client by default."""
        del info
        return False

    def _verification_state(self, infohash: str) -> dict[str, Any] | None:
        """The torrent's state and tags (None: gone). A client overrides this with a query that
        skips the file list: it is polled while the client checks the files."""
        return self.inspect_torrent(infohash)

    def _after_initial_check(self, infohash: str, info: dict[str, Any]) -> dict[str, Any]:
        """The client's check of the data a new add found, given INITIAL_CHECK_GRACE_SEC to end:
        the torrent read again when it ended (the add goes on as any other), else ``info`` as it
        was - the add is handed over unfinished then."""
        try:
            self._wait(
                infohash,
                lambda current: not self._defers_initial_check(current),
                "client.managed.verification_pending",
                polls=self.START_POLLS,
                timeout=self.INITIAL_CHECK_GRACE_SEC,
                read=self._verification_state,
            )
        except ClientError as error:
            if error.code != "client.managed.verification_pending":
                raise
            return info
        return self._inspect_with_files(infohash)

    def _awaiting_initial_check(
        self,
        infohash: str,
        info: dict[str, Any],
        destination: str,
        metadata: TorrentMetadata,
        selected: set[int],
        *,
        start: bool,
    ) -> ClientVerificationPending:
        """The add, handed over while its client checks: hash, folder and file identities bound to
        the requested selection, not applied yet (``phase``), and whether it starts afterwards. No
        stop, start or selection change happens before the check ends; validated as handed over."""
        rows = list(info.get("files") or [])
        mapping = self._map_files(metadata.files, rows, metadata.name)
        if not selected or not selected.issubset(mapping):
            raise self._fail("client.managed.bad_selection")
        receipt = {
            "hash": infohash.upper(),
            "phase": AWAITING_INITIAL_CHECK,
            "start": start,
            "files": [{key: row.get(key) for key in ("index", "name", "size", "priority")} for row in rows],
            "wanted": sorted(mapping[index] for index in selected),
            "ignored": sorted(self._ignored_padding(rows, set(mapping.values()), metadata.files, selected)),
        }
        self._verify_receipt(infohash, destination, receipt, require_pending=True, applied=False)
        return ClientVerificationPending(infohash, prefix=self.title, receipt=receipt)

    def _finish_initial_check(
        self, infohash: str, destination: str, receipt: dict[str, Any], *, on_start: Callable[[], None]
    ) -> dict[str, Any]:
        """The client's check of an add handed over before its selection ended: the selection is
        applied to the stopped torrent and read back, then it starts when the add asked for it
        and is released - from then on an ordinary receipt. Changed ownership, folder or files
        refuse it before anything is applied; a check still running changes nothing."""
        info = self._verify_receipt(infohash, destination, receipt, require_pending=True, applied=False)
        if self._preparing(info, after_check=True):
            raise ClientVerificationPending(infohash, prefix=self.title, receipt=receipt)
        rows = list(receipt["files"])
        wanted, ignored = set(receipt["wanted"]), set(receipt.get("ignored") or [])
        if not self._stopped(info):
            self._require_owned(infohash)
            self._stop(infohash)
            self._wait_stopped(infohash)
        self._require_owned(infohash)
        self._set_wanted(infohash, wanted, sorted(int(row["index"]) for row in rows))
        verified = self._inspect_with_files(infohash)
        files.verify_selection(
            rows,
            list(verified.get("files") or []),
            wanted,
            ignored=ignored,
            fail=lambda: self._fail("client.managed.wrong_selection"),
        )
        applied = {key: value for key, value in receipt.items() if key not in {"phase", "start"}}
        applied["files"] = [
            {key: row.get(key) for key in ("index", "name", "size", "priority")} for row in verified.get("files") or []
        ]
        if receipt.get("start") is not True:
            return self._clear_pending(infohash)  # released stopped: its files wait for disk space
        self._require_owned(infohash)
        on_start()
        self._start(infohash)
        try:
            self._wait_started(infohash, repeats=0)
        except ClientVerificationPending:
            raise ClientVerificationPending(infohash, prefix=self.title, receipt=applied) from None
        return self._finish_verification(infohash, destination, applied, start_stopped=False, on_start=on_start)

    def _assert_started(self, info: dict[str, Any]) -> None:
        if not self._running(info) and not (self._stopped(info) and completed_progress(info.get("progress"))):
            raise self._fail("client.managed.start_unconfirmed")

    @staticmethod
    def _running(info: dict[str, Any]) -> bool:
        state = str(info.get("state") or "").casefold()
        return state.startswith(("downloading", "uploading", "stalled", "queued", "forceddl", "forcedup"))

    def _after_verification_timeout(
        self,
        infohash: str,
        destination: str,
        verified: dict[str, Any],
        metadata: TorrentMetadata,
        selected: set[int],
        *,
        require_pending: bool,
    ) -> dict[str, Any]:
        receipt = self._verification_receipt(infohash, verified, metadata, selected)
        info = self._verify_receipt(infohash, destination, receipt, require_pending=require_pending)
        if self._preparing(info, after_check=True):
            raise ClientVerificationPending(infohash, prefix=self.title, receipt=receipt)
        self._assert_started(info)
        return info

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
            ignored=self._ignored_padding(rows, mapped_ids, source_files, selected),
        )
        self._require_owned(infohash)
        return verified

    def _ignored_padding(
        self,
        rows: list[dict[str, Any]],
        mapped_ids: set[int],
        source_files: tuple[TorrentFile, ...],
        selected: set[int],
    ) -> set[int]:
        """Client files the selection read-back does not judge: padding no torrent file maps to
        (some clients keep its flag as it was)."""
        return {row["index"] for row in rows if row["index"] not in mapped_ids and self._padding_like(row.get("name"))}

    def _restore_selection(self, infohash: str, previous: dict[int, int], before_rows: list[dict[str, Any]]) -> None:
        """A failed selection change, rolled back on the stopped torrent: the files wanted before
        are wanted again, read back."""
        wanted = {index for index, priority in previous.items() if priority > 0}
        self._require_owned(infohash)
        self._set_wanted(infohash, wanted, sorted(previous))
        restored = self._inspect_with_files(infohash)
        files.verify_selection(
            before_rows,
            list(restored.get("files") or []),
            wanted,
            fail=lambda: self._fail("client.managed.wrong_selection"),
        )

    def _add_label(self, infohash: str, tags: list[str], label: str) -> None:
        """``label`` added to the torrent's current ``tags``."""
        self._set_labels(infohash, [*tags, label])

    def _remove_label(self, infohash: str, tags: list[str], label: str) -> None:
        """``label`` taken off the torrent's current ``tags``."""
        self._set_labels(infohash, [tag for tag in tags if tag.strip().casefold() != label])

    def _clear_pending(self, infohash: str) -> dict[str, Any]:
        self._remove_label(infohash, self._require_owned(infohash), PENDING)
        return self._wait(
            infohash,
            lambda current: OWNER in self._tags(current) and PENDING not in self._tags(current),
            "client.managed.pending_not_cleared",
        )

    def _prepare_add(self) -> None:
        """What must be in place before a torrent is added (Deluge: its labels). Default: nothing."""

    def _mark_after_failed_add(self, aliases: list[str], labels: list[str], error: Exception) -> None:
        """An accepted add's marking failed: retry only its confirmed newly added identity.

        An ambiguous request failure without that proof may finish a torrent already carrying
        both transaction marks, never claim an unmarked task merely because it appeared now.
        A confirmed add that still cannot be marked is reported; otherwise the original error stands.
        """
        accepted = error.infohash if isinstance(error, AddMarkError) else None
        original = error.error if isinstance(error, AddMarkError) else error
        if isinstance(original, ClientError) and original.code in {
            "client.managed.already_there",
            "client.deluge.add_refused",
        }:
            # An explicit refusal proves no add of ours. A torrent that appeared meanwhile
            # may be the owner's, not a partially marked add: never adopt it implicitly.
            raise original
        try:
            present = next(
                (
                    alias
                    for alias in aliases
                    if (accepted is None or alias == accepted) and self._owner_tags(alias) is not None
                ),
                None,
            )
        except Exception:  # noqa: BLE001 - the client does not answer: the add's own error says more
            present = None
        if present is None:
            raise original
        try:
            tags = self._tags({"tags": self._owner_tags(present) or []})
        except Exception:  # noqa: BLE001 - an unreadable identity cannot grant ownership
            raise original from None
        if accepted is None:
            if not {OWNER, PENDING}.issubset(tags):
                raise original  # merely appearing after a timeout does not prove an add of ours
            return  # both marks prove the accepted add without claiming any unmarked torrent
        try:
            if OWNER not in tags:
                self._set_labels(present, labels)
            marked = OWNER in self._tags({"tags": self._owner_tags(present) or []})
        except Exception:  # noqa: BLE001 - reported below as the torrent TOW cannot mark
            marked = False
        if not marked:
            raise self._fail("client.managed.added_unmarked") from error
        _LOG.warning("an add step failed (%s); the torrent is in the client, marked again", type(error).__name__)

    def _check_add(
        self, content: bytes, save_path: str | None, infohash: str, selected_indices: list[int] | tuple[int, ...]
    ) -> tuple[TorrentMetadata, str, set[int]]:
        """An add refused before it reaches the client: another torrent, no folder, or files the
        torrent does not have. Returns the torrent, the folder and the files to download."""
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
        return metadata, destination, selected

    def _stop_after_failure(self, infohash: str, *, error: Exception | None = None) -> None:
        """A failed change stops the torrent it touched (when it is still TOW's)."""
        del error  # optional context for a client's conservative cleanup policy
        try:
            self._require_owned(infohash)
            self._stop(infohash)
        except Exception as cleanup_error:  # noqa: BLE001 - the original failure is re-raised; this must not hide it
            _LOG.warning("cleanup after a failed client change did not finish: %s", cleanup_error)

    def _stop_after_add(self, added: dict[str, Any]) -> bool:
        """Whether the torrent just added must still be stopped (the client started it anyway)."""
        return not self._stopped(added)

    def _ready_for_selection(self, before: dict[str, Any]) -> dict[str, Any]:
        """A read-only client-specific preparation, before the selection transaction."""
        return before

    def _finish_add(
        self,
        infohash: str,
        added: dict[str, Any],
        destination: str,
        metadata: TorrentMetadata,
        selected: set[int],
        *,
        start: bool = True,
    ) -> dict[str, Any]:
        """The add is visible with both marks: its folder, a confirmed stop, the file selection,
        the start (unless ``start`` is False: the files do not fit yet), the release. A failure
        before the release stops the torrent again."""
        release_requested = False
        try:
            if not paths_equal(str(added.get("save_path") or ""), destination):
                raise self._fail("client.managed.wrong_folder")
            if self._defers_initial_check(added) and added.get("files"):
                added = self._after_initial_check(infohash, added)
            if start and self._preparing(added) and added.get("files"):
                try:
                    receipt = self._verification_receipt(infohash, added, metadata, selected)
                    self._verify_receipt(infohash, destination, receipt, require_pending=True)
                except ClientError as error:
                    if error.code not in {
                        "client.managed.wrong_selection",
                        "client.managed.file_unmatched",
                        "client.managed.bad_selection",
                    }:
                        raise
                else:
                    return self.finish_verification(infohash, destination, receipt, start_stopped=True)
            if self._defers_initial_check(added) and added.get("files"):
                raise self._awaiting_initial_check(infohash, added, destination, metadata, selected, start=start)
            if self._stop_after_add(added):
                self._require_owned(infohash)
                self._stop(infohash)
            self._wait_stopped(infohash)
            verified = self._apply_selection(infohash, metadata.files, selected, metadata.name)
            if start:
                self._require_owned(infohash)
                self._start(infohash)
                try:
                    self._wait_started(infohash)
                except ClientVerificationPending:
                    self._after_verification_timeout(
                        infohash, destination, verified, metadata, selected, require_pending=True
                    )
            release_requested = True
            return self._clear_pending(infohash)
        except Exception as error:
            if not release_requested and not (isinstance(error, ClientVerificationPending) and error.receipt):
                self._stop_after_failure(infohash, error=error)
            raise

    def add_torrent_selected(
        self,
        content: bytes,
        save_path: str | None,
        infohash: str,
        selected_indices: list[int] | tuple[int, ...],
        *,
        start: bool = True,
    ) -> dict[str, Any]:
        metadata, destination, selected = self._check_add(content, save_path, infohash, selected_indices)
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
        try:
            added = self._wait_owned(infohash)
        except Exception:
            self._stop_after_failure(infohash)
            raise
        return self._finish_add(infohash, added, destination, metadata, selected, start=start)

    def configure_torrent_selection(
        self,
        content: bytes,
        infohash: str,
        selected_indices: list[int] | tuple[int, ...],
        *,
        ensure_started: bool = False,
        keep_stopped: bool = False,
    ) -> dict[str, Any]:
        """The new file selection, read back. The torrent runs afterwards when it ran before,
        when ``ensure_started`` asks it, and when it is an unfinished add of TOW's (pending) -
        never with ``keep_stopped``: its files do not fit on the drive (``tow.check.space``)."""
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
        selected = {int(index) for index in selected_indices}
        if pending and self._defers_initial_check(before):
            before = self._after_initial_check(infohash, before)
        if pending and self._preparing(before) and not keep_stopped:
            try:
                receipt = self._verification_receipt(infohash, before, metadata, selected)
                self._verify_receipt(infohash, str(before.get("save_path") or ""), receipt, require_pending=True)
            except ClientError as error:
                if error.code not in {
                    "client.managed.wrong_selection",
                    "client.managed.file_unmatched",
                    "client.managed.bad_selection",
                }:
                    raise
            else:
                return self.finish_verification(
                    infohash, str(before.get("save_path") or ""), receipt, start_stopped=True
                )
        if pending and self._defers_initial_check(before):
            raise self._awaiting_initial_check(
                infohash, before, str(before.get("save_path") or ""), metadata, selected, start=not keep_stopped
            )
        before = self._ready_for_selection(before)
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
            verified = self._apply_selection(infohash, metadata.files, selected, metadata.name)
            if not keep_stopped and (pending or not was_stopped or ensure_started):
                self._require_owned(infohash)
                self._start(infohash)
                try:
                    started = self._wait_started(infohash)
                except ClientVerificationPending:
                    started = self._after_verification_timeout(
                        infohash,
                        str(before.get("save_path") or ""),
                        verified,
                        metadata,
                        selected,
                        require_pending=pending,
                    )
                verified = started
            if pending:
                release_requested = True
                verified = self._clear_pending(infohash)
            return verified
        except Exception as error:
            if release_requested or (isinstance(error, ClientVerificationPending) and error.receipt):
                raise
            try:
                self._require_owned(infohash)
                self._stop(infohash)
                self._wait_stopped(infohash)
                self._restore_selection(infohash, previous, list(before.get("files") or []))
                if not was_stopped:
                    self._require_owned(infohash)
                    self._start(infohash)
                    self._wait_started(infohash)
            except Exception as cleanup_error:  # noqa: BLE001 - the original failure is re-raised; this must not hide it
                _LOG.warning("cleanup after a failed client change did not finish: %s", cleanup_error)
            raise

    def adopt_torrent(self, infohash: str, *, replace_label: bool = False) -> list[str]:
        """The owner's "adopt into TOW": TOW's mark on a torrent already in the client, and
        nothing else - its files, folder, file selection and state stay as they are. The mark is
        read back; returns the torrent's tags (labels) then. ``replace_label``: a client that
        keeps one label per torrent may replace the owner's (``_keeps_owner_label``)."""
        tags = self._owner_tags(infohash)
        if tags is None:
            raise self._fail("client.managed.missing")
        if OWNER not in self._tags({"tags": tags}):
            if not replace_label:
                self._keeps_owner_label(tags)
            self._add_label(infohash, tags, OWNER)
            tags = self._owner_tags(infohash) or []
            if OWNER not in self._tags({"tags": tags}):
                raise self._fail("client.managed.adopt_unconfirmed")
        return tags

    def _keeps_owner_label(self, tags: list[str]) -> None:
        """Refuse a mark that would take the owner's own label away (a client with one label per
        torrent); a client that keeps several tags adds TOW's beside them."""

    def stop_owned_torrent(self, infohash: str) -> dict[str, Any]:
        info = self.inspect_torrent(infohash)
        if info is None:
            raise self._fail("client.managed.missing")
        if OWNER not in self._tags(info):
            raise self._fail("client.managed.not_owned_stop")
        if self._stopped(info):
            return info
        self._require_owned(infohash)
        self._stop(infohash)
        return self._wait_stopped(infohash)

    def start_owned_torrent(self, infohash: str) -> dict[str, Any]:
        """Start TOW's own stopped torrent (one added stopped while its files did not fit); the
        start is read back. A torrent already running is left as it is."""
        info = self.inspect_torrent(infohash)
        if info is None:
            raise self._fail("client.managed.missing")
        if OWNER not in self._tags(info):
            raise self._fail("client.managed.not_owned")
        if not self._stopped(info):
            return info
        self._require_owned(infohash)
        self._start(infohash)
        return self._wait_started(infohash)

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
