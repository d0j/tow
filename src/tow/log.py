from __future__ import annotations

import functools
import json
import os
import re
import sys
import threading
from collections.abc import Callable, Generator, Iterable, Iterator, Mapping
from contextlib import closing, contextmanager, suppress
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

from tow import errors, i18n
from tow.clock import format_ui_timestamp, iso_now
from tow.events import new_event_id
from tow.i18n import t
from tow.net_errors import humanize
from tow.paths import data_dir
from tow.platform import locks
from tow.records import ErrorFields
from tow.store import init_lock_file

# 5 MiB x (1 + 4 rotated) keeps months of history (1 MiB x 3 kept about ten days).
MAX_BYTES = 5 * 1024 * 1024
BACKUPS = 4
_TAIL_BYTES = 120_000
_LOG_THREAD_LOCK = threading.RLock()
EXPORT_EVENT_KEYS = {
    "ts",
    "created_at",
    "event_id",
    "kind",
    "operation_id",
    "topic_id",
    "topic",
    "status",
    "cls",
    "how",
    "client_id",
    "client_kind",
    "integration_id",
    "tracker",
    "hash",
    "apply",
    "ok",
    "n",
}


def owner_language() -> str:
    """The language of this page or task; outside both (a bare call), the owner's message language."""
    return i18n.current()


class Labels(Mapping[str, str]):
    """Labels from the language catalog, read as a plain mapping in the current language
    (``CLS_RU.get(cls, ...)`` keeps working); ``label(name, lang)`` picks another language."""

    def __init__(self, keys: dict[str, str]) -> None:
        self._keys = keys

    def __getitem__(self, name: str) -> str:
        return t(self._keys[name], owner_language())

    def __iter__(self) -> Iterator[str]:
        return iter(self._keys)

    def __len__(self) -> int:
        return len(self._keys)

    def label(self, name: str, lang: str, default: str = "") -> str:
        key = self._keys.get(name)
        return t(key, lang) if key else default


def kind_label(kind: str, lang: str | None = None) -> str:
    """The owner's word for an event kind: ``log.kind.<kind>`` from the language files (a new
    kind needs only a line there). A kind without one - say, from an older TOW's log - reads
    as words, never as an identifier."""
    key = f"log.kind.{kind}"
    if i18n.has(key):
        return t(key, lang or owner_language())
    return kind.replace("_", " ").strip()


_CLASSES = errors.STATUS_CLASSES
CLS_RU = Labels({cls: f"log.cls.{cls}" for cls in _CLASSES})


def cls_label(cls: str, lang: str | None = None) -> str:
    """The owner's word for an error class (``lang`` for a message, else this page's language)."""
    return CLS_RU.label(cls, lang or owner_language(), cls)


# Tracker announce addresses carry the owner's passkey (in the query or the path): udp:// as
# well as http(s)://, and inside a magnet link's tr= values.
_URL_RE = re.compile(r"(?:https?|udp|wss?)://[^\s'\"<>]+", re.IGNORECASE)
_MAGNET_RE = re.compile(r"magnet:\?[^\s'\"<>]*", re.IGNORECASE)
_MAGNET_XT_RE = re.compile(r"(?:^|&)xt=([^&]*)", re.IGNORECASE)
_SECRET_HEADER_RE = re.compile(r"\b(?:authorization|cookie|set-cookie)\s*[:=][^\r\n]*", re.IGNORECASE)
_SECRET_VALUE_RE = re.compile(
    r"\b(password|passphrase|passkey|token|secret|api[_-]?key|access[_-]?key|client[_-]?secret|uk|pk)\s*[:=]\s*"
    r"(?:\"[^\"\r\n]*\"|'[^'\r\n]*'|[^\s,;&]+)",
    re.IGNORECASE,
)


def _magnet(match: re.Match[str]) -> str:
    """A magnet link keeps only its info-hash (xt): trackers (tr=), peers and names go."""
    raw = match.group(0)
    stripped = raw.rstrip(".,;:)]}")
    xt = _MAGNET_XT_RE.search(stripped[len("magnet:?") :])
    return (f"magnet:?xt={xt.group(1)}" if xt else "magnet:?") + raw[len(stripped) :]


_BEARER_RE = re.compile(r"\bbearer\s+[^\s,;]+", re.IGNORECASE)


def scrub_text(value: str) -> str:
    def replace(match: re.Match[str]) -> str:
        raw = match.group(0)
        suffix = ""
        while raw and raw[-1] in ".,;:)]}":
            suffix = raw[-1] + suffix
            raw = raw[:-1]
        try:
            parsed = urlsplit(raw)
            hostname = parsed.hostname
            if not parsed.scheme or not hostname:
                return "[REDACTED_URL]" + suffix
            if ":" in hostname and not hostname.startswith("["):
                hostname = f"[{hostname}]"
            port = f":{parsed.port}" if parsed.port else ""
            return f"{parsed.scheme.lower()}://{hostname}{port}" + suffix
        except ValueError:
            return "[REDACTED_URL]" + suffix

    cleaned = _MAGNET_RE.sub(_magnet, value)
    cleaned = _URL_RE.sub(replace, cleaned)
    cleaned = _SECRET_HEADER_RE.sub("[REDACTED_HEADER]", cleaned)
    cleaned = _SECRET_VALUE_RE.sub(lambda match: f"{match.group(1)}=***", cleaned)
    return _BEARER_RE.sub("Bearer ***", cleaned)


# --- the status class of an error ---------------------------------------------------------------
# A typed error (tow.errors) carries its class: it never depends on the wording or the language.
# Text is matched only for what has no code - an error stored by TOW 1.17 or older (English or
# Russian, as those versions wrote it) and a foreign exception's message. The wording below is
# frozen: it describes what old versions wrote, so editing a language file never changes it.

# Messages whose tail is owner content (a file name, a topic title): the start fixes the class,
# so "Frozen.Planet…" or "Безлимитный…" in the tail cannot recolour them.
_LEGACY_STARTS = (
    ("previous torrent revision is still active", "qbit"),
    ("selected torrent client cannot", "qbit"),
    ("torrent hash is already claimed", "error"),
    ("клиент недоступен", "qbit"),
    ("client unreachable", "qbit"),
    ("раздача удалена из клиента", "qbit"),
    ("the torrent was removed from the client", "qbit"),
    ("мало места на диске", "disk"),
    ("not enough disk space", "disk"),
)
_LEGACY_MARKERS = {
    # The site's daily download limit only: a bare "лимит"/"quota" also matched
    # "Лимитированная серия" and "disk quota exceeded".
    "quota": ("лимит скачиваний", "daily download limit reached"),
    "tracker_auth": (
        "no download link on page",
        "tracker auth",
        "tracker_auth",
        "tracker authentication",
        "нужен вход",
        "log in to",
        "not a torrent (sign-in needed",
    ),
    "tracker": (
        "all hosts failed",
        "все зеркала",
        "cross-origin redirect",
        "запрещён cross-origin",
        "запрещено перенаправление на другой адрес или порт",
        "all mirrors are paused",
    ),
    "no_tracker": ("no tracker", "неизвестн", "unknown site"),
    "no_path": ("save_path", "папк", "no folder", "the client saved the torrent to a different folder"),
    "qbit": ("qbit", "торрент-клиент", "torrent client"),
}
# File names and paths inside other messages ("Room.401.mkv", "C:\\Media\\…").
_FILE_DETAIL_RE = re.compile(r"\S*[\\/]\S*|\S+\.[^\s.]{2,5}(?=[\s,;)]|$)")


@functools.cache
def _client_prefixes(_version: int) -> tuple[str, ...]:
    """``<client>:`` for every client module (its kind, title and header label): a client
    adapter writes its name in front of its messages. A new client module is found by itself."""
    from tow.clients.spec import discover

    names = {name for spec in discover().values() for name in (spec.kind, spec.title, spec.short) if name}
    return tuple(sorted(f"{name.casefold()}:" for name in names))


def _has(m: str, cls: str) -> bool:
    return any(marker in m for marker in _LEGACY_MARKERS[cls])


def _legacy_class(msg: str) -> str:
    """The class of an error known only by its text (see above)."""
    m = (msg or "").lower()
    head = m.removeprefix("reconcile: ")
    for prefix, cls in _LEGACY_STARTS:
        if head.startswith(prefix):
            return cls
    m = _FILE_DETAIL_RE.sub(" ", m)
    if _has(m, "quota"):
        return "quota"
    if _has(m, "tracker_auth"):
        return "tracker_auth"
    if "cloudflare" in m or "just a moment" in m:
        return "cloudflare"
    if re.search(r"\bhttp 4(?:04|10)\b", m) and "all hosts failed" in m:
        return "gone"  # every mirror says the topic does not exist (404) or is gone for good (410)
    if _has(m, "tracker"):
        return "tracker"
    if "frozen" in m:
        return "frozen"
    if _has(m, "no_tracker"):
        return "no_tracker"
    if _has(m, "no_path"):
        return "no_path"
    # Torrent-client failures: qBittorrent's own messages, TOW's neutral wording and the
    # other adapters, which prefix every message with the client's name.
    if _has(m, "qbit") or m.startswith(_client_prefixes(i18n.version())):
        return "qbit"
    if "not a torrent" in m or "не торрент-файл" in m or "не torrent" in m:
        return "not_torrent"
    if re.search(r"\b40[13]\b", m) or "login" in m:
        return "auth"
    return "error"


def error_class(error: Any, code: str | None = None) -> str:
    """The status class of an error: a typed error's (or a stored record's) own class, then the
    class of ``code``; text is matched only as the fallback for errors without a code."""
    if isinstance(error, BaseException):
        record = errors.record_of(error)
        return str(record["cls"]) if record else _legacy_class(str(error))
    if isinstance(error, Mapping):
        cls = error.get("cls")
        if isinstance(cls, str) and cls in _CLASSES:
            return cls
        code = code or (error.get("code") if isinstance(error.get("code"), str) else None)
        error = error.get("text") or ""
    if code and (cls := errors.class_of(code)):
        return cls
    return _legacy_class(str(error or ""))


def is_daily_limit(error: Any) -> bool:
    """The site said "download limit for today"."""
    return error_class(error) == "quota"


def error_fields(error: BaseException | str) -> ErrorFields:
    """The fields a log event keeps about an error: its text (in the language of the moment, for
    older readers), its code and values (rendered again in the reader's language) and its class."""
    record = errors.record_of(error) if isinstance(error, BaseException) else None
    fields = ErrorFields(error=str(error), cls=error_class(error))
    if record:
        fields["error_code"] = record["code"]
        fields["error_params"] = record["params"]
    return fields


def _redact(obj: Any) -> Any:
    if isinstance(obj, dict):
        out = {}
        for k, v in obj.items():
            lk = str(k).lower()
            if any(s in lk for s in ("pass", "token", "secret", "cookie", "uid")) or lk in {"uk", "pk"}:
                out[k] = "***"
            else:
                out[k] = _redact(v)
        return out
    if isinstance(obj, str):
        return scrub_text(obj)
    if isinstance(obj, list):
        return [_redact(x) for x in obj]
    return obj


def export_event_projection(record: dict[str, Any]) -> dict[str, Any]:
    """Return the bounded, non-secret diagnostic schema used by .towx exports."""
    if not isinstance(record, dict):
        return {}
    out: dict[str, Any] = {}
    for key in EXPORT_EVENT_KEYS:
        value = record.get(key)
        if value is None or isinstance(value, (bool, int, float)):
            out[key] = value
        elif isinstance(value, str):
            if key == "hash" and value and (len(value) != 40 or not re.fullmatch(r"[0-9a-fA-F]{40}", value)):
                continue
            out[key] = value[:128]
    return out


def log_path() -> Path:
    return data_dir() / "tow.jsonl"


@contextmanager
def _log_file_lock() -> Iterator[None]:
    """Serialize log reads, rotation and append across threads and processes."""
    lock_path = data_dir() / ".tow-log.lock"
    lock_path.parent.mkdir(parents=True, exist_ok=True)
    with _LOG_THREAD_LOCK, lock_path.open("a+b") as handle:
        init_lock_file(handle)
        locks.lock(handle, poll=0.01)
        try:
            yield
        finally:
            locks.unlock(handle)


@contextmanager
def locked_log_path() -> Iterator[Path]:
    """A stable event-log file while a backup reads it: no append or rotation."""
    with _log_file_lock():
        yield log_path()


def _rotate_if_needed(path: Path) -> None:
    try:
        if not path.is_file() or path.stat().st_size < MAX_BYTES:
            return
    except OSError:
        return
    oldest = path.with_name(f"{path.name}.{BACKUPS}")
    if oldest.exists():
        oldest.unlink()
    for i in range(BACKUPS - 1, 0, -1):
        src = path.with_name(f"{path.name}.{i}")
        dst = path.with_name(f"{path.name}.{i + 1}")
        if src.exists():
            src.replace(dst)
    path.replace(path.with_name(f"{path.name}.1"))


def log_event(kind: str, **fields: Any) -> bool:
    ts = iso_now()
    rec = {
        "ts": ts,
        "created_at": ts,
        "event_id": new_event_id(),
        "kind": kind,
        **_redact(fields),
    }
    # The audit log is best-effort: a full disk or a file held open by a reader
    # must never abort the operation being logged (e.g. after a confirmed client add).
    try:
        path = log_path()
        # The log lock alone: no restore replaces the event log, so an event never waits
        # for the data lock (a night copy, a restore, a long check commit).
        with _log_file_lock():
            try:
                _rotate_if_needed(path)
            except OSError as exc:
                print(f"TOW log rotation skipped: {type(exc).__name__}", file=sys.stderr)
            with path.open("a", encoding="utf-8") as f:
                f.write(json.dumps(rec, ensure_ascii=False) + "\n")
    except Exception as exc:  # noqa: BLE001 - an unwritable log must not abort the logged operation
        print(f"TOW log write failed ({kind}): {type(exc).__name__}", file=sys.stderr)
        return False
    return True


def read_events(*, limit: int = 80) -> list[dict[str, Any]]:
    path = log_path()
    with _log_file_lock():
        if not path.is_file():
            return []
        # Only the tail is shown: read just that, not the whole (multi-MiB) file.
        with path.open("rb") as handle:
            size = handle.seek(0, os.SEEK_END)
            handle.seek(max(0, size - _TAIL_BYTES))
            raw = handle.read()
    text = raw.decode("utf-8", "replace")
    lines = [ln for ln in text.splitlines() if ln.strip()]
    out: list[dict[str, Any]] = []
    for ln in lines[-limit:]:
        try:
            rec = json.loads(ln)
        except json.JSONDecodeError:
            continue
        if isinstance(rec, dict):
            out.append(rec)
    out.reverse()
    return out[:limit]


# G7: the history page shows what happened to the downloads, not every bookkeeping event.
HISTORY_GROUPS = {
    "downloads": frozenset(
        {
            "client_added",
            "client_started",
            "client_updated",
            "new_file",
            "revision_updated",
            "file_completed",
            "episode_completed",
            "client_removed",
            "client_restored",
            "client_stopped",
            "client_adopted",
        }
    ),
    "errors": frozenset(
        {
            "check_fail",
            "content_cache_failed",
            "client_add_failed",
            "reconcile_failed",
            "check_blocked",
            "client_unreachable",
            "browser_auth_failed",
            "watchdog_alert",
            "client_stop_failed",
            "client_adopt_failed",
            "backup_cleanup_pending",
            "settings_backup_check_fail",
            "settings_backup_delete_fail",
        }
    ),
    "changes": frozenset(
        {
            "topic_add",
            "topic_delete",
            "topic_edit",
            "topic_pause",
            "undo",
            "site_add",
            "site_edit",
            "site_delete",
            "settings_interval",
            "backup_created",
            "backup_restored",
            "settings_restore_point_applied",
            "settings_portable_restore",
            "settings_update_started",
            "settings_backup_deleted",
            "settings_backup_retention",
            "settings_backup_automatic",
        }
    ),
    "notifications": frozenset({"bot_delivery_succeeded", "bot_delivery_failed"}),
}


_KIND_RE = re.compile(r'"kind":\s*"([A-Za-z0-9_]+)"')
# The fields event_title looks up a topic's current name by (a superset: either id field).
_TITLE_REF_RE = re.compile(r'"(topic_id|topic|hash)":\s*"((?:[^"\\]|\\.)*)"')
_BLOCK_BYTES = 256 * 1024


def _lines_newest_first(path: Path, *, live: bool, skip: Callable[[bytes], bool] | None = None) -> Generator[str]:
    """The lines of one log file from its end, read in blocks: the history page usually needs
    only the newest events, not the whole file (the live one can be tens of MiB). ``skip``: a
    test of a block's whole lines (raw) that says none of them is wanted."""
    try:
        if live:
            with _log_file_lock():  # the size between two appends: no half-written line
                handle = path.open("rb")
                end = handle.seek(0, os.SEEK_END)
        else:
            handle = path.open("rb")
            end = handle.seek(0, os.SEEK_END)
    except OSError:
        return
    # The bytes up to ``end`` never change: the log is only appended to and rotated by renaming.
    with handle:
        rest = b""
        while end > 0:
            start = max(0, end - _BLOCK_BYTES)
            try:
                handle.seek(start)
                block = handle.read(end - start) + rest
            except OSError:
                return
            end = start
            lines = block.split(b"\n")
            rest = lines.pop(0) if start > 0 else b""  # maybe the end of a line in the block before
            if skip is not None and skip(block[len(rest) + 1 :] if start > 0 else block):
                continue
            for line in reversed(lines):
                if line.strip():
                    yield line.decode("utf-8", "replace")


def _title_refs(needle: str, title_index: Mapping[str, str] | None) -> tuple[set[str], set[str]]:
    """(topic ids, hashes) whose current name contains the search text."""
    ids: set[str] = set()
    hashes: set[str] = set()
    for key, title in (title_index or {}).items():
        if needle in title.casefold():
            kind, _, value = key.partition(":")
            (ids if kind == "id" else hashes).add(value)
    return ids, hashes


@functools.cache
def _ascii_folds() -> dict[str, frozenset[bytes]]:
    """For each ASCII character, the non-ASCII characters (UTF-8) whose casefold holds it: ß and
    ẞ fold to "ss", the Kelvin sign to "k", ﬁ to "fi". Only these let a line with no ASCII copy of
    the search text still hold it once folded. They are all in the Basic Multilingual Plane
    (a test checks every code point of this Python's Unicode), which is read in a few ms."""
    found: dict[str, set[bytes]] = {}
    ascii_char = re.compile(r"[\x00-\x7f]")
    for start in range(0x80, 0x10000, 256):
        chunk = "".join(map(chr, range(start, start + 256)))
        if not ascii_char.search(chunk.casefold()):
            continue  # nearly every chunk: checked as a whole
        for char in chunk:
            for folded in char.casefold():
                if folded.isascii():
                    found.setdefault(folded, set()).add(char.encode("utf-8"))
    return {char: frozenset(encoded) for char, encoded in found.items()}


def _block_without(needle: str, refs: tuple[set[str], set[str]]) -> Callable[[bytes], bool] | None:
    """A test that no line of a raw block can pass ``_may_match`` (None: no such cheap test).

    For an ASCII search text and no topic named by it, ``_may_match`` takes a line only when the
    text is in its casefold or the line has an escape. A line's ASCII bytes are its ASCII
    characters (UTF-8 never uses them inside a longer character), so the text can be in the
    casefold only if it is in the block's bytes with ASCII lowered, or if the block has one of
    the few characters that fold to letters of the text: one pass over the bytes instead of a
    decode and a casefold of every line (a search with no result read 25 MB line by line).
    """
    if refs[0] or refs[1] or not needle.isascii():
        return None
    target = needle.encode("ascii")
    folds = _ascii_folds()
    folding = tuple({encoded for char in set(needle) for encoded in folds.get(char, ())})

    def without(block: bytes) -> bool:
        return target not in block.lower() and b"\\u" not in block and not any(item in block for item in folding)

    return without


def _may_match(line: str, needle: str, refs: tuple[set[str], set[str]]) -> bool:
    """Whether a raw line can be a search result, without parsing it: the text is in the line
    (written by json.dumps exactly as the full test writes the record again), or the line names
    a topic whose current name has it. Escaped text is parsed: it may hide the needle."""
    if needle in line.casefold() or "\\u" in line:
        return True
    ids, hashes = refs
    if not ids and not hashes:
        return False
    for match in _TITLE_REF_RE.finditer(line):
        value = match.group(2)
        if "\\" in value or value in ids or value.upper() in hashes:
            return True
    return False


def history_events(
    *, group: str = "", text: str = "", limit: int = 300, title_index: Mapping[str, str] | None = None
) -> list[dict[str, Any]]:
    """Newest first, across the current log and its rotated files (G7)."""
    kinds = HISTORY_GROUPS.get(group) or frozenset().union(*HISTORY_GROUPS.values())
    needle = text.strip().casefold()
    # A quote or a backslash in the search text is escaped in the raw line: no shortcut then.
    shortcut = bool(needle) and '"' not in needle and "\\" not in needle
    refs = _title_refs(needle, title_index) if shortcut else (set(), set())
    skip = _block_without(needle, refs) if shortcut else None
    path = log_path()
    files = [path, *(path.with_name(f"{path.name}.{index}") for index in range(1, BACKUPS + 1))]
    out: list[dict[str, Any]] = []
    for index, candidate in enumerate(files):
        # Only the live file is being written: rotated files are read without the log lock,
        # so a running check is not held up by the history page.
        with closing(_lines_newest_first(candidate, live=index == 0, skip=skip)) as lines:
            for line in lines:
                kind = _KIND_RE.search(line)
                if kind is None or kind.group(1) not in kinds:
                    continue  # most lines are other events: no JSON parsing for them
                if shortcut and not _may_match(line, needle, refs):
                    continue
                try:
                    record = json.loads(line)
                except json.JSONDecodeError:
                    continue
                if not isinstance(record, dict) or record.get("kind") not in kinds:
                    continue
                if (
                    needle
                    and needle not in json.dumps(record, ensure_ascii=False).casefold()
                    and needle not in event_title(record, title_index).casefold()
                ):
                    continue
                out.append(record)
                if len(out) >= limit:
                    return out
    return out


ERR_RU = Labels(
    {
        "frozen": "log.err.frozen",
        "nnmclub: no download link on page": "log.err.no_download_link",
        "no tracker": "log.err.no_tracker",
        "no save_path": "log.err.no_save_path",
        "qbit down": "log.err.qbit_down",
    }
)


def index_event_titles(topics: Iterable[Mapping[str, Any]]) -> dict[str, str]:
    """Current names for historical events that recorded only a topic ID or torrent hash.

    A shared hash cannot identify a unique topic; leave those events as hashes rather than
    guessing. This index changes only how the log is displayed, never the stored records.
    """
    names: dict[str, str] = {}
    conflicts: set[str] = set()
    for topic in topics:
        title = str(topic.get("title") or topic.get("tracker_title") or "").strip()
        if not title:
            continue
        keys = [f"id:{topic['id']}"] if topic.get("id") else []
        for value in [topic.get("hash"), *(topic.get("previous_hashes") or [])]:
            h = str(value or "")
            if re.fullmatch(r"[0-9a-fA-F]{40}", h):
                keys.append(f"hash:{h.upper()}")
        for key in keys:
            if key in names and names[key] != title:
                conflicts.add(key)
            else:
                names[key] = title
    for key in conflicts:
        names.pop(key, None)
    return names


def event_title(rec: Mapping[str, Any], title_index: Mapping[str, str] | None = None) -> str:
    title = str(rec.get("title") or "").strip()
    if title or not title_index:
        return title
    topic_id = str(rec.get("topic_id") or rec.get("topic") or "")
    if topic_id and (title := title_index.get(f"id:{topic_id}", "")):
        return title
    h = str(rec.get("hash") or "")
    if re.fullmatch(r"[0-9a-fA-F]{40}", h):
        return title_index.get(f"hash:{h.upper()}", "")
    return ""


def _shown_kind(rec: Mapping[str, Any], kind: str) -> str:
    """A pause button toggles: its one event kind reads as "resumed" when it turned pause off."""
    if kind == "topic_pause" and rec.get("paused") is False:
        return "topic_resume"
    if kind == "site_pause" and rec.get("status") == "resumed":
        return "site_resume"
    return kind


def format_event(rec: Mapping[str, Any], *, title_index: Mapping[str, str] | None = None) -> dict[str, str]:
    ts = str(rec.get("created_at") or rec.get("ts") or "")
    at = ts
    with suppress(TypeError, ValueError):
        at = format_ui_timestamp(ts)
    kind = str(rec.get("kind") or "")
    lang = owner_language()
    label = kind_label(_shown_kind(rec, kind), lang)
    label = label[:1].upper() + label[1:]  # a line of the log starts like a sentence
    title = event_title(rec, title_index)[:100]
    bits: list[str] = []
    how = rec.get("how")
    if how == "manual":
        bits.append(t("log.how.manual", lang))
    elif how == "auto":
        bits.append(t("log.how.auto", lang))
    elif how == "timer":
        bits.append(t("log.how.timer", lang))
    if kind == "backup_cleanup_pending" or kind.startswith("settings_backup_"):
        cleanup_key = {
            "night": "log.cleanup.night",
            "safety": "log.cleanup.safety",
            "restore_point": "log.cleanup.restore_point",
        }.get(str(rec.get("copy_kind") or ""))
        if cleanup_key:
            bits.append(t(cleanup_key, lang))
    cls = str(rec.get("cls") or "")
    if cls:
        bits.append(CLS_RU.label(cls, lang, cls))
    if title:
        bits.append(title)
    raw = ""
    if rec.get("error") or rec.get("error_code"):
        err = scrub_text(str(rec.get("error") or "")).strip()
        shown = errors.render_stored(rec.get("error_code"), rec.get("error_params"), "", lang)
        text = scrub_text(shown) if shown else ERR_RU.label(err, lang, err)
        # A socket or HTTP-library error in words; its raw text goes under "Details".
        human = humanize(text, lang)
        raw = text[:600] if human != text else ""
        if cls and human.casefold().startswith(CLS_RU.label(cls, lang, cls).casefold()):
            bits.remove(CLS_RU.label(cls, lang, cls))  # "torrent client · torrent client: refused" says it twice
        bits.append(human[:220])
    elif kind == "check" and rec.get("n") is not None:
        bits.append(t("log.check_result", lang, ok=rec.get("ok"), n=rec.get("n")))
        if rec.get("apply") is False:
            bits.append(t("log.check_preview", lang))
    alerts = rec.get("alerts")
    if isinstance(alerts, list):  # the watchdog's messages, as they were sent
        bits.extend(scrub_text(str(alert)).strip()[:400] for alert in alerts if str(alert).strip())
    if rec.get("tracker"):
        bits.append(str(rec["tracker"]))
    h = str(rec.get("hash") or "")
    if len(h) == 40 and re.fullmatch(r"[0-9a-fA-F]{40}", h):
        bits.append(h[:8])
    if rec.get("path"):
        path = str(rec["path"])
        bits.append(path if len(path) <= 80 else "…" + path[-79:])  # the folder's own name is at the end
    u = scrub_text(str(rec.get("url") or "")).strip()
    if u and not u.startswith(("***",)):
        bits.append(u[:90])
    detail = " · ".join(dict.fromkeys(bits))
    return {"at": at, "label": label, "detail": detail, "kind": kind, "raw": raw}
