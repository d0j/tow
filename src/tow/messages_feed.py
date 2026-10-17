"""Recent messages: one bounded list per install, the same on every signed-in device.

The list is a projection of the event log (``data/tow.jsonl`` and its rotated files), never a
store of its own. What every device shares besides it is ``data/ui.json``: how far the list is
read and cleared, as two cursor markers (schema version, segment, offset, event id) - no text,
draft, credential or form base. It is excluded from Undo, restore points, night copies, the
``.towx`` file and update snapshots (``scripts/update.py`` ``SKIP_FILES``); a restore or an
import leaves it as it is. Losing it can bring cleared messages and unread badges back.

The event -> message matrix (``MATRIX``) decides which events are messages, their category and
their typed severity (never read from translated words):

=========== ===========================================================================
Category    Events
=========== ===========================================================================
downloads   confirmed client results: client_added, client_started, client_updated,
            client_selection_updated, new_file, revision_updated, file_completed,
            episode_completed, client_adopted, client_restored, client_stopped, qbit_move,
            qbit_move_undo (ok; an unconfirmed move warn); client_removed (warn);
            client_add_failed, client_adopt_failed, client_stop_failed, qbit_move_fail,
            qbit_move_undo_fail (err)
checks      check_fail, add_check_fail (warn for transport classes, else err; repeated
            failures of one site aggregate into one entry), check_blocked, add_check_blocked,
            reconcile_failed, check_report_failed, client_unreachable, browser_auth_failed
            (err), check_site_skipped (warn); a successful check of a topic whose check
            failed before becomes ``check_recovered`` (ok)
actions     committed owner results: topic_add/edit/delete/pause, site_add/save/delete/pause,
            settings_* results, settings_restore_point_applied, settings_portable_restore,
            backup_restored, undo, sessions_signed_out/reset, setup_password,
            master_key_created, browser_auth (verified only), browser_auth_succeeded (ok);
            their *_fail counterparts (err); a manual undo_expired (warn)
operations  watchdog_alert, backup_cleanup_pending, backup_restore_recovered, secret undo
            cleanup failures (warn); backup_failed, backup_restore_failed,
            backup_restore_recovery_failed (err); a night copy after a failed one becomes
            ``backup_recovered`` (ok)
updates     settings_update_started (info), settings_update_refused (err) and
            settings_update_result, reconciled once per job from the update job record by
            its operation id (ok / warn for rolled back or recovered / err)
delivery    bot_delivery_failed (warn, err when dropped; repeated failures of one messenger
            aggregate); a delivery after a failure becomes ``delivery_recovered`` (ok)
=========== ===========================================================================

Everything else - routine checks and passes, ``tracker_checked``, attempts (``client_add_started``,
``site_login`` and a ``browser_auth`` session that a check may still revert,
``portable_import_applied`` that a restore point may still roll back), diagnostics
(``protection_*``, ``content_cache_failed``), bookkeeping and every successful delivery - is
History only. Scheduled and CLI events use the same projection as the web's.

Order is the physical append order: a cursor names a segment (a SHA-256 of a file's first line,
so a file keeps its identity when rotation renames it), the byte offset after the event's line
and the event id - never a time (``event_id`` is a UUID4 and clocks go back). The feed is up to
``FEED_LIMIT`` entries from ``RETENTION_SEC`` of the retained logs: rotation may remove events
sooner. The log is read by one background reader per log file, without the log lock: only
complete lines are read, and a line is appended in one write.
"""

from __future__ import annotations

import base64
import binascii
import contextvars
import hashlib
import json
import os
import re
import tempfile
import threading
import time
from collections import deque
from collections.abc import Callable, Iterable, Iterator, Mapping
from contextlib import contextmanager, suppress
from dataclasses import dataclass, field, replace
from datetime import datetime
from pathlib import Path
from typing import Any, BinaryIO

from tow import i18n, paths, status, store
from tow import log as eventlog
from tow.i18n import t
from tow.platform import locks
from tow.scrub import scrub_display, without_paths

SCHEMA = 1  # the projection's schema: part of every revision
UI_SCHEMA = 1  # data/ui.json
FEED_LIMIT = 50
RETENTION_SEC = 7 * 86400
SEGMENT_RECORDS = 4000  # the newest message records kept per log file
AGGREGATE_GAP_SEC = 900  # repeated failures of one site closer than this are one entry
_PARSE_BLOCK = 1024 * 1024
_FIRST_LINE_MAX = 256 * 1024
_UI_MAX_BYTES = 4096

DOWNLOADS, CHECKS, ACTIONS, OPERATIONS, UPDATES, DELIVERY = (
    "downloads",
    "checks",
    "actions",
    "operations",
    "updates",
    "delivery",
)
SEVERITIES = ("ok", "info", "warn", "err")
Severity = Callable[[Mapping[str, Any]], str | None]


def _fixed(severity: str) -> Severity:
    return lambda _rec: severity


def _by_class(rec: Mapping[str, Any]) -> str:
    """Transport trouble that passes by itself is a warning, every other error an actionable
    failure (the status-colour contract): by the stored class, never by the words."""
    return "warn" if status.error_tone(str(rec.get("cls") or "")) == "warn" else "err"


def _move(rec: Mapping[str, Any]) -> str:
    return "ok" if rec.get("status") in ("succeeded", "moving") else "warn"


def _manual(severity: str) -> Severity:
    return lambda rec: severity if rec.get("how") == "manual" else None


def _verified(rec: Mapping[str, Any]) -> str | None:
    return "ok" if rec.get("status") == "verified" else None


def _delivery_failed(rec: Mapping[str, Any]) -> str:
    return "err" if rec.get("status") == "dropped" else "warn"


_UPDATE_RESULTS = {"ok": "ok", "rolled_back": "warn", "recovered": "warn", "failed": "err", "refused": "err"}


def _update_result(rec: Mapping[str, Any]) -> str | None:
    return _UPDATE_RESULTS.get(str(rec.get("status") or ""))


OK, WARN, ERR, INFO = _fixed("ok"), _fixed("warn"), _fixed("err"), _fixed("info")

# kind -> (category, severity of the record or None: not a message). The table in the module
# docstring says the same; docs/guide.md says it for the owner.
MATRIX: dict[str, tuple[str, Severity]] = {
    **dict.fromkeys(
        (
            "client_added",
            "client_started",
            "client_updated",
            "client_selection_updated",
            "new_file",
            "revision_updated",
            "file_completed",
            "episode_completed",
            "client_adopted",
            "client_restored",
            "client_stopped",
        ),
        (DOWNLOADS, OK),
    ),
    "qbit_move": (DOWNLOADS, _move),
    "qbit_move_undo": (DOWNLOADS, _move),
    "client_removed": (DOWNLOADS, WARN),
    **dict.fromkeys(
        ("client_add_failed", "client_adopt_failed", "client_stop_failed", "qbit_move_fail", "qbit_move_undo_fail"),
        (DOWNLOADS, ERR),
    ),
    "check_fail": (CHECKS, _by_class),
    "add_check_fail": (CHECKS, _by_class),
    **dict.fromkeys(
        (
            "check_blocked",
            "add_check_blocked",
            "reconcile_failed",
            "check_report_failed",
            "client_unreachable",
            "browser_auth_failed",
            "browser_auth_restore_failed",
            "site_login_restore_failed",
        ),
        (CHECKS, ERR),
    ),
    "check_site_skipped": (CHECKS, WARN),
    "check_recovered": (CHECKS, OK),
    **dict.fromkeys(
        (
            "topic_add",
            "topic_edit",
            "topic_delete",
            "topic_pause",
            "site_add",
            "site_save",
            "site_delete",
            "site_pause",
            "settings_client",
            "settings_client_add",
            "settings_client_remove",
            "settings_client_default",
            "settings_notifier",
            "settings_notifier_removed",
            "settings_interval",
            "settings_language",
            "settings_theme",
            "settings_access",
            "settings_password",
            "settings_backup_location",
            "settings_backup_retention",
            "settings_backup_automatic",
            "settings_backup_deleted",
            "settings_backup_checked",
            "settings_restore_point_created",
            "settings_restore_point_applied",
            "settings_portable_export",
            "settings_portable_restore",
            "settings_service_autostart",
            "settings_service_restart",
            "settings_service_migrate",
            "settings_access_undo",
            "settings_clients_undo",
            "backup_restored",
            "undo",
            "sessions_signed_out",
            "sessions_reset",
            "setup_password",
            "master_key_created",
            "browser_auth_succeeded",
        ),
        (ACTIONS, OK),
    ),
    "browser_auth": (ACTIONS, _verified),
    "undo_expired": (ACTIONS, _manual("warn")),
    **dict.fromkeys(
        (
            "site_save_fail",
            "settings_save_fail",
            "settings_rollback_fail",
            "settings_interval_fail",
            "settings_backup_check_fail",
            "settings_backup_delete_fail",
            "settings_restore_point_create_fail",
            "settings_restore_point_apply_fail",
            "settings_portable_export_fail",
            "settings_portable_import_fail",
            "settings_service_autostart_fail",
            "settings_service_restart_fail",
            "settings_service_migrate_fail",
            "settings_undo_fail",
            "settings_access_undo_restore_fail",
            "settings_clients_undo_restore_fail",
            "undo_fail",
        ),
        (ACTIONS, ERR),
    ),
    **dict.fromkeys(
        (
            "watchdog_alert",
            "backup_cleanup_pending",
            "backup_restore_recovered",
            "secret_undo_cleanup_fail",
            "secret_undo_cleanup_retry_fail",
            "settings_undo_cleanup_fail",
            "settings_undo_cleanup_marker_fail",
            "settings_access_undo_cleanup_fail",
            "settings_clients_undo_cleanup_fail",
            "site_secret_undo_cleanup_fail",
            "site_secret_undo_cleanup_marker_fail",
        ),
        (OPERATIONS, WARN),
    ),
    **dict.fromkeys(("backup_failed", "backup_restore_failed", "backup_restore_recovery_failed"), (OPERATIONS, ERR)),
    "backup_recovered": (OPERATIONS, OK),
    "settings_update_started": (UPDATES, INFO),
    "settings_update_refused": (UPDATES, ERR),
    "settings_update_result": (UPDATES, _update_result),
    "bot_delivery_failed": (DELIVERY, _delivery_failed),
    "delivery_recovered": (DELIVERY, OK),
}

# A success after a failure of the same subject is a message (a recovery); every other success of
# these kinds is routine. (failure kinds, success kind, the field naming the subject, message kind)
RECOVERIES: tuple[tuple[frozenset[str], str, str, str], ...] = (
    (frozenset({"check_fail", "add_check_fail"}), "tracker_checked", "topic", "check_recovered"),
    (frozenset({"backup_failed"}), "backup_created", "", "backup_recovered"),
    (frozenset({"bot_delivery_failed"}), "bot_delivery_succeeded", "integration_id", "delivery_recovered"),
)
# Repeated failures that are one problem: kind -> the field naming what they share (one site,
# one messenger). Members also share their class and who started them.
AGGREGATED = {"check_fail": "tracker", "bot_delivery_failed": "integration_id"}

_SYNTHETIC = frozenset(kind for *_, kind in RECOVERIES)
RELEVANT = (frozenset(MATRIX) - _SYNTHETIC) | {success for _, success, _, _ in RECOVERIES}
_KIND_RE = re.compile(rb'"kind":\s*"([A-Za-z0-9_]+)"')
# The fields a message may use; the rest of a record (folders, links, hashes, raw diagnostics)
# never leaves the reader.
_KEEP = (
    "event_id",
    "ts",
    "created_at",
    "kind",
    "operation_id",
    "topic",
    "topic_id",
    "title",
    "tracker",
    "cls",
    "error",
    "error_code",
    "error_params",
    "status",
    "how",
    "client_id",
    "integration_id",
    "target_version",
    "previous_version",
    "reason",
    "alerts",
    "paused",
    "topics",
    "copy_kind",
    "undo",
)
_HEX32 = re.compile(r"[0-9a-f]{32}")
_TOPIC_ID = re.compile(r"[A-Za-z0-9_-]{1,64}")


# --------------------------------------------------------------------------- cursors


@dataclass(frozen=True)
class Pos:
    """A place in the retained append sequence: a log file (by its first line) and the offset
    after an event's line, with that event's id."""

    segment: str
    offset: int
    event: str

    def cursor(self) -> str:
        raw = f"{self.segment}.{self.offset}.{self.event}".encode("ascii")
        return base64.urlsafe_b64encode(raw).decode("ascii").rstrip("=")

    def marker(self) -> dict[str, Any]:
        return {"segment": self.segment, "offset": self.offset, "event": self.event}


def decode_cursor(value: str) -> Pos | None:
    """A cursor this server gave out, else None (bounded, strict)."""
    if not isinstance(value, str) or not 1 <= len(value) <= 128 or not re.fullmatch(r"[A-Za-z0-9_-]+", value):
        return None
    try:
        raw = base64.urlsafe_b64decode(value + "=" * (-len(value) % 4)).decode("ascii")
    except binascii.Error, UnicodeDecodeError, ValueError:
        return None
    match = re.fullmatch(r"([0-9a-f]{32})\.([1-9][0-9]{0,12})\.([0-9a-f]{32})", raw)
    if match is None:
        return None
    return Pos(match.group(1), int(match.group(2)), match.group(3))


def _marker_pos(value: Any) -> Pos | None:
    if not isinstance(value, dict) or set(value) != {"segment", "offset", "event"}:
        return None
    segment, offset, event = value["segment"], value["offset"], value["event"]
    if not (
        isinstance(segment, str) and _HEX32.fullmatch(segment) and isinstance(event, str) and _HEX32.fullmatch(event)
    ):
        return None
    if type(offset) is not int or not 0 < offset < 10**13:
        return None
    return Pos(segment, offset, event)


# --------------------------------------------------------------------------- data/ui.json


@dataclass(frozen=True)
class Markers:
    read: Pos | None = None
    cleared: Pos | None = None
    status: str = "missing"  # ok, missing, damaged


def _parse_markers(raw: bytes) -> Markers:
    try:
        value = json.loads(raw.decode("utf-8"))
    except UnicodeDecodeError, ValueError, RecursionError:
        return Markers(status="damaged")
    if not isinstance(value, dict) or set(value) != {"schema", "read_upto", "cleared_upto"}:
        return Markers(status="damaged")
    if type(value["schema"]) is not int or value["schema"] != UI_SCHEMA:
        return Markers(status="damaged")
    read, cleared = value["read_upto"], value["cleared_upto"]
    read_pos, cleared_pos = _marker_pos(read), _marker_pos(cleared)
    if (read is not None and read_pos is None) or (cleared is not None and cleared_pos is None):
        return Markers(status="damaged")
    return Markers(read_pos, cleared_pos, "ok")


def load_markers() -> Markers:
    """The shared markers as stored; a missing or damaged file is no markers (nothing read or
    cleared). Reading never writes, repairs or creates anything."""
    path = paths.ui_state_path()
    try:
        with path.open("rb") as handle:
            raw = handle.read(_UI_MAX_BYTES + 1)
    except FileNotFoundError:
        return Markers()
    except OSError:
        return Markers(status="damaged")
    if len(raw) > _UI_MAX_BYTES:
        return Markers(status="damaged")
    return _parse_markers(raw)


def ui_state_bytes(markers: Markers) -> bytes:
    """The exact bytes of ``data/ui.json`` for ``markers``: schema and cursor metadata only."""
    value = {
        "schema": UI_SCHEMA,
        "read_upto": markers.read.marker() if markers.read else None,
        "cleared_upto": markers.cleared.marker() if markers.cleared else None,
    }
    return (json.dumps(value, sort_keys=True) + "\n").encode("ascii")


_UI_THREAD_LOCK = threading.RLock()


@contextmanager
def ui_state_lock() -> Iterator[None]:
    """data/ui.json's own lock, across threads and processes: never the data lock, so marking
    messages read never waits for a check's commit."""
    lock_path = paths.data_dir() / ".ui.lock"
    with _UI_THREAD_LOCK, lock_path.open("a+b") as handle:
        store.init_lock_file(handle)
        locks.lock(handle, poll=0.01)
        try:
            yield
        finally:
            locks.unlock(handle)


def _write_ui_state(content: bytes) -> None:
    """Temporary file, fsync, rename: a reader sees the old markers or the new ones, whole."""
    path = paths.ui_state_path(create=True)
    temporary: Path | None = None
    try:
        with tempfile.NamedTemporaryFile(
            mode="wb", prefix=f".{path.name}.", suffix=".tmp", dir=path.parent, delete=False
        ) as handle:
            temporary = Path(handle.name)
            handle.write(content)
            handle.flush()
            os.fsync(handle.fileno())
        store.replace_with_retry(temporary, path)
        temporary = None
    finally:
        if temporary is not None:
            with suppress(OSError):
                temporary.unlink()


# --------------------------------------------------------------------------- the log reader


@dataclass
class _Segment:
    """One log file, known by the hash of its first line wherever rotation has moved it."""

    fp: str
    consumed: int = 0
    records: deque[dict[str, Any]] = field(default_factory=lambda: deque(maxlen=SEGMENT_RECORDS))
    failing_after: frozenset[tuple[int, str]] = frozenset()
    dropped: bool = False  # older records went past SEGMENT_RECORDS


@dataclass(frozen=True)
class Window:
    """The retained log as the reader saw it: segments oldest first, and the message records in
    append order. ``full``: rotation already drops files, so a segment that is gone is older
    than every retained one (not a reset)."""

    chain: tuple[str, ...] = ()
    records: tuple[dict[str, Any], ...] = ()
    full: bool = False
    stamp: tuple[Any, ...] = ()
    # ``stable``: no rotation moved the files while they were read (``LogReader.scan``);
    # ``readable``: every log file there was could be read.
    stable: bool = True
    readable: bool = True

    def rank(self, pos: Pos | None) -> tuple[int, int] | None:
        """The place of ``pos`` in append order; (-1, 0) when its file rotated away; None when
        the log it belonged to is gone (reset) or for no position."""
        if pos is None:
            return None
        try:
            return (self.chain.index(pos.segment), pos.offset)
        except ValueError:
            return (-1, 0) if self.full else None


def _first_line(handle: BinaryIO) -> bytes | None:
    """The open file's first complete line (None while there is none), read in small blocks."""
    head = b""
    handle.seek(0)
    while len(head) < _FIRST_LINE_MAX:
        block = handle.read(4096)
        if not block:
            return None
        head += block
        end = head.find(b"\n")
        if end >= 0:
            return head[: end + 1]
    return None


def _fingerprint(first: bytes | None) -> str | None:
    """A file's identity: the hash of its first line (file ids can be reused or missing, a first
    line with its event id is not)."""
    return None if first is None else hashlib.sha256(first).hexdigest()[:32]


_MISSING, _UNREADABLE = "missing", "unreadable"
_SCAN_ATTEMPTS = 3


def _light(rec: Mapping[str, Any]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for key in _KEEP:
        value = rec.get(key)
        if value is None:
            continue
        if isinstance(value, str):
            out[key] = value[:1000]
        elif isinstance(value, (bool, int, float)):
            out[key] = value
        elif key == "alerts" and isinstance(value, list):
            out[key] = [str(item)[:400] for item in value[:10]]
        elif key == "error_params" and isinstance(value, dict) and len(json.dumps(value, default=str)) <= 4000:
            out[key] = value
    return out


def _timestamp(rec: Mapping[str, Any]) -> float | None:
    value = str(rec.get("created_at") or rec.get("ts") or "")
    try:
        moment = datetime.fromisoformat(value)
    except ValueError:
        return None
    if moment.tzinfo is None:
        return None
    return moment.timestamp()


class LogReader:
    """Reads the retained log incrementally: rotated files once (their hash names them), the live
    file from where it stopped. A file that shrank or changed its first line is read anew.

    A file's size, first line and records come from one open file: a rotation meanwhile (which
    renames files, on Windows only those nobody holds open) never puts one file's records under
    another's identity. Which file is where is looked at again after the pass; a pass during
    which a rotation moved the files is read again (``_SCAN_ATTEMPTS``), and one that never held
    still says so (``Window.stable``)."""

    def __init__(self, path: Path) -> None:
        self.path = path
        self._segments: dict[str, _Segment] = {}

    def _files(self) -> list[Path]:
        rotated = [self.path.with_name(f"{self.path.name}.{index}") for index in range(eventlog.BACKUPS, 0, -1)]
        return [*rotated, self.path]

    def _inventory(self) -> tuple[str | None, ...]:
        """Which file is at each place now: its identity (None: no complete line yet), or
        missing or unreadable."""
        found: list[str | None] = []
        for path in self._files():
            try:
                with path.open("rb") as handle:
                    found.append(_fingerprint(_first_line(handle)))
            except FileNotFoundError:
                found.append(_MISSING)
            except OSError:
                found.append(_UNREADABLE)
        return tuple(found)

    @staticmethod
    def _held_still(seen: tuple[str | None, ...], now: tuple[str | None, ...]) -> bool:
        """No rotation between the pass (``seen``) and ``now``: every rotated file is the same
        one, and so is the live file - or it had no line yet (an append is no rotation)."""
        if len(seen) != len(now) or seen[:-1] != now[:-1]:
            return False
        return seen[-1] == now[-1] or seen[-1] in (None, _MISSING)

    def scan(self, now: float) -> Window:
        window = Window()
        for _attempt in range(_SCAN_ATTEMPTS):
            window, seen = self._pass(now)
            if self._held_still(seen, self._inventory()):
                return window
            # The files moved while they were read: what was read follows that pass's order
            # (a failure's recovery among them), so it is all read again.
            self._segments.clear()
        return replace(window, stable=False)

    def _pass(self, now: float) -> tuple[Window, tuple[str | None, ...]]:
        chain: list[str] = []
        stamp: list[Any] = []
        seen: list[str | None] = []
        state: frozenset[tuple[int, str]] = frozenset()
        present = 0
        readable = True
        files = self._files()
        for index, path in enumerate(files):
            live = index == len(files) - 1
            try:
                handle = path.open("rb")
            except FileNotFoundError:
                seen.append(_MISSING)
                continue
            except OSError:
                stamp.append((path.name, _UNREADABLE))
                seen.append(_UNREADABLE)
                readable = False
                continue
            with handle:
                try:
                    info = os.fstat(handle.fileno())
                    fp = _fingerprint(_first_line(handle))
                except OSError:
                    stamp.append((path.name, _UNREADABLE))
                    seen.append(_UNREADABLE)
                    readable = False
                    continue
                present += 1
                stamp.append((path.name, info.st_size, info.st_mtime_ns, info.st_ino))
                seen.append(fp)
                if fp is None or fp in chain:
                    continue
                segment = self._segments.get(fp)
                if segment is None or segment.consumed > info.st_size:
                    segment = _Segment(fp)
                    self._segments[fp] = segment
                    old = not live and info.st_mtime < now - RETENTION_SEC - 86400
                    if old:  # nothing in it is recent enough: its identity alone counts
                        segment.consumed = info.st_size
                        segment.failing_after = state
                if segment.consumed < info.st_size:
                    try:
                        self._parse(
                            handle, segment, info.st_size, state if segment.consumed == 0 else segment.failing_after
                        )
                    except OSError:
                        readable = False
                state = segment.failing_after
                chain.append(fp)
        for fp in [fp for fp in self._segments if fp not in chain]:
            del self._segments[fp]
        records = tuple(rec for fp in chain for rec in self._segments[fp].records)
        window = Window(tuple(chain), records, present == len(files), tuple(stamp), readable=readable)
        return window, tuple(seen)

    def _parse(self, handle: BinaryIO, segment: _Segment, size: int, failing: frozenset[tuple[int, str]]) -> None:
        """The records of the open file ``handle`` from where ``segment`` stopped up to ``size``."""
        state = set(failing)
        handle.seek(segment.consumed)
        position = segment.consumed
        rest = b""
        while position < size:
            block = handle.read(min(_PARSE_BLOCK, size - position))
            if not block:
                break
            position += len(block)
            data = rest + block
            end = data.rfind(b"\n")
            if end < 0:
                rest = data
                continue
            complete, rest = data[: end + 1], data[end + 1 :]
            start = position - len(rest) - len(complete)
            for line_end, line in _lines(complete, start):
                self._take(segment, line, line_end, state)
            segment.consumed = position - len(rest)
        segment.failing_after = frozenset(state)

    def _take(self, segment: _Segment, line: bytes, line_end: int, failing: set[tuple[int, str]]) -> None:
        kind_match = _KIND_RE.search(line)
        if kind_match is None or kind_match.group(1).decode("ascii") not in RELEVANT:
            return
        try:
            rec = json.loads(line.decode("utf-8", "replace"))
        except ValueError:
            return
        if not isinstance(rec, dict) or not isinstance(rec.get("event_id"), str):
            return
        event = rec["event_id"]
        if not _HEX32.fullmatch(event):
            return
        kind = str(rec.get("kind") or "")
        shown = kind
        for index, (failures, success, key_field, recovered) in enumerate(RECOVERIES):
            key = (index, str(rec.get(key_field) or "") if key_field else "")
            if kind in failures:
                failing.add(key)
            elif kind == success:
                if key not in failing:
                    return  # routine success
                failing.discard(key)
                shown = recovered
        if shown not in MATRIX:
            return
        light = _light(rec)
        light.update(_kind=shown, _pos=Pos(segment.fp, line_end, event))
        if len(segment.records) == segment.records.maxlen:
            segment.dropped = True
        segment.records.append(light)


def _lines(data: bytes, start: int) -> Iterator[tuple[int, bytes]]:
    offset = start
    for line in data.split(b"\n")[:-1]:
        offset += len(line) + 1
        if line.strip():
            yield offset, line


# --------------------------------------------------------------------------- the projection


@dataclass(frozen=True)
class Projection:
    entries: tuple[dict[str, Any], ...] = ()
    unread: int = 0
    reset: bool = False
    expires_at: float | None = None
    markers: str = "missing"


def _severity(rec: Mapping[str, Any]) -> str | None:
    category_severity = MATRIX.get(str(rec.get("_kind") or ""))
    if category_severity is None:
        return None
    severity = category_severity[1](rec)
    return severity if severity in SEVERITIES else None


def _group_key(rec: Mapping[str, Any], severity: str) -> tuple[Any, ...] | None:
    kind = str(rec.get("_kind") or "")
    field_name = AGGREGATED.get(kind)
    if field_name is None or not rec.get(field_name):
        return None
    return (kind, str(rec[field_name]), str(rec.get("cls") or ""), str(rec.get("how") or ""), severity)


def project(window: Window, markers: Markers, now: float) -> Projection:
    """The feed: up to FEED_LIMIT entries, newest first, of the last RETENTION_SEC, after the
    cleared marker; each read when its newest event is at or before the read marker."""
    read_rank, cleared_rank = window.rank(markers.read), window.rank(markers.cleared)
    reset = (markers.read is not None and read_rank is None) or (markers.cleared is not None and cleared_rank is None)
    entries: list[dict[str, Any]] = []
    open_groups: dict[tuple[Any, ...], dict[str, Any]] = {}
    for rec in window.records:
        pos: Pos = rec["_pos"]
        rank = window.rank(pos)
        if rank is None or (cleared_rank is not None and rank <= cleared_rank):
            continue
        at = _timestamp(rec)
        if at is None or now - at > RETENTION_SEC:
            continue
        severity = _severity(rec)
        if severity is None:
            continue
        key = _group_key(rec, severity)
        group = open_groups.get(key) if key else None
        if group is not None and 0 <= at - group["at"] <= AGGREGATE_GAP_SEC:
            group.update(at=at, pos=pos, rank=rank, rec=rec, count=group["count"] + 1)
            group["subjects"].add(str(rec.get("topic") or ""))
            continue
        entry = {
            "id": rec["event_id"],
            "pos": pos,
            "rank": rank,
            "at": at,
            "severity": severity,
            "category": MATRIX[rec["_kind"]][0],
            "kind": rec["_kind"],
            "rec": rec,
            "count": 1,
            "subjects": {str(rec.get("topic") or "")},
        }
        entries.append(entry)
        if key:
            open_groups[key] = entry
    entries.sort(key=lambda entry: entry["rank"], reverse=True)
    shown = entries[:FEED_LIMIT]
    for entry in shown:
        entry["read"] = read_rank is not None and entry["rank"] <= read_rank
        entry["topics"] = len(entry.pop("subjects") - {""})
    expires = min((entry["at"] for entry in shown), default=None)
    return Projection(
        tuple(shown),
        sum(1 for entry in shown if not entry["read"]),
        reset,
        None if expires is None else expires + RETENTION_SEC,
        markers.status,
    )


# --------------------------------------------------------------------------- words


# A message says what happened on its own, as a sentence: History's label of these kinds is a
# bare word ("interval", "undone", "added") that its line explains, so the feed has its own.
_FEED_LABELS = {
    key.rsplit(".", 1)[1]: key
    for key in (
        "log.feed.kind.topic_add",
        "log.feed.kind.topic_delete",
        "log.feed.kind.topic_edit",
        "log.feed.kind.topic_pause",
        "log.feed.kind.topic_resume",
        "log.feed.kind.client_updated",
        "log.feed.kind.qbit_move",
        "log.feed.kind.browser_auth",
        "log.feed.kind.settings_interval",
        "log.feed.kind.settings_language",
        "log.feed.kind.settings_theme",
        "log.feed.kind.settings_password",
        "log.feed.kind.settings_client",
        "log.feed.kind.settings_client_default",
        "log.feed.kind.settings_access",
        "log.feed.kind.settings_service_autostart",
        "log.feed.kind.settings_backup_location",
        "log.feed.kind.undo",
    )
}
# An undo names what it undid when its event says (the undo record's kind).
_UNDO_LABELS = {
    key.rsplit(".", 1)[1]: key
    for key in (
        "log.feed.undo.topic",
        "log.feed.undo.topic_put",
        "log.feed.undo.topic_add",
        "log.feed.undo.site",
        "log.feed.undo.settings",
        "log.feed.undo.settings_access",
        "log.feed.undo.settings_clients",
    )
}


def _feed_label(rec: Mapping[str, Any]) -> str | None:
    """The feed's own words for what happened, or None: History's label says it."""
    kind = str(rec.get("_kind") or "")
    if kind == "topic_pause" and rec.get("paused") is False:
        kind = "topic_resume"  # the pause button turned pause off
    key = _UNDO_LABELS.get(str(rec.get("undo") or "")) if kind == "undo" else None
    key = key or _FEED_LABELS.get(kind)
    return t(key) if key else None


_FORMAT_FIELDS = (
    "kind",
    "ts",
    "created_at",
    "cls",
    "error",
    "error_code",
    "error_params",
    "status",
    "paused",
    "alerts",
)


def _href(entry: Mapping[str, Any]) -> str:
    rec, category = entry["rec"], entry["category"]
    topic = str(rec.get("topic") or rec.get("topic_id") or "")
    if entry["count"] == 1 and topic and _TOPIC_ID.fullmatch(topic) and category in (DOWNLOADS, CHECKS, ACTIONS):
        return f"/#row-{topic}"
    if category == UPDATES:
        return "/settings?open=updates#acc-updates"
    if category == DELIVERY:
        return "/settings?open=bots"
    if category == CHECKS:
        return "/history?group=errors"
    if str(rec.get("_kind") or "").startswith("site_") or (rec.get("tracker") and category == ACTIONS):
        return "/sites"
    return "/history"


def _subject(entry: Mapping[str, Any], title_index: Mapping[str, str], site_titles: Mapping[str, str]) -> str:
    from tow.notify import short_series_title

    rec = entry["rec"]
    if entry["count"] == 1 or entry["topics"] <= 1:
        title = eventlog.event_title(rec, title_index)
        if title:
            return short_series_title(title, i18n.current())
    site = str(rec.get("tracker") or "")
    return str(site_titles.get(site) or site)


def render_entry(
    entry: Mapping[str, Any], *, title_index: Mapping[str, str], site_titles: Mapping[str, str]
) -> dict[str, Any]:
    """An entry as every device gets it, in the current language: allowlisted fields, their
    message values without paths (``tow.scrub.without_paths``), formatted by the History's rules, then
    scrubbed (no paths, links reduced to their host, no raw diagnostics), with a link to a page
    of this TOW only."""
    rec = entry["rec"]
    subject = scrub_display(_subject(entry, title_index, site_titles), 200)
    fields = {key: rec[key] for key in _FORMAT_FIELDS if key in rec}
    if "error_params" in fields:
        fields["error_params"] = without_paths(fields["error_params"])
    fields["kind"] = rec["_kind"]
    site = str(rec.get("tracker") or "")
    if site and subject != str(site_titles.get(site) or site):
        fields["tracker"] = site  # a topic's message names its site too; a site's says it once
    row = eventlog.format_event(fields, site_titles=site_titles)
    bits = [_feed_label(rec) or row["label"]]
    if entry["kind"] == "settings_update_result":
        outcome = str(rec.get("status") or "")
        if outcome in _UPDATE_RESULTS:
            bits.append(t(f"log.update.{outcome}", version=str(rec.get("target_version") or "")))
    if entry["count"] > 1:
        bits.append(
            t("log.feed.topics", n=entry["topics"]) if entry["topics"] > 1 else t("log.feed.times", n=entry["count"])
        )
    if row["detail"]:
        bits.append(row["detail"])
    return {
        "id": entry["id"],
        "cursor": entry["pos"].cursor(),
        "kind": entry["severity"],
        "category": entry["category"],
        "subject": subject,
        "text": scrub_display(" · ".join(bits)),
        "at": int(entry["at"]),
        "href": _href(entry),
        "read": bool(entry["read"]),
    }


def render(
    projection: Projection,
    *,
    lang: str,
    title_index: Mapping[str, str] | None = None,
    site_titles: Mapping[str, str] | None = None,
) -> dict[str, Any]:
    """The feed payload in ``lang`` with its revision: a SHA-256 (128 bits shown) of everything
    the payload says, so equal revisions mean equal feeds for that language."""

    def build() -> dict[str, Any]:
        i18n.use(lang)
        items = [
            render_entry(entry, title_index=title_index or {}, site_titles=site_titles or {})
            for entry in projection.entries
        ]
        return {
            "schema": SCHEMA,
            "items": items,
            "unread": projection.unread,
            "worst": _worst(items),
            "cursor": items[0]["cursor"] if items else "",
            "reset": projection.reset,
            "markers": projection.markers,
            "expires_at": None if projection.expires_at is None else int(projection.expires_at),
            "limit": FEED_LIMIT,
            "retention_days": RETENTION_SEC // 86400,
        }

    payload = contextvars.copy_context().run(build)
    canonical = json.dumps([lang, payload], sort_keys=True, ensure_ascii=False, separators=(",", ":"))
    payload["revision"] = hashlib.sha256(canonical.encode("utf-8")).hexdigest()[:32]
    return payload


_RANK = {"info": 0, "ok": 1, "warn": 2, "err": 3}


def _worst(items: Iterable[Mapping[str, Any]]) -> str:
    unread = [str(item["kind"]) for item in items if not item["read"]]
    return max(unread, key=lambda kind: _RANK[kind], default="")


# --------------------------------------------------------------------------- the shared feed


class MarkerRefused(ValueError):
    """The cursor is not one of the displayed entries any more (another device cleared them, the
    log rotated): the page fetches the feed again."""


@dataclass
class _Snapshot:
    window: Window
    markers: Markers
    projection: Projection
    built: float
    ui_stamp: tuple[Any, ...]
    quick: tuple[Any, ...] = ()


def _file_stamp(path: Path) -> tuple[Any, ...]:
    try:
        info = path.stat()
    except FileNotFoundError:
        return ("missing",)
    except OSError:
        return ("unreadable",)
    return (info.st_size, info.st_mtime_ns, info.st_ino)


def _ui_stamp() -> tuple[Any, ...]:
    try:
        info = paths.ui_state_path().stat()
    except FileNotFoundError:
        return ("missing",)
    except OSError:
        return ("unreadable",)
    return (info.st_size, info.st_mtime_ns, info.st_ino)


class Feed:
    """The feed of one log file: a background reader (one at a time) keeps a snapshot; callers get
    the newest snapshot without ever taking the log lock or the data lock."""

    MIN_AGE = 0.5  # a snapshot younger than this is fresh enough

    def __init__(self, path: Path) -> None:
        self.reader = LogReader(path)
        self._lock = threading.Lock()
        self._scan_lock = threading.Lock()
        self._snapshot: _Snapshot | None = None
        self._thread: threading.Thread | None = None
        self._generation = 0

    def _quick(self) -> tuple[Any, ...]:
        """Two stats - the live log and data/ui.json - that say whether the snapshot may be old."""
        return (_file_stamp(self.reader.path), _ui_stamp())

    def _build(self) -> bool:
        """Read the log and the markers into a new snapshot; False when the log's files kept
        moving while they were read (the last good snapshot stays, the next call reads again)."""
        with self._scan_lock:
            quick = self._quick()
            now = time.time()
            window = self.reader.scan(now)
            stamp = _ui_stamp()
            markers = load_markers()
            previous = self._snapshot
            if not window.stable and previous is not None:
                return False
            if (
                previous is not None
                and previous.window.stamp == window.stamp
                and previous.ui_stamp == stamp
                and previous.window.records == window.records
                and (previous.projection.expires_at is None or now < previous.projection.expires_at)
            ):
                previous.built = time.monotonic()
                previous.quick = quick
                return True
            projection = project(window, markers, now)
            self._snapshot = _Snapshot(window, markers, projection, time.monotonic(), stamp, quick)
            self._generation += 1
            return window.stable

    def _run(self) -> None:
        try:
            self._build()
        finally:
            with self._lock:
                self._thread = None

    def snapshot(self, *, wait: float = 2.0) -> _Snapshot:
        """The newest snapshot; a stale one starts the reader (at most one) and waits for it up to
        ``wait`` seconds, then answers with what there is."""
        quick = self._quick()
        with self._lock:
            current = self._snapshot
            stale = current is None or current.quick != quick or time.monotonic() - current.built >= self.MIN_AGE
            if stale and self._thread is None:
                self._thread = threading.Thread(target=self._run, name="tow-messages-reader", daemon=True)
                self._thread.start()
            thread = self._thread
        if stale and thread is not None and wait > 0:
            thread.join(wait)
        return self._snapshot or _Snapshot(Window(), Markers(), Projection(), 0.0, ())

    def refresh(self) -> _Snapshot:
        """Read now (the caller waits): after this process changed the log or the markers."""
        self._build()
        assert self._snapshot is not None
        return self._snapshot

    def mark(self, cursor: str, *, clear: bool) -> _Snapshot:
        """Read (or clear) everything up to the displayed entry ``cursor`` names, for every device:
        an atomic read-modify-write of data/ui.json under its own lock. Markers only move
        forward in append order; an event appended after the displayed one stays unread and
        uncleared.

        The log is read again under the lock, after every marker saved so far: a marker another
        device saved meanwhile - perhaps in a file a rotation brought in since this page's list
        - is ranked in that newer window, never taken for an older one. A saved marker the
        window cannot place while a log file could not be read may be newer: the request is
        refused, never a guess.

        MarkerRefused for a cursor that is not displayed now, while the log's files keep moving
        or for a saved marker that cannot be proven older; OSError when the markers cannot be
        saved."""
        pos = decode_cursor(cursor)
        if pos is None:
            raise MarkerRefused(cursor)
        with ui_state_lock():
            if not self._build():
                raise MarkerRefused(cursor)
            snapshot = self._snapshot
            assert snapshot is not None
            window = snapshot.window
            target = window.rank(pos)
            if target is None or pos not in {entry["pos"] for entry in snapshot.projection.entries}:
                raise MarkerRefused(cursor)
            current = load_markers()

            def forward(old: Pos | None) -> Pos:
                if old is None:
                    return pos
                rank = window.rank(old)
                if rank is None and not window.readable:
                    raise MarkerRefused(cursor)  # perhaps in the file that could not be read
                # None: its log is gone (replaced) - the window read now is newer than it.
                return old if rank is not None and rank >= target else pos

            read = forward(current.read)
            cleared = forward(current.cleared) if clear else current.cleared
            if current.status == "ok" and read == current.read and cleared == current.cleared:
                return self.refresh()
            _write_ui_state(ui_state_bytes(Markers(read, cleared, "ok")))
        return self.refresh()

    def operation_results(self, kind: str) -> set[str]:
        """The operation ids of the retained ``kind`` events (an update's result is logged once)."""
        snapshot = self.refresh()
        return {str(rec.get("operation_id")) for rec in snapshot.window.records if rec.get("kind") == kind}


_FEEDS: dict[Path, Feed] = {}
_FEEDS_LOCK = threading.Lock()


def feed() -> Feed:
    """The feed of this install's event log (one per log file in this process)."""
    path = eventlog.log_path(create=False)
    with _FEEDS_LOCK:
        found = _FEEDS.get(path)
        if found is None:
            if len(_FEEDS) >= 8:
                _FEEDS.pop(next(iter(_FEEDS)))
            found = _FEEDS[path] = Feed(path)
        return found


# --------------------------------------------------------------------------- update results

_TERMINAL_UPDATES = frozenset(_UPDATE_RESULTS)
_RECONCILE_LOCK = threading.Lock()


def reconcile_update(job: Mapping[str, Any] | None, *, now: float | None = None) -> bool:
    """The result of a finished web update, logged once per job (its id is the operation id),
    from the update job record - the authority the update worker writes outside the event log.
    True when it was logged now; a job already logged, still running or older than the feed's
    retention logs nothing. A failed append is tried again at the next call."""
    if not isinstance(job, Mapping):
        return False
    job_id, outcome = job.get("id"), job.get("status")
    if not isinstance(job_id, str) or not _HEX32.fullmatch(job_id) or outcome not in _TERMINAL_UPDATES:
        return False
    finished = job.get("finished_at")
    moment = time.time() if now is None else now
    if isinstance(finished, (int, float)) and not isinstance(finished, bool) and moment - finished > RETENTION_SEC:
        return False
    with _RECONCILE_LOCK:
        if job_id in feed().operation_results("settings_update_result"):
            return False
        logged = eventlog.append_event(
            "settings_update_result",
            operation_id=job_id,
            status=outcome,
            target_version=str(job.get("target") or "")[:40],
            previous_version=str(job.get("previous") or "")[:40],
            how="auto",
        )
        if logged is not None:
            feed().refresh()
        return logged is not None
