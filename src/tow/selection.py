from __future__ import annotations

import fnmatch
import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass
from pathlib import PurePosixPath
from typing import Any

from tow.episodes import (
    EpisodeLabel,
    is_video_file,
    normalize_episode_seasons,
    resolve_episode_coverages,
)
from tow.errors import TowError
from tow.torrent import TorrentFile

MODES = frozenset({"all", "episodes", "files"})
TRACKING_MODES = frozenset({"watch", "once"})


class SelectionError(TowError, ValueError):
    """A file selection that cannot be used (its text: ``selection.*`` in the language files)."""


class SelectionPendingError(SelectionError):
    """Every selected episode is still ahead of what the torrent has: not out yet."""


MAX_RULE_TEXT = 8192
MAX_RULES = 500


@dataclass(frozen=True, slots=True)
class SelectionPlan:
    mode: str
    expression: str
    selected_indices: tuple[int, ...]
    selected_files: tuple[str, ...]
    selected_episode_keys: tuple[str, ...]
    total_files: int
    ignored_pad_files: int

    def as_dict(self) -> dict[str, Any]:
        preview_limit = 200
        return {
            "mode": self.mode,
            "expression": self.expression,
            "selected_indices": list(self.selected_indices),
            "selected_files": list(self.selected_files[:preview_limit]),
            "selected_files_truncated": len(self.selected_files) > preview_limit,
            "selected_episode_keys": list(self.selected_episode_keys),
            "selected_file_count": len(self.selected_indices),
            "total_files": self.total_files,
            "ignored_pad_files": self.ignored_pad_files,
        }


def normalize_policy(mode: object = "all", expression: object = "", tracking_mode: object = "watch") -> dict[str, str]:
    normalized_mode = str(mode or "all").strip().lower()
    lifecycle = str(tracking_mode or "watch").strip().lower()
    value = str(expression or "").strip()
    if normalized_mode not in MODES:
        raise SelectionError("selection.unknown_mode")
    if lifecycle not in TRACKING_MODES:
        raise SelectionError("selection.unknown_tracking")
    if len(value) > MAX_RULE_TEXT:
        raise SelectionError("selection.too_long", limit=MAX_RULE_TEXT)
    if normalized_mode == "all":
        value = ""
    elif not value:
        raise SelectionError("selection.empty")
    if normalized_mode == "episodes":
        rules = _split_rules(value)
        if "*" in rules and len(rules) != 1:
            raise SelectionError("selection.star_alone")
        for rule in rules:
            _episode_range(rule)
    elif normalized_mode == "files":
        _safe_globs(value)
    return {"mode": normalized_mode, "value": value, "tracking_mode": lifecycle}


def policy_from_topic(topic: Mapping[str, Any]) -> dict[str, str]:
    raw = topic.get("selection")
    if not isinstance(raw, dict):
        raw = {}
    return normalize_policy(raw.get("mode", "all"), raw.get("value", ""), topic.get("tracking_mode", "watch"))


def _split_rules(value: str) -> list[str]:
    rules = [part.strip() for part in re.split(r"[,;\n]+", value) if part.strip()]
    if not rules or len(rules) > MAX_RULES:
        raise SelectionError("selection.rule_count", limit=MAX_RULES)
    return rules


def _episode_range(token: str) -> tuple[EpisodeLabel, ...] | None:
    compact = re.sub(r"\s+", "", token).replace("–", "-").replace("—", "-")
    if compact == "*":
        return None
    match = re.fullmatch(r"(?i)s(\d{1,2})e(\d{1,4})(?:-(?:s(\d{1,2}))?e?(\d{1,4}))?", compact)
    if not match:
        match = re.fullmatch(r"(?i)(\d{1,2})[xх](\d{1,4})(?:-(?:(\d{1,2})[xх])?(\d{1,4}))?", compact)
    if match:
        season = int(match.group(1))
        first = int(match.group(2))
        end_season = int(match.group(3) or season)
        last = int(match.group(4) or first)
        if season <= 0 or first <= 0 or end_season != season or last < first or last - first > 1000:
            raise SelectionError("selection.bad_range", rule=token)
        return tuple(EpisodeLabel(episode=n, season=season) for n in range(first, last + 1))
    match = re.fullmatch(r"(?i)(?:e|ep)?(\d{1,4})(?:-(?:e|ep)?(\d{1,4}))?", compact)
    if match:
        first, last = int(match.group(1)), int(match.group(2) or match.group(1))
        if first <= 0 or last < first or last - first > 1000:
            raise SelectionError("selection.bad_range", rule=token)
        return tuple(EpisodeLabel(episode=n) for n in range(first, last + 1))
    raise SelectionError("selection.bad_episode", rule=token)


def _resolve_episode_keys(value: str, files: Iterable[TorrentFile], preferred_season: int | None = None) -> set[str]:
    rules = _split_rules(value)
    available: dict[str, EpisodeLabel] = {}
    rows = tuple(files)
    coverages = normalize_episode_seasons(
        resolve_episode_coverages(file.path for file in rows),
        preferred_season,
        names=(file.path for file in rows),
    )
    seasons = {label.season for coverage in coverages for label in coverage if label.season is not None}
    for row, coverage in zip(rows, coverages, strict=True):
        if not is_video_file(row.path):
            continue
        for label in coverage:
            available[label.key] = label
    if not available:
        raise SelectionError("selection.no_episodes")
    if "*" in rules:
        if len(rules) != 1:
            raise SelectionError("selection.star_alone")
        return set(available)
    wanted: set[str] = set()
    requested: list[str] = []
    requested_labels: list[EpisodeLabel] = []
    for rule in rules:
        labels = _episode_range(rule)
        assert labels is not None
        requested_labels.extend(labels)
        for label in labels:
            if label.season is None:
                candidates = [item for item in available.values() if item.episode == label.episode]
                candidate_seasons = {item.season for item in candidates}
                if len(candidate_seasons) > 1 or (len(seasons) > 1 and not candidates):
                    raise SelectionError("selection.ambiguous", episode=label.episode)
                wanted.update(item.key for item in candidates)
                requested.append(str(label.episode))
            else:
                # Same rule as bare numbers: episodes of the range that are not out yet
                # are simply not selected (watch topics grow into the range).
                if label.key in available:
                    wanted.add(label.key)
                requested.append(label.key)
    if not wanted:
        if all(_is_ahead(label, available.values()) for label in requested_labels):
            raise SelectionPendingError("selection.not_out_yet", episodes=", ".join(requested[:10]))
        raise SelectionError("selection.absent", episodes=", ".join(requested[:10]))
    return wanted


def _is_ahead(label: EpisodeLabel, available: Iterable[EpisodeLabel]) -> bool:
    """``label`` comes after everything the torrent has (a later episode or season)."""
    rows = tuple(available)
    same_season = [item.episode for item in rows if label.season is None or item.season == label.season]
    if same_season:
        return label.episode > max(same_season)
    return label.season is not None and all(item.season is not None and item.season < label.season for item in rows)


def _safe_globs(value: str) -> list[str]:
    globs = _split_rules(value)
    for pattern in globs:
        normalized = pattern.replace("\\", "/")
        if normalized.startswith(("/", "~")) or ":" in normalized or "\x00" in normalized:
            raise SelectionError("selection.unsafe_mask", mask=pattern)
        if any(part in {"", ".", ".."} for part in normalized.split("/")):
            raise SelectionError("selection.unsafe_mask", mask=pattern)
    return [pattern.replace("\\", "/").casefold() for pattern in globs]


def resolve_selection(
    files: Iterable[TorrentFile],
    policy: dict[str, str],
    *,
    preferred_season: int | None = None,
) -> SelectionPlan:
    rows = tuple(files)
    selectable = tuple(row for row in rows if not row.is_pad)
    mode = policy["mode"]
    expression = policy.get("value", "")
    selected: list[TorrentFile]
    episode_keys: set[str] = set()
    coverage_by_index = dict(
        zip(
            (row.index for row in selectable),
            normalize_episode_seasons(
                resolve_episode_coverages(row.path for row in selectable),
                preferred_season,
                names=(row.path for row in selectable),
            ),
            strict=True,
        )
    )

    def labels(row: TorrentFile) -> tuple[EpisodeLabel, ...]:
        return coverage_by_index.get(row.index, ())

    if mode == "all":
        selected = list(selectable)
        for row in selected:
            if is_video_file(row.path):
                episode_keys.update(label.key for label in labels(row))
    elif mode == "episodes":
        episode_keys = _resolve_episode_keys(expression, selectable, preferred_season)
        selected = [row for row in selectable if episode_keys.intersection(label.key for label in labels(row))]
    elif mode == "files":
        patterns = _safe_globs(expression)
        selected = []
        for row in selectable:
            path = row.path.casefold()
            base = PurePosixPath(path).name
            if any(fnmatch.fnmatchcase(path, pattern) or fnmatch.fnmatchcase(base, pattern) for pattern in patterns):
                selected.append(row)
        selected_coverages = normalize_episode_seasons(
            (labels(row) for row in selected),
            preferred_season,
            names=(row.path for row in selected),
        )
        coverage_by_index.update(
            {row.index: coverage for row, coverage in zip(selected, selected_coverages, strict=True)}
        )
        for row in selected:
            if is_video_file(row.path):
                episode_keys.update(label.key for label in labels(row))
    else:
        raise SelectionError("selection.unknown_mode")
    if not selected:
        raise SelectionError("selection.nothing_matched")
    return SelectionPlan(
        mode=mode,
        expression=expression,
        selected_indices=tuple(row.index for row in selected),
        selected_files=tuple(row.path for row in selected),
        selected_episode_keys=tuple(sorted(episode_keys)),
        total_files=len(selectable),
        ignored_pad_files=len(rows) - len(selectable),
    )
