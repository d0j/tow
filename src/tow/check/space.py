"""Waiting for disk space (G6): a new revision whose selected files do not fit on the target
drive is added STOPPED with its file selection, confirmed by read-back like every add, and
started by TOW itself once there is room.

The topic keeps ``waiting_space`` = {hash, client, needed, free, path, since, selection, kind}
(``selection`` fingerprints the files the client downloads, ``kind`` is the message the start
sends: ``added`` or ``updated``). Its error (``check.waiting_space``, class ``disk``: red, the
owner can free space) is reported once. While it waits, a check asks only the client, never
the site: no new ``.torrent`` download, no second add. Every check and the supervisor's
``space`` pass (every few minutes, only while a topic waits) look again: still short - the
numbers are refreshed; room now - TOW starts it, reads the start back and tells the owner;
gone, foreign, started or reselected by the owner - the waiting ends and the normal check
takes over. A dry run never starts anything.

Only NEW bytes count (M5): selected files already in the folder are not downloaded again. A
folder this computer does not see (a remote client's ``/downloads``, a missing drive) and
free space that cannot be read never make a topic wait.
"""

from __future__ import annotations

import hashlib
import math
import shutil
from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from pathlib import Path
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any

from tow.check.client_ops import (
    PREVIOUS_REVISION_ACTIVE,
    client_unreachable,
    info_confirms,
    info_owned_by_tow,
    live_previous_overlap,
)
from tow.check.rows import client_marks, fail_row, set_error, stamp_result, withdrawn_meanwhile
from tow.clients.managed import completed_progress
from tow.clients.spec import TorrentClientAdapter
from tow.clock import iso_now
from tow.errors import TowError
from tow.events import new_operation_id
from tow.jsonish import as_dict
from tow.log import error_fields
from tow.records import Topic, topics_of

if TYPE_CHECKING:
    from tow.check.topic import CheckRun

# Keep this much free beyond the torrent itself (temporary files, other downloads).
FREE_SPACE_MARGIN = 512 * 1024 * 1024
WAITING_SPACE = "check.waiting_space"
_GIB = 1024**3
_STOPPED = ("stopped", "paused")
# The states of a running torrent, in qBittorrent's words (every adapter reports these).
_RUNNING = ("downloading", "uploading", "stalled", "queued", "forced", "metadl")

# What a look at a waiting topic found (``recheck``).
WAITING, STARTED, WOULD_START = "waiting", "started", "would_start"
GONE, FOREIGN, OWNER, STALE = "gone", "foreign", "owner", "stale"
WITHDRAWN = "withdrawn"  # the owner paused, deleted or edited the topic while the run went on


@dataclass(frozen=True)
class Shortfall:
    """The selected files do not fit: their new bytes, the free space, the folder measured."""

    needed: int
    free: int
    path: str

    @property
    def missing(self) -> int:
        """What must be freed before TOW starts the torrent (the margin included)."""
        return max(0, self.needed + FREE_SPACE_MARGIN - self.free)

    def error(self) -> TowError:
        # Numbers, not text: the reader's language writes their decimal sign.
        return TowError(
            "check.waiting_space",
            missing=max(0.1, math.ceil(self.missing / _GIB * 10) / 10),
            needed=round(self.needed / _GIB, 1),
            free=round(self.free / _GIB, 1),
            path=self.path,
        )


def _on_disk(candidates: Iterable[Path], size: int) -> int:
    """How much of a file of ``size`` bytes already lies at one of ``candidates``."""
    for candidate in candidates:
        try:
            if candidate.is_file():
                return min(int(candidate.stat().st_size), size)
        except OSError, ValueError:
            continue
    return 0


def measure(dest: str, wanted: Iterable[tuple[tuple[Path, ...], int, int]]) -> Shortfall | None:
    """Whether the files ``wanted`` - (where each may already lie, its size, the bytes the
    client already has) - fit in ``dest``; None when they do or when it cannot be known."""
    return _measured(dest, wanted)[1]


def _measured(dest: str, wanted: Iterable[tuple[tuple[Path, ...], int, int]]) -> tuple[bool, Shortfall | None]:
    """``measure`` with whether it is known at all: (False, None) for a folder this computer
    does not see or free space it cannot read - a new add then does not wait, but a torrent
    that already waits is not started on a drive that is gone for the moment."""
    from tow.folders import seen_from_here

    items = list(wanted)
    needed = sum(size for _where, size, _done in items)
    if needed <= 0:
        return True, None
    if not seen_from_here(dest):
        return False, None  # /downloads of a remote client, or a drive this PC does not have
    needed -= sum(min(size, max(done, _on_disk(where, size))) for where, size, done in items)
    if needed <= 0:
        return True, None
    probe = Path(dest)
    while not probe.exists() and probe.parent != probe:
        probe = probe.parent
    try:
        free = shutil.disk_usage(probe).free
    except OSError:
        return False, None
    if needed + FREE_SPACE_MARGIN <= free:
        return True, None
    return True, Shortfall(needed=needed, free=free, path=str(probe))


def folder_free(dest: str) -> dict[str, Any]:
    """The free space the add and edit forms show for a folder, measured as an add measures it:
    ``free`` in bytes (None when this computer does not see the folder or cannot read it) and
    the ``margin`` an add keeps. A hint only: the add itself decides (``measure``)."""
    from tow.folders import seen_from_here

    value = str(dest or "").strip()
    free: int | None = None
    if value and len(value) <= 4096 and seen_from_here(value):
        probe = Path(value)
        while not probe.exists() and probe.parent != probe:
            probe = probe.parent
        try:
            free = shutil.disk_usage(probe).free
        except OSError, ValueError:
            free = None
    return {"free": free, "margin": FREE_SPACE_MARGIN}


def _wanted_rows(info: dict[str, Any]) -> Iterator[dict[str, Any]]:
    """The client's files of the torrent it downloads (priority above 0)."""
    for row in info.get("files") or []:
        try:
            if isinstance(row, dict) and int(row.get("priority") or 0) > 0:
                yield row
        except TypeError, ValueError:
            continue


def _client_wanted(info: dict[str, Any], dest: str) -> Iterator[tuple[tuple[Path, ...], int, int]]:
    """The selected files as the client reports them (names relative to its save folder)."""
    for row in _wanted_rows(info):
        size = int(row.get("size") or 0)
        try:
            progress = min(1.0, max(0.0, float(row.get("progress") or 0)))
        except TypeError, ValueError:
            progress = 0.0
        name = str(row.get("name") or "").replace("\\", "/")
        where = (Path(dest, *[part for part in name.split("/") if part]),) if name else ()
        yield where, size, int(size * progress)


def selection_key(info: dict[str, Any] | None) -> str:
    """A fingerprint of which files the client downloads: the owner changing them ends the wait."""
    indexes = sorted(str(row.get("index")) for row in _wanted_rows(info or {}))
    return hashlib.sha256(",".join(indexes).encode()).hexdigest()[:16]


def is_stopped(info: dict[str, Any] | None) -> bool:
    """The client keeps the torrent stopped (or paused, as qBittorrent 4 says)."""
    return str((info or {}).get("state") or "").casefold().startswith(_STOPPED)


def _running(info: dict[str, Any] | None) -> bool:
    """The client runs the torrent (someone started it). Not a state it passes through on its
    own - loading after a restart, checking, moving the files - nor an error state."""
    return str((info or {}).get("state") or "").casefold().startswith(_RUNNING)


def waiting_of(topic: Topic) -> dict[str, Any]:
    return as_dict(topic.get("waiting_space"))


def waiting_topics(state: dict[str, Any]) -> list[Topic]:
    """The topics whose revision waits for room (a paused one is left alone, as by checks)."""
    return [topic for topic in topics_of(state) if waiting_of(topic) and not topic.get("paused")]


def waiting_for(topic: Topic, infohash: str) -> dict[str, Any] | None:
    """The topic's waiting record when it is about this very torrent."""
    waiting = waiting_of(topic)
    return waiting if waiting and str(waiting.get("hash") or "").upper() == infohash.upper() else None


def forget(topic: Topic) -> None:
    topic.pop("waiting_space", None)


def wait_for_space(
    topic: Topic,
    run: CheckRun,
    row: dict[str, Any],
    *,
    h: str,
    client_id: str,
    info: dict[str, Any] | None,
    shortfall: Shortfall,
    kind: str,
) -> None:
    """The revision is in the client, stopped and TOW's: the topic waits for room. Its error
    says so (the owner hears it once - an error is reported when it starts or changes class)."""
    earlier = waiting_for(topic, h) or {}
    topic["waiting_space"] = {
        "hash": h,
        "client": client_id,
        "needed": shortfall.needed,
        "free": shortfall.free,
        "path": shortfall.path,
        "since": str(earlier.get("since") or iso_now()),
        "selection": selection_key(info),
        "kind": kind,
    }
    _report_waiting(topic, run, row, h=h, error=shortfall.error())


def _keep_waiting(topic: Topic, run: CheckRun, row: dict[str, Any], *, h: str, waiting: dict[str, Any]) -> str:
    """Nothing can be decided now (the client passes through a state of its own, the drive is
    not there for the moment): the topic waits as it did, with the numbers last measured."""

    def number(value: object) -> int:
        return value if isinstance(value, int) and not isinstance(value, bool) else 0

    last = Shortfall(
        needed=number(waiting.get("needed")), free=number(waiting.get("free")), path=str(waiting.get("path") or "")
    )
    _report_waiting(topic, run, row, h=h, error=last.error())
    return WAITING


def _report_waiting(topic: Topic, run: CheckRun, row: dict[str, Any], *, h: str, error: TowError) -> None:
    row.update({"ok": False, "hash": h, "status": "waiting_space", "waiting_space": True})
    set_error(row, error)
    if run.notify:
        run.queue_notification(
            topic,
            kind="error",
            operation_id=new_operation_id("check-error"),
            tracker=str(row.get("tracker") or ""),
            error=error,
        )


def started(topic: Topic, run: CheckRun, row: dict[str, Any], *, h: str, client_id: str, kind: str) -> None:
    """The waiting torrent was started (its start read back): History and the owner hear it."""
    forget(topic)
    run.record(
        "client_started",
        operation_id=new_operation_id("client-start"),
        component="client",
        integration_id=client_id,
        client_id=client_id,
        client_kind=row.get("client_kind"),
        topic_id=topic.get("id"),
        topic=topic.get("id"),
        title=topic.get("title"),
        hash=h,
        reason="space",
        status="succeeded",
        how=run.how,
    )
    if run.notify:
        # The waiting error was reported: queue_recoveries adds "problem fixed" to this message.
        run.queue_notification(
            topic, kind=kind, operation_id=new_operation_id("bot"), tracker=str(row.get("tracker") or "")
        )


def recheck(
    topic: Topic,
    run: CheckRun,
    row: dict[str, Any],
    client: TorrentClientAdapter | None,
    client_id: str,
    *,
    begun: tuple[str, ...] | None = None,
) -> str:
    """Look at a waiting topic's torrent in its client - never at the site - and start it when
    its files fit now. Returns WAITING, STARTED, WOULD_START (a dry run), WITHDRAWN (the owner
    paused, deleted or edited the topic since ``begun``, its ``client_marks`` when the run
    began: nothing is started) or why the waiting ended without TOW: GONE, FOREIGN (TOW's mark
    removed), OWNER (started or reselected by the owner), STALE (the record is of an earlier
    revision or another client). Raises when the client does not answer, a previous revision
    still runs on the same files, or the start is not confirmed (the topic keeps waiting).

    A state the client passes through on its own (loading after its restart, checking, moving
    the files, an error) and a drive that cannot be measured right now decide nothing: the
    topic keeps waiting as it was."""
    waiting = waiting_of(topic)
    h = str(waiting.get("hash") or "").upper()
    recorded_client = str(waiting.get("client") or "")
    if not h or h != str(topic.get("hash") or "").upper() or (recorded_client and recorded_client != client_id):
        forget(topic)  # a record left from an earlier revision, or of a client the topic left
        return STALE
    row.update({"hash": h, "changed": False})
    if client is None:
        raise client_unreachable(run, client_id)
    info = client.inspect_torrent(h)
    if info is None:
        forget(topic)
        return GONE
    if not info_owned_by_tow(info, h):
        forget(topic)
        return FOREIGN
    if not is_stopped(info):
        if not _running(info):
            return _keep_waiting(topic, run, row, h=h, waiting=waiting)
        forget(topic)  # the owner started it: his decision
        return OWNER
    if not info.get("files"):
        return _keep_waiting(topic, run, row, h=h, waiting=waiting)  # its files not listed (yet)
    if selection_key(info) != waiting.get("selection"):
        forget(topic)  # the owner chose its files in the client: his decision
        return OWNER
    dest = str(info.get("save_path") or topic.get("save_path") or "")
    known, shortfall = (True, None) if client_id in run.remote_clients else _measured(dest, _client_wanted(info, dest))
    kind = str(waiting.get("kind") or "added")
    if not known:
        return _keep_waiting(topic, run, row, h=h, waiting=waiting)
    if shortfall is not None:
        wait_for_space(topic, run, row, h=h, client_id=client_id, info=info, shortfall=shortfall, kind=kind)
        return WAITING
    names = [SimpleNamespace(path=str(item.get("name") or "")) for item in _wanted_rows(info)]
    overlap = live_previous_overlap(client, list(map(str, topic.get("previous_hashes") or [])), names)
    if overlap:
        raise TowError(PREVIOUS_REVISION_ACTIVE, file=overlap, hash=h)  # which revision waits: logged once
    if not run.apply:
        row["would_start"] = True
        return WOULD_START
    if withdrawn_meanwhile(topic, row, h, begun):
        return WITHDRAWN  # the next check works with what the owner saved
    client.start_owned_torrent(h)
    after = client.inspect_torrent(h)  # the start counts only once the client confirms it
    if not info_confirms(after, h, dest, require_tow_ownership=True) or (
        is_stopped(after) and not completed_progress((after or {}).get("progress"))
    ):
        raise TowError("check.start_unconfirmed")
    row.update({"ok": True, "status": "succeeded", "started": True})
    _clear_row_error(row)
    started(topic, run, row, h=h, client_id=client_id, kind=kind)
    return STARTED


def _clear_row_error(row: dict[str, Any]) -> None:
    for key in ("error", "error_record", "error_class"):
        row.pop(key, None)


def _show_error(topic: Topic, row: dict[str, Any]) -> None:
    """The row's waiting error (fresh numbers) on the topic, without counting as a check of it."""
    record = as_dict(row.get("error_record"))
    topic["last_error"] = str(row.get("error") or "")
    topic["last_error_code"] = str(record.get("code") or WAITING_SPACE)
    topic["last_error_params"] = as_dict(record.get("params"))
    topic["last_error_class"] = str(row.get("error_class") or "disk")


def _log_failure(topic: Topic, run: CheckRun, error: BaseException, before: tuple[Any, ...]) -> None:
    """History has a failure of the space pass as it has a check's, once: the pass runs every
    few minutes, so the same failure again is not logged again, nor is a client that does not
    answer (the pass logs that once for the client)."""
    if getattr(error, "code", None) == "check.client_unreachable":
        return
    if before == (topic.get("last_error_code"), topic.get("last_error_params"), topic.get("last_error")):
        return
    run.record(
        "check_fail",
        topic=topic.get("id"),
        title=topic.get("title"),
        url=topic.get("url"),
        **error_fields(error),
        how=run.how,
    )


def space_pass_row(topic: Topic, run: CheckRun, row: dict[str, Any]) -> None:
    """The supervisor's space pass (no site traffic) for one waiting topic: ``row`` starts as
    a progress-only row; the topic's result changes only when the waiting changed. A file
    selection the owner changed in TOW meanwhile is left to the next check, which applies it
    (and starts the torrent when the new selection fits)."""
    if topic.get("selection_dirty"):
        return
    begun = client_marks(topic)  # this run's copy, as it was read when the run began
    client_id, client = run.get_client_for(topic)
    row["client_id"] = client_id
    row["client_kind"] = client.client_kind if client is not None else None
    try:
        outcome = recheck(topic, run, row, client, client_id, begun=begun)
    except Exception as error:  # noqa: BLE001 - one topic's failure is its result, never the pass's end
        before = (topic.get("last_error_code"), topic.get("last_error_params"), topic.get("last_error"))
        row.update({"ok": False, "status": "failed"})
        set_error(row, error)
        stamp_result(topic, row)
        _log_failure(topic, run, error, before)
        if run.notify:
            run.queue_notification(
                topic, kind="error", operation_id=new_operation_id("check-error"), error=error, tracker=""
            )
        return
    if outcome == WAITING:
        _show_error(topic, row)
        row.update({"ok": True, "status": "skipped"})  # still waiting: the pass itself went fine
        _clear_row_error(row)
    elif outcome == GONE:
        fail_row(topic, row, TowError("check.removed_from_client"))
    elif outcome == FOREIGN:
        seen = {"hash": str(row.get("hash") or ""), "url": str(topic.get("url") or ""), "client": client_id}
        fail_row(topic, row, TowError("check.not_owned_existing", cls="qbit", **seen))
    elif outcome in {STARTED, OWNER}:
        row.update({"ok": True, "status": "succeeded" if outcome == STARTED else "skipped"})
        stamp_result(topic, row)
    # STALE: a record left from an earlier revision or client, dropped; the topic's result stays.
    # WITHDRAWN: the owner paused, deleted or edited it meanwhile; the row says so, nothing else.
