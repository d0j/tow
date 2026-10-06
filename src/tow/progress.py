from __future__ import annotations

import hashlib
import json
import math
import re
import stat
import time
from collections.abc import Mapping
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from tow.clients.files import map_files, normalize_path
from tow.clients.spec import TorrentClientAdapter
from tow.clock import iso_from_epoch, parse_timestamp
from tow.episodes import (
    expected_for_topic,
    is_video_file,
    item_identity,
    normalize_episode_seasons,
    parse_episode_label,
    parse_season_hint,
    resolve_episode_coverages,
    summarize_completion,
)
from tow.errors import Msg, TowError
from tow.folders import paths_equal
from tow.i18n import t
from tow.jsonish import as_dict
from tow.log import owner_language
from tow.records import DownloadHistory, HistoryItem, HistoryRecord, Topic
from tow.selection import SelectionError, SelectionPlan, policy_from_topic, resolve_selection
from tow.torrent import TorrentFile


def _get(value: Any, key: str, default: Any = None) -> Any:
    if isinstance(value, dict):
        return value.get(key, default)
    return getattr(value, key, default)


# How long a file seen on disk counts as confirmed (see reconcile_topic).
CONFIRMATION_REUSE_SEC = 24 * 3600


def safe_relative_path(save_path: str, client_file_name: str, *, base: Path | None = None) -> str | None:
    """``base`` is ``Path(save_path).resolve()`` when the caller already has it."""
    raw = str(client_file_name or "").replace("\\", "/")
    if raw.startswith("/"):
        return None
    parts = [part for part in raw.split("/") if part not in ("", ".")]
    if not parts or any(part == ".." for part in parts) or ":" in parts[0]:
        return None
    rel = "/".join(parts)
    base = base or Path(save_path).resolve()
    target = (base / Path(*parts)).resolve()
    try:
        target.relative_to(base)
    except ValueError:
        return None
    return rel


def filesystem_confirmation(
    save_path: str, relative_path: str, expected_size: int | None, *, base: Path | None = None
) -> bool | None:
    from tow.folders import seen_from_here

    folder = Path(save_path)
    if not seen_from_here(save_path):
        return None  # /downloads of a remote client, or a drive this PC does not see
    try:
        base = base or folder.resolve()
        path = (base / Path(*relative_path.split("/"))).resolve()
        path.relative_to(base)
        details = path.stat()
        if not stat.S_ISREG(details.st_mode):
            return False
        return not (expected_size is not None and details.st_size < int(expected_size))
    except FileNotFoundError:
        return False
    except OSError, ValueError, TypeError:
        return None


def _completion_key(item: HistoryItem) -> tuple[float, int, int, str]:
    try:
        timestamp = parse_timestamp(str(item.get("completed_observed_at"))).timestamp()
    except TypeError, ValueError, OverflowError, OSError:
        timestamp = 0.0
    label = parse_episode_label(str(item.get("label") or item.get("identity") or ""))
    return (
        timestamp,
        label.season if label and label.season is not None else -1,
        label.episode if label else -1,
        str(item.get("label") or item.get("identity") or ""),
    )


def _set_latest_event(record: HistoryRecord, event: dict[str, Any]) -> None:
    current = record.get("last_event")
    if not isinstance(current, dict) or not current.get("at"):
        record["last_event"] = event
        return
    try:
        event_time = parse_timestamp(str(event["at"]))
        current_time = parse_timestamp(str(current["at"]))
        rank = {"episode_completed": 3, "file_completed": 2, "revision_updated": 1, "new_file": 0}
        is_newer = event_time > current_time or (
            event_time == current_time and rank.get(str(event.get("kind")), 0) > rank.get(str(current.get("kind")), 0)
        )
    except TypeError, ValueError, OverflowError, OSError:
        is_newer = True
    if is_newer:
        record["last_event"] = event


def _label_fields(value: Msg | str | None) -> dict[str, Any]:
    """An event's label: its text (for older readers) and, when it is TOW's own wording, its
    catalog key and values - the History and Home render it in the reader's language."""
    if isinstance(value, Msg):
        record = value.record()
        return {"label": value.text(), "label_code": record["code"], "label_params": record["params"]}
    return {"label": value}


def _set_label(event: dict[str, Any], value: Msg | str | None) -> None:
    event.pop("label_code", None)
    event.pop("label_params", None)
    event.update(_label_fields(value))


def _item_label(item: HistoryItem) -> Msg | str:
    """An item's label; an episode's comes again from its keys (so it is TOW's wording, typed)."""
    keys = {str(key) for key in item.get("episode_keys") or [] if key}
    if not keys and item.get("episode_key"):
        keys = {str(item["episode_key"])}
    label = _episode_keys_label_msg(keys) if keys else ""
    return label or str(item.get("label") or item.get("identity") or "") or Msg("progress.file")


def _latest_item_label(record: HistoryRecord, *, prefer_completed: bool = True) -> Msg | str:
    all_items = list((record.get("items") or {}).values())
    items = [item for item in all_items if not item.get("superseded")] or all_items
    completed = [item for item in items if item.get("status") == "completed"]
    candidates = completed if prefer_completed and completed else items
    if not candidates:
        return Msg("progress.file")

    episode_candidates = [
        item
        for item in candidates
        if item.get("kind") != "file" and parse_episode_label(str(item.get("label") or item.get("identity") or ""))
    ]
    if episode_candidates:

        def episode_key(item: HistoryItem) -> tuple[int, int, str]:
            label = parse_episode_label(str(item.get("label") or item.get("identity") or ""))
            return (
                label.season if label and label.season is not None else -1,
                label.episode if label else -1,
                str(item.get("label") or item.get("identity") or ""),
            )

        return _item_label(max(episode_candidates, key=episode_key))

    labels = {str(item.get("label") or item.get("identity") or "") for item in candidates}
    if len(labels) == 1:
        return next(iter(labels)) or Msg("progress.file")
    return Msg("progress.files")


def _episode_keys_label(keys: set[str]) -> str:
    """The label of these episodes in the current language ("S01E05", "Episodes 05–07")."""
    return str(_episode_keys_label_msg(keys))


def _episode_keys_label_msg(keys: set[str]) -> Msg | str:
    parsed: list[tuple[int | None, int]] = []
    for key in keys:
        match = re.fullmatch(r"episode:(?:s(\d+))?e(\d+)", key)
        if match:
            parsed.append((int(match.group(1)) if match.group(1) else None, int(match.group(2))))
    if not parsed:
        return ""
    parsed.sort(key=lambda value: (-1 if value[0] is None else value[0], value[1]))
    if len(parsed) == 1:
        season, episode = parsed[0]
        return (
            f"S{season:02d}E{episode:02d}" if season is not None else Msg("progress.episode", episode=f"{episode:02d}")
        )
    seasons = {season for season, _episode in parsed}
    episodes = [episode for _season, episode in parsed]
    contiguous = episodes == list(range(episodes[0], episodes[-1] + 1))
    if len(seasons) == 1 and contiguous:
        season = parsed[0][0]
        if season is not None:
            return f"S{season:02d}E{episodes[0]:02d}–{episodes[-1]:02d}"
        return Msg("progress.episodes", first=f"{episodes[0]:02d}", last=f"{episodes[-1]:02d}")
    labels = [f"S{s:02d}E{e:02d}" if s is not None else f"E{e:02d}" for s, e in parsed]
    return ", ".join(labels[:8]) + ("…" if len(labels) > 8 else "")


def _repair_last_event_semantics(record: HistoryRecord) -> None:
    """Normalize legacy event labels after file names gain episode parsing."""
    event = record.get("last_event")
    if not isinstance(event, dict):
        return
    if event.get("kind") in {"client_added", "torrent_completed"}:
        _set_label(event, _latest_item_label(record, prefer_completed=event.get("kind") != "client_added"))
        return
    if event.get("kind") == "episode_completed":
        # Season hints can become available after an earlier completion was recorded. Reword
        # the same event from the current items completed in that pass; do not emit a new one.
        completed = {
            identity: item
            for identity, item in (record.get("items") or {}).items()
            if isinstance(item, dict)
            and not item.get("superseded")
            and item.get("status") == "completed"
            and event.get("at")
            and item.get("completed_observed_at") == event["at"]
        }
        keys = {
            str(key)
            for item in completed.values()
            if item.get("kind") == "episode"
            for key in item.get("episode_keys") or ([str(item["episode_key"])] if item.get("episode_key") else [])
        }
        if keys:
            _set_label(event, _episode_keys_label_msg(keys))
        elif completed and all(item.get("kind") == "file" for item in completed.values()):
            # A formerly misclassified special now has file evidence for the original
            # timestamp. Repair its wording silently, without inventing a new completion.
            event["kind"] = "file_completed"
            _set_label(event, _latest_item_label({"items": completed}))
        return
    if event.get("kind") != "file_completed":
        return
    items = record.get("items") or {}
    item = items.get(str(event.get("item") or ""))
    if not isinstance(item, dict) and event.get("relative_path"):
        wanted = str(event["relative_path"]).casefold()
        item = next(
            (
                candidate
                for candidate in items.values()
                if str(candidate.get("relative_path") or "").casefold() == wanted
            ),
            None,
        )
    if not isinstance(item, dict) or item.get("kind") != "episode":
        return
    keys = {str(value) for value in item.get("episode_keys") or [] if value}
    if not keys and item.get("episode_key"):
        keys.add(str(item["episode_key"]))
    event["kind"] = "episode_completed"
    _set_label(event, _episode_keys_label_msg(keys) or str(item.get("label") or "") or Msg("progress.episode_fallback"))


def _client_event(info: Any, client_adapter: TorrentClientAdapter) -> dict[str, Any] | None:
    completion_at = iso_from_epoch(_get(info, "completion_on"))
    added_at = iso_from_epoch(_get(info, "added_on"))
    if completion_at:
        kind = "torrent_completed"
        at = completion_at
    elif added_at:
        kind = "client_added"
        at = added_at
    else:
        return None
    return {
        "kind": kind,
        "label": None,
        "at": at,
        "source": "client",
        "client_id": client_adapter.client_id,
        "client_kind": client_adapter.client_kind,
    }


def _repair_synthetic_baseline_completion(record: HistoryRecord) -> None:
    repaired = set()
    for item in (record.get("items") or {}).values():
        if item.get("new_after_baseline") is not False:
            continue
        observed_at = item.get("completed_observed_at")
        if observed_at and observed_at == item.get("first_seen_at"):
            repaired.add(item.get("identity"))
            item.pop("completed_observed_at", None)
            item["completion_seen"] = True
    last = record.get("last_completed")
    if isinstance(last, dict) and last.get("identity") in repaired:
        record.pop("last_completed", None)


def _summary_record(topic_record: HistoryRecord, expected: dict[str, Any] | None) -> dict[str, Any]:
    summary = summarize_completion(topic_record.get("items") or {}, expected)
    topic_record["summary"] = summary
    completed = [
        item
        for item in (topic_record.get("items") or {}).values()
        if not item.get("superseded") and item.get("status") == "completed" and item.get("completed_observed_at")
    ]
    if completed:
        topic_record["last_completed"] = max(completed, key=_completion_key)
    else:
        topic_record.pop("last_completed", None)
    return summary


def _completed_episode_keys(items: dict[str, HistoryItem]) -> set[str]:
    groups: dict[str, list[bool]] = {}
    for item in items.values():
        if item.get("superseded"):
            continue
        keys = item.get("episode_keys") or ([str(item.get("episode_key"))] if item.get("episode_key") else [])
        if not keys and item.get("kind") != "file":
            label = parse_episode_label(str(item.get("label") or item.get("identity") or ""))
            keys = [label.key] if label else []
        for key in keys:
            groups.setdefault(str(key), []).append(item.get("status") == "completed")
    return {key for key, statuses in groups.items() if statuses and all(statuses)}


def _track_client_presence(
    record: HistoryRecord, info: Any, client_adapter: TorrentClientAdapter, now: str, events: list[str]
) -> bool:
    """Record client_removed/client_restored transitions; False when the torrent is gone."""
    client_fields = {
        "at": now,
        "source": "tow",
        "client_id": client_adapter.client_id,
        "client_kind": client_adapter.client_kind,
    }
    if not info:
        if record.get("client_present") is True:
            record["client_present"] = False
            events.append("client_removed")
            _set_latest_event(
                record, {"kind": "client_removed", **_label_fields(_latest_item_label(record)), **client_fields}
            )
        return False
    if record.get("client_present") is False:
        events.append("client_restored")
        _set_latest_event(
            record,
            {"kind": "client_restored", **_label_fields(Msg("progress.back_in_client")), **client_fields},
        )
    record["client_present"] = True
    return True


def _mark_superseded(items: dict[str, HistoryItem], current_source_hash: str, *, has_files: bool) -> None:
    """Items of other revisions are superseded; the current revision's are live again."""
    for historical_item in items.values():
        historical_hash = str(historical_item.get("source_hash") or "").casefold()
        if not historical_hash or historical_hash != current_source_hash:
            historical_item["superseded"] = True
        elif has_files:
            historical_item["superseded"] = False


def _check_save_path(topic: Topic, info: Any) -> str:
    """The folder whose files are evidence; the client must agree with the topic.

    A move the owner started (move_pending) is accepted while the client still
    moves or still reports the old folder; the marker is dropped once they agree.
    """
    topic_save_path = str(topic.get("save_path") or "")
    client_save_path = str(_get(info, "save_path", "") or "")
    move_pending = topic.get("move_pending") if isinstance(topic.get("move_pending"), dict) else None
    if client_save_path and topic_save_path and not paths_equal(client_save_path, topic_save_path):
        client_state = str(_get(info, "state", "") or "").casefold()
        still_moving = bool(
            move_pending
            and paths_equal(str(move_pending.get("to") or ""), topic_save_path)
            and (client_state == "moving" or paths_equal(client_save_path, str(move_pending.get("from") or "")))
        )
        if not still_moving:
            raise TowError("progress.path_differs")
    elif move_pending and client_save_path and paths_equal(client_save_path, str(move_pending.get("to") or "")):
        # Only what this check saw ends the move: the client now reports the folder it was sent to.
        topic.pop("move_pending", None)
    return client_save_path or topic_save_path


def _preferred_season(topic: Topic, selected_episode_keys: list[str]) -> int | None:
    """The season a season-relative numbering belongs to: the selection's, else the title's."""
    selected_seasons = {
        int(match.group(1)) for key in selected_episode_keys if (match := re.fullmatch(r"episode:s(\d+)e\d+", key))
    }
    if len(selected_seasons) == 1 and not any(re.fullmatch(r"episode:e\d+", key) for key in selected_episode_keys):
        return next(iter(selected_seasons))
    tracker_title = str(topic.get("tracker_title") or "")
    tracker_season = parse_season_hint(tracker_title)
    if tracker_season is not None:
        return tracker_season
    # A fresh tracker heading may change "14 из 14" to "14 из ?", losing the corroboration
    # that made "Show 3: ..." a safe season hint. Keep the original explicit hint unless the
    # tracker heading itself names one or more seasons ambiguously.
    if re.search(r"(?i)(?<!\w)s\d{1,2}(?!\d)|\b(?:season|сезон)\w*\b|\[\s*\d{1,2}[xх]\d", tracker_title):
        return None
    return parse_season_hint(str(topic.get("title") or ""))


def _client_files(
    files: list[Any], evidence_save_path: str, base: Path | None = None
) -> list[tuple[Any, str, int | None, bool]]:
    """(row, safe relative path, size, selected) for every usable client file row."""
    prepared: list[tuple[Any, str, int | None, bool]] = []
    for row in files:
        priority = _get(row, "priority")
        try:
            selected = priority is None or int(priority) != 0
        except TypeError, ValueError:
            continue
        rel = safe_relative_path(evidence_save_path, str(_get(row, "name", "") or ""), base=base)
        if not rel:
            continue
        size = _get(row, "size")
        try:
            size = int(size) if size is not None else None
        except TypeError, ValueError:
            size = None
        prepared.append((row, rel, size, selected))
    return prepared


def _season_relative_expected(
    expected: dict[str, Any] | None, observed_episode_keys: list[str], preferred_season: int | None
) -> dict[str, Any] | None:
    """A title total like "из 12" with numbering from E13 on is season-relative when the
    files are S0xE01..E12 of one season; return the corrected expectation, else None."""
    if not (
        expected
        and expected.get("source") == "title"
        and int(expected.get("episode_min") or 1) > 1
        and observed_episode_keys
    ):
        return None
    season_relative = [re.fullmatch(r"episode:s(\d+)e(\d+)", key) for key in observed_episode_keys]
    observed_seasons = {int(match.group(1)) for match in season_relative if match}
    if (
        all(season_relative)
        and len(observed_seasons) == 1
        and (preferred_season is None or preferred_season in observed_seasons)
        and min(int(match.group(2)) for match in season_relative if match) == 1
        and max(int(match.group(2)) for match in season_relative if match) <= int(expected["total"])
    ):
        return {
            **expected,
            "episode_min": 1,
            "episode_max": int(expected["total"]),
            "numbering": "season-relative",
        }
    return None


def _selection_is_current(topic: Topic) -> bool:
    return (
        not topic.get("selection_dirty")
        and topic.get("selection_verified") is not False
        and (
            not topic.get("selection_hash")
            or str(topic.get("selection_hash")).casefold() == str(topic.get("hash") or "").casefold()
        )
    )


def _client_content_root(content_path: str, save_path: str) -> str:
    """Only a direct child reported by the client is a root, never a path suffix guess.

    Work lexically: the client may be remote or use a different OS's path syntax.
    A single-file content path is harmless: no file has that name as a directory.
    """
    content = content_path.replace("\\", "/").rstrip("/")
    if any(part in {".", ".."} for part in content.split("/")):
        return ""
    parent, separator, name = content.rpartition("/")
    if not separator or not name or not paths_equal(parent or "/", save_path):
        return ""
    return name


def _file_rule_plan(
    topic: Topic,
    prepared_all: list[tuple[Any, str, int | None, bool]],
    preferred_season: int | None,
    *,
    content_path: str = "",
    save_path: str = "",
) -> SelectionPlan | None:
    """Re-read literal/mask membership, not client priorities or stale episode caches."""
    mode = str(as_dict(topic.get("selection")).get("mode") or "all")
    if not prepared_all or mode not in {"files", "exact"} or not _selection_is_current(topic):
        return None
    root = _client_content_root(content_path, save_path)
    native_rows = tuple(
        TorrentFile(index, rel, size if size is not None else 0)
        for index, (_row, rel, size, _selected) in enumerate(prepared_all)
        if (mode != "exact" or size is not None) and (size is None or 0 <= size <= 2**63 - 1)
    )
    policy = policy_from_topic(topic)
    canonical: dict[int, str] = {}
    if mode == "exact":
        wanted = tuple(TorrentFile(index, item["path"], item["size"]) for index, item in enumerate(policy["files"]))
        mapping = map_files(
            wanted,
            [{"index": row.index, "name": row.path, "size": row.size} for row in native_rows],
            root,
            fail=lambda _path: SelectionError("selection.exact_changed"),
        )
        canonical = {mapping[row.index]: row.path for row in wanted}

    def relative(path: str) -> str:
        first, separator, rest = path.partition("/")
        return rest if root and separator and normalize_path(first) == normalize_path(root) else path

    rows = tuple(TorrentFile(row.index, canonical.get(row.index, relative(row.path)), row.size) for row in native_rows)
    return resolve_selection(rows, policy, preferred_season=preferred_season)


def _selection_fingerprint(topic: Topic) -> str | None:
    mode = str(as_dict(topic.get("selection")).get("mode") or "all")
    if mode not in {"all", "files", "exact"} or not _selection_is_current(topic):
        return None
    keys = [str(value) for value in topic.get("selected_episode_keys") or []]
    context = [
        str(topic.get("hash") or "").casefold(),
        str(topic.get("client_id") or ""),
        policy_from_topic(topic),
        _preferred_season(topic, keys),
    ]
    if mode == "all":
        context.append(expected_for_topic(topic))
    return hashlib.sha256(json.dumps(context, sort_keys=True, separators=(",", ":")).encode("utf-8")).hexdigest()


def _expected_from_observed(
    topic: Topic,
    expected: dict[str, Any] | None,
    observed_episode_keys: list[str],
    selected_episode_keys: list[str],
) -> dict[str, Any] | None:
    """Count current evidence without turning an unconfirmed rule into a known target."""
    mode = str(as_dict(topic.get("selection")).get("mode") or "all")
    partial_selection = mode != "all"
    uncertain_selection = partial_selection and (
        not _selection_is_current(topic) or (mode == "episodes" and not selected_episode_keys)
    )
    if observed_episode_keys and (
        uncertain_selection
        or expected is None
        or (not partial_selection and int(expected.get("total") or 0) < len(observed_episode_keys))
        or (
            not partial_selection
            and expected.get("source") == "files"
            and len(observed_episode_keys) >= int(expected.get("total") or 0)
            and set(expected.get("keys") or []) != set(observed_episode_keys)
        )
    ):
        return {
            "kind": "episodes",
            "total": len(observed_episode_keys),
            "keys": observed_episode_keys,
            "source": "selection" if partial_selection and not uncertain_selection else "files",
            "confidence": "exact" if partial_selection and not uncertain_selection else "current",
        }
    return None


def _seasoned_selection_expected(
    expected: dict[str, Any] | None, observed_episode_keys: list[str], preferred_season: int | None
) -> dict[str, Any] | None:
    """Match old seasonless selection keys to one unambiguous selected file season.

    The persisted selection is left untouched: a later torrent revision can replace
    the files, and a season mismatch must never count an unrelated episode.
    """
    if not expected or expected.get("source") != "selection":
        return None
    observed_seasons = {
        int(match.group(1)) for key in observed_episode_keys if (match := re.fullmatch(r"episode:s(\d+)e\d+", key))
    }
    if len(observed_seasons) != 1:
        return None
    season = next(iter(observed_seasons))
    if preferred_season is not None and preferred_season != season:
        return None
    observed = set(observed_episode_keys)
    keys = list(expected.get("keys") or [])
    aligned: list[str] = []
    for key in keys:
        match = re.fullmatch(r"episode:e(\d+)", key)
        seasoned = f"episode:s{season:02d}e{int(match.group(1)):02d}" if match else ""
        aligned.append(seasoned if key not in observed and seasoned in observed else key)
    return {**expected, "keys": aligned} if aligned != keys else None


def _path_key(item: HistoryItem) -> str:
    return str(item.get("relative_path") or "").casefold()


class _HistoryIndex:
    """History items by relative path, so each client file looks at its own few items
    instead of scanning the whole history (several times) - F5.

    Entries are verified on use: an item moved to another identity or path leaves a
    stale entry that is simply skipped; ``add`` (re)registers an item's current path.
    """

    def __init__(self, items: dict[str, HistoryItem]) -> None:
        self._items = items
        self._by_path: dict[str, list[str]] = {}
        for identity in items:
            self.add(identity)

    def add(self, identity: str) -> None:
        item = self._items.get(identity)
        if isinstance(item, dict):
            bucket = self._by_path.setdefault(_path_key(item), [])
            if identity not in bucket:
                bucket.append(identity)

    def at_path(self, rel: str) -> list[tuple[str, HistoryItem]]:
        key = rel.casefold()
        found = []
        for identity in dict.fromkeys(self._by_path.get(key, ())):
            item = self._items.get(identity)
            if isinstance(item, dict) and _path_key(item) == key:
                found.append((identity, item))
        return found


def _adopt_previous_item(
    items: dict[str, HistoryItem],
    base_identity: str,
    *,
    rel: str,
    size: int | None,
    label: Any,
    coverage: tuple[Any, ...],
    current_source_hash: str,
    current_client_paths: set[str],
    index: _HistoryIndex,
    orphans_by_size: dict[Any, list[str]],
) -> None:
    """Carry an existing history item over to ``base_identity`` when the same file was
    renamed in the client, stored under the legacy episode key, or tracked by path."""
    if base_identity not in items and size is not None:
        rename_candidates = [
            (old_identity, old_item)
            for old_identity in orphans_by_size.get(size, ())
            if isinstance(old_item := items.get(old_identity), dict)
            and str(old_item.get("source_hash") or "").casefold() == current_source_hash
            and _path_key(old_item) not in current_client_paths
            and old_item.get("size") == size
        ]
        if len(rename_candidates) == 1:
            old_identity, renamed = rename_candidates[0]
            items.pop(old_identity)
            renamed["identity"] = base_identity
            items[base_identity] = renamed
    legacy_key = label.key if label else ""
    legacy = items.get(legacy_key) if legacy_key else None
    if (
        base_identity not in items
        and isinstance(legacy, dict)
        and str(legacy.get("relative_path") or "").casefold() == rel.casefold()
    ):
        items.pop(legacy_key, None)
        legacy["identity"] = base_identity
        legacy["episode_key"] = legacy_key  # == label.key: legacy exists only for a label
        legacy["episode_keys"] = [item.key for item in coverage]
        items[base_identity] = legacy
    same_path_current = [
        (old_identity, old_item)
        for old_identity, old_item in index.at_path(rel)
        if str(old_item.get("source_hash") or "").casefold() in {"", current_source_hash}
    ]
    if base_identity not in items and len(same_path_current) == 1:
        old_identity, migrated = same_path_current[0]
        items.pop(old_identity, None)
        migrated["identity"] = base_identity
        items[base_identity] = migrated
    index.add(base_identity)


def _file_progress(row: Any, info: Any) -> float:
    """Per-file progress (0..1), falling back to the torrent's progress."""
    progress = _get(row, "progress", _get(info, "progress", 0.0))
    if isinstance(progress, bool) or not isinstance(progress, (int, float, str)):
        return 0.0
    try:
        value = float(progress)
        return value if math.isfinite(value) and 0.0 <= value <= 1.0 else 0.0
    except TypeError, ValueError, OverflowError:
        return 0.0


def _revision_identity(
    items: dict[str, HistoryItem], base_identity: str, rel: str, source_hash: str, index: _HistoryIndex
) -> tuple[str, bool]:
    """Identity for this file in the current revision; a file whose path belonged to an
    older revision gets a ``#revision:<hash>`` identity so both stay in the history."""
    current = source_hash.casefold()
    previous = items.get(base_identity)
    previous_is_current = bool(previous and str(previous.get("source_hash") or "").casefold() == current)
    is_revision = bool(
        not previous_is_current
        and any(
            old_item.get("source_hash") and str(old_item.get("source_hash")).casefold() != current
            for _identity, old_item in index.at_path(rel)
        )
    )
    return (f"{base_identity}#revision:{current}" if is_revision else base_identity), is_revision


def _carry_completion(
    identity: str, rel: str, size: int | None, current_source_hash: str, index: _HistoryIndex
) -> HistoryItem | None:
    """For a new item: the completed same-size item at the same path (whose completion
    carries over), superseding same-path items of older revisions on the way."""
    carried: HistoryItem | None = None
    for old_identity, old_item in index.at_path(rel):
        if old_identity == identity:
            continue
        if (
            old_item.get("status") == "completed"
            and old_item.get("size") is not None
            and size is not None
            and old_item.get("size") == size
        ):
            carried = old_item
        if str(old_item.get("source_hash") or "").casefold() != current_source_hash:
            old_item["superseded"] = True
    return carried


@dataclass
class _ReconcilePass:
    """One topic's reconcile in progress: what its per-file steps share and collect."""

    topic: Topic
    client: TorrentClientAdapter
    record: HistoryRecord
    items: dict[str, HistoryItem]
    info: Any
    now: str
    initial_baseline: bool
    evidence_save_path: str
    evidence_base: Path | None
    current_client_paths: set[str]
    events: list[str]
    index: _HistoryIndex = field(init=False)
    orphans_by_size: dict[Any, list[str]] = field(init=False)
    observed_completed_episode_keys: set[str] = field(default_factory=set)
    completed_file_labels: list[str] = field(default_factory=list)
    seen_identities: set[str] = field(default_factory=set)

    def __post_init__(self) -> None:
        self.index = _HistoryIndex(self.items)
        self.orphans_by_size = {}
        for old_identity, old_item in self.items.items():
            if isinstance(old_item, dict) and _path_key(old_item) not in self.current_client_paths:
                self.orphans_by_size.setdefault(old_item.get("size"), []).append(old_identity)

    @property
    def source_hash(self) -> str:
        return str(self.topic.get("hash") or "")

    def event(self, kind: str, label: Msg | str | None, **fields: Any) -> dict[str, Any]:
        """An event TOW observed now (``fields``: what it is about)."""
        return {
            "kind": kind,
            **_label_fields(label),
            "at": self.now,
            "source": "tow",
            **fields,
            "client_id": self.client.client_id,
            "client_kind": self.client.client_kind,
        }


def _open_record(topic: Topic, history: DownloadHistory) -> tuple[HistoryRecord, bool, dict[str, Any] | None]:
    """The topic's history record (created on first sight), whether this is its first scan
    (the baseline: nothing seen then is "new"), and how many episodes are expected."""
    topics = history.setdefault("topics", {})
    record = topics.setdefault(str(topic.get("id") or ""), {"items": {}})
    initial_baseline = "baseline_at" not in record
    record.setdefault("items", {})
    _repair_synthetic_baseline_completion(record)
    expected = expected_for_topic(topic) if _selection_is_current(topic) else None
    previous = record.get("expected")
    fingerprint = _selection_fingerprint(topic)
    if fingerprint and previous and previous.get("selection_fingerprint") == fingerprint:
        expected = previous
    if expected:
        record["expected"] = expected
    else:
        record.pop("expected", None)
    return record, initial_baseline, expected


def _selected_files(
    topic: Topic,
    record: HistoryRecord,
    expected: dict[str, Any] | None,
    prepared_all: list[tuple[Any, str, int | None, bool]],
    preferred_season: int | None,
    *,
    content_path: str = "",
    save_path: str = "",
) -> tuple[list[tuple[tuple[Any, str, int | None], tuple[Any, ...]]], dict[str, Any] | None]:
    """The selected client files with the episodes each covers, and the expectation corrected
    by what the files show (season-relative numbering, a count from the files)."""
    file_plan = _file_rule_plan(topic, prepared_all, preferred_season, content_path=content_path, save_path=save_path)
    if file_plan is not None:
        wanted = set(file_plan.selected_indices)
        prepared_all = [
            (row, rel, size, selected and index in wanted)
            for index, (row, rel, size, selected) in enumerate(prepared_all)
        ]
        if file_plan.selected_episode_keys:
            expected = record["expected"] = {
                "kind": "episodes",
                "total": len(file_plan.selected_episode_keys),
                "keys": list(file_plan.selected_episode_keys),
                "source": "selection",
                "confidence": "exact",
                "selection_fingerprint": _selection_fingerprint(topic),
            }
        else:
            expected = None
            record.pop("expected", None)
    all_coverages = normalize_episode_seasons(
        resolve_episode_coverages(rel for _row, rel, _size, _selected in prepared_all),
        preferred_season,
        names=(rel for _row, rel, _size, _selected in prepared_all),
    )
    selected_video_keys = {
        label.key
        for (_row, rel, _size, selected), coverage in zip(prepared_all, all_coverages, strict=True)
        if selected and is_video_file(rel)
        for label in coverage
    }
    prepared = [
        (
            (row, rel, size),
            coverage if is_video_file(rel) else tuple(label for label in coverage if label.key in selected_video_keys),
        )
        for (row, rel, size, selected), coverage in zip(prepared_all, all_coverages, strict=True)
        if selected
    ]
    observed_episode_keys = sorted({entry.key for _file, coverage in prepared for entry in coverage})
    all_mode = str(as_dict(topic.get("selection")).get("mode") or "all") == "all"
    if all_mode:
        observed_episode_keys = sorted(
            {
                label.key
                for (_row, rel, _size, _selected), coverage in zip(prepared_all, all_coverages, strict=True)
                if is_video_file(rel)
                for label in coverage
            }
        )
    season_relative = _season_relative_expected(expected, observed_episode_keys, preferred_season)
    if season_relative is not None:
        expected = record["expected"] = season_relative
    seasoned_selection = _seasoned_selection_expected(expected, observed_episode_keys, preferred_season)
    if seasoned_selection is not None:
        expected = record["expected"] = seasoned_selection
    selected_episode_keys = [str(value) for value in topic.get("selected_episode_keys") or []]
    observed = _expected_from_observed(topic, expected, observed_episode_keys, selected_episode_keys)
    if observed is not None:
        expected = record["expected"] = observed
    if all_mode and observed_episode_keys and expected is not None:
        expected = record["expected"] = {**expected, "selection_fingerprint": _selection_fingerprint(topic)}
    return prepared, expected


def _track_item(
    work: _ReconcilePass, rel: str, size: int | None, coverage: tuple[Any, ...]
) -> tuple[HistoryItem, HistoryItem | None]:
    """The history item of one client file (adopted, carried over or new), and for a new item
    the completed item of an older revision whose completion it carries."""
    items, index = work.items, work.index
    label = coverage[0] if coverage else None
    display_label = _episode_keys_label({entry.key for entry in coverage}) if coverage else Path(rel).name
    base_identity = item_identity(rel, label, size)
    current_source_hash = work.source_hash.casefold()
    _adopt_previous_item(
        items,
        base_identity,
        rel=rel,
        size=size,
        label=label,
        coverage=coverage,
        current_source_hash=current_source_hash,
        current_client_paths=work.current_client_paths,
        index=index,
        orphans_by_size=work.orphans_by_size,
    )
    source_hash = work.source_hash
    identity, is_revision = _revision_identity(items, base_identity, rel, source_hash, index)
    is_new = identity not in items
    carried_completion = _carry_completion(identity, rel, size, current_source_hash, index) if is_new else None
    item = items.setdefault(
        identity,
        {
            "identity": identity,
            "kind": "episode" if label else "file",
            "label": display_label,
            "episode_key": label.key if label else None,
            "episode_keys": [item.key for item in coverage],
            "relative_path": rel,
            "client_id": work.client.client_id,
            "client_kind": work.client.client_kind,
            "source_hash": str(work.topic.get("hash") or ""),
            "size": size,
            "first_seen_at": work.now,
            "status": "seen",
            "new_after_baseline": bool(work.record.get("baseline_at")) and not work.initial_baseline,
        },
    )
    work.seen_identities.add(identity)
    item.update(
        {
            "kind": "episode" if label else "file",
            "label": display_label,
            "episode_key": label.key if label else None,
            "episode_keys": [entry.key for entry in coverage],
            "relative_path": rel,
            "source_hash": source_hash,
            "size": size,
            "superseded": False,
        }
    )
    index.add(identity)
    if carried_completion is not None and is_new:
        item["completion_carried_from"] = carried_completion.get("identity")
        if carried_completion.get("completed_observed_at"):
            item["completed_observed_at"] = carried_completion["completed_observed_at"]
    if item.get("first_seen_at") is None:
        item["first_seen_at"] = work.now
    if is_new and item.get("new_after_baseline"):
        event_name = "revision_updated" if is_revision else "new_file"
        work.events.append(event_name)
        _set_latest_event(
            work.record,
            work.event(event_name, _item_label(item), item=item.get("identity"), relative_path=rel),
        )
    return item, carried_completion


def _file_confirmed(
    work: _ReconcilePass, item: HistoryItem, rel: str, size: int | None, progress: float
) -> bool | None:
    """Is the file on disk in full? A file confirmed within the day, still complete in the
    client, is not looked at again: every 30 minutes woke sleeping drives and a NAS for nothing."""
    confirmed_ts = item.get("confirmed_ts")
    fresh = (
        item.get("status") == "completed"
        and progress >= 1.0
        and isinstance(confirmed_ts, (int, float))
        and 0 <= time.time() - float(confirmed_ts) < CONFIRMATION_REUSE_SEC
    )
    confirmed = True if fresh else filesystem_confirmation(work.evidence_save_path, rel, size, base=work.evidence_base)
    if confirmed is True and not fresh:
        item["confirmed_ts"] = int(time.time())
    return confirmed


def _update_item_status(
    work: _ReconcilePass,
    item: HistoryItem,
    row: Any,
    rel: str,
    size: int | None,
    coverage: tuple[Any, ...],
    carried_completion: HistoryItem | None,
) -> None:
    """The item's progress and status from the client and the disk; a completion seen for
    the first time (after the baseline) becomes an event."""
    progress = _file_progress(row, work.info)
    item["progress"] = progress
    if item.get("status") == "completed":
        item["completion_seen"] = True
    completed_before = bool(
        item.get("completion_seen")
        or item.get("completed_observed_at")
        or item.get("status") == "completed"
        or carried_completion is not None
    )
    confirmed = _file_confirmed(work, item, rel, size, progress)
    item.pop("verification_unavailable", None)
    if progress >= 1.0 and confirmed is True:
        item["status"] = "completed"
        item["completion_seen"] = True
        if not completed_before and not work.initial_baseline:
            _note_completion(work, item, rel, coverage)
    elif confirmed is None:
        item["status"] = "unverified"
        item["verification_unavailable"] = True
    elif progress >= 1.0:
        item["status"] = "missing"
    else:
        item["status"] = "downloading" if progress > 0 else "seen"
    completion_on = _get(work.info, "completion_on")
    if completion_on:
        item["torrent_completed_at"] = completion_on


def _note_completion(work: _ReconcilePass, item: HistoryItem, rel: str, coverage: tuple[Any, ...]) -> None:
    """A file completed in this run: an episode counts towards "episodes completed", another
    file is its own event. A completion carried over from an older revision is silent."""
    if item.get("completion_carried_from"):
        return
    item["completed_observed_at"] = work.now
    label = coverage[0] if coverage else None
    if label:
        work.observed_completed_episode_keys.update(entry.key for entry in coverage)
        return
    work.events.append("file_completed")
    work.completed_file_labels.append(str(item.get("label") or t("progress.file", owner_language())))
    _set_latest_event(
        work.record,
        work.event("file_completed", _item_label(item), item=item.get("identity"), relative_path=rel),
    )


def _note_completed_episodes(work: _ReconcilePass) -> str:
    """The episodes completed in this run, as one event; returns their label ("" for none)."""
    newly_completed = work.observed_completed_episode_keys.intersection(_completed_episode_keys(work.items))
    if not newly_completed or work.initial_baseline:
        return ""
    work.events.append("episode_completed")
    message = _episode_keys_label_msg(newly_completed)
    _set_latest_event(work.record, work.event("episode_completed", message))
    return str(message)


def reconcile_topic(
    topic: Topic,
    client_adapter: TorrentClientAdapter,
    history: DownloadHistory,
    now: str,
) -> dict[str, Any]:
    """Look at the topic's torrent in its client and its files on disk; record in ``history``
    what changed (new files, completions, the torrent gone or back) and return the events."""
    record, initial_baseline, expected = _open_record(topic, history)
    events: list[str] = []
    if (client_adapter.capabilities or {}).get("inspect") is False:
        record["last_scan_at"] = now
        return {"summary": _summary_record(record, expected), "events": events}

    info = client_adapter.inspect_torrent(str(topic.get("hash") or ""))
    record["last_scan_at"] = now
    if not _track_client_presence(record, info, client_adapter, now, events):
        return {"summary": _summary_record(record, expected), "events": events}
    client_event = _client_event(info, client_adapter)

    files = _get(info, "files", []) or []
    if files and initial_baseline:
        record["baseline_at"] = now
    items = record["items"]
    current_source_hash = str(topic.get("hash") or "").casefold()
    _mark_superseded(items, current_source_hash, has_files=bool(files))
    evidence_save_path = _check_save_path(topic, info)
    try:
        evidence_base: Path | None = Path(evidence_save_path).resolve()  # once, not 4x per file (F4)
    except OSError, ValueError:
        evidence_base = None
    selected_episode_keys = [str(value) for value in topic.get("selected_episode_keys") or []]
    preferred_season = _preferred_season(topic, selected_episode_keys)
    prepared_all = _client_files(files, evidence_save_path, evidence_base)
    prepared, expected = _selected_files(
        topic,
        record,
        expected,
        prepared_all,
        preferred_season,
        content_path=str(_get(info, "content_path", "") or ""),
        save_path=evidence_save_path,
    )
    work = _ReconcilePass(
        topic=topic,
        client=client_adapter,
        record=record,
        items=items,
        info=info,
        now=now,
        initial_baseline=initial_baseline,
        evidence_save_path=evidence_save_path,
        evidence_base=evidence_base,
        current_client_paths={rel.casefold() for _row, rel, _size, _selected in prepared_all},
        events=events,
    )
    for (row, rel, size), coverage in prepared:
        item, carried_completion = _track_item(work, rel, size, coverage)
        _update_item_status(work, item, row, rel, size, coverage, carried_completion)

    if files:
        for identity, historical_item in items.items():
            if (
                str(historical_item.get("source_hash") or "").casefold() == current_source_hash
                and identity not in work.seen_identities
            ):
                historical_item["superseded"] = True

    completed_episode_label = _note_completed_episodes(work)
    if client_event:
        _set_label(
            client_event, _latest_item_label(record, prefer_completed=client_event.get("kind") != "client_added")
        )
        _set_latest_event(record, client_event)
    _repair_last_event_semantics(record)
    summary = _summary_record(record, expected)
    labels = work.completed_file_labels
    return {
        "summary": summary,
        "events": list(dict.fromkeys(events)),
        "completed_episode_label": completed_episode_label,
        "completed_file_label": (labels[0] if len(labels) == 1 else t("progress.files", owner_language()))
        if labels
        else "",
    }


def progress_summary(topic_id: str, history: Mapping[str, Any]) -> dict[str, Any]:
    record = (history.get("topics") or {}).get(str(topic_id)) or {}
    return summarize_completion(record.get("items") or {}, record.get("expected"))
