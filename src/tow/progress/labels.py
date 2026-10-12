"""Events and labels: the latest event of a history record and the wording of its items."""

from __future__ import annotations

import re
from typing import Any

from tow.clock import parse_timestamp
from tow.episodes import parse_episode_label
from tow.errors import Msg
from tow.records import HistoryItem, HistoryRecord

# Events of one pass share its time: which one Home shows as the latest. A file that is new
# in this revision (the new episode) outranks the files the revision only carried over.
_EVENT_RANK = {"episode_completed": 3, "file_completed": 2, "new_file": 1, "revision_updated": 0}


def _event_order(event: dict[str, Any]) -> tuple[int, int, int]:
    """An event's place among events of the same moment: its kind, then its episode."""
    label = parse_episode_label(str(event.get("label") or ""))
    return (
        _EVENT_RANK.get(str(event.get("kind")), 0),
        label.season if label and label.season is not None else -1,
        label.episode if label else -1,
    )


def set_latest_event(record: HistoryRecord, event: dict[str, Any]) -> None:
    current = record.get("last_event")
    if not isinstance(current, dict) or not current.get("at"):
        record["last_event"] = event
        return
    try:
        event_time = parse_timestamp(str(event["at"]))
        current_time = parse_timestamp(str(current["at"]))
        is_newer = event_time > current_time or (
            event_time == current_time and _event_order(event) > _event_order(current)
        )
    except TypeError, ValueError, OverflowError, OSError:
        is_newer = True
    if is_newer:
        record["last_event"] = event


def label_fields(value: Msg | str | None) -> dict[str, Any]:
    """An event's label: its text (for older readers) and, when it is TOW's own wording, its
    catalog key and values - the History and Home render it in the reader's language."""
    if isinstance(value, Msg):
        record = value.record()
        return {"label": value.text(), "label_code": record["code"], "label_params": record["params"]}
    return {"label": value}


def set_label(event: dict[str, Any], value: Msg | str | None) -> None:
    event.pop("label_code", None)
    event.pop("label_params", None)
    event.update(label_fields(value))


def item_label(item: HistoryItem) -> Msg | str:
    """An item's label; an episode's comes again from its keys (so it is TOW's wording, typed)."""
    keys = {str(key) for key in item.get("episode_keys") or [] if key}
    if not keys and item.get("episode_key"):
        keys = {str(item["episode_key"])}
    label = episode_keys_label_msg(keys) if keys else ""
    return label or str(item.get("label") or item.get("identity") or "") or Msg("progress.file")


def latest_item_label(record: HistoryRecord, *, prefer_completed: bool = True) -> Msg | str:
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

        return item_label(max(episode_candidates, key=episode_key))

    labels = {str(item.get("label") or item.get("identity") or "") for item in candidates}
    if len(labels) == 1:
        return next(iter(labels)) or Msg("progress.file")
    return Msg("progress.files")


def episode_keys_label(keys: set[str]) -> str:
    """The label of these episodes in the current language ("S01E05", "Episodes 05–07")."""
    return str(episode_keys_label_msg(keys))


def episode_keys_label_msg(keys: set[str]) -> Msg | str:
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


def repair_last_event_semantics(record: HistoryRecord) -> None:
    """Normalize legacy event labels after file names gain episode parsing."""
    event = record.get("last_event")
    if not isinstance(event, dict):
        return
    if event.get("kind") in {"client_added", "torrent_completed"}:
        set_label(event, latest_item_label(record, prefer_completed=event.get("kind") != "client_added"))
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
            set_label(event, episode_keys_label_msg(keys))
        elif completed and all(item.get("kind") == "file" for item in completed.values()):
            # A formerly misclassified special now has file evidence for the original
            # timestamp. Repair its wording silently, without inventing a new completion.
            event["kind"] = "file_completed"
            set_label(event, latest_item_label({"items": completed}))
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
    set_label(event, episode_keys_label_msg(keys) or str(item.get("label") or "") or Msg("progress.episode_fallback"))
