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
from tow.torrent import MAX_FILES, TorrentFile, validate_relative_path, windows_path_key

MODES = frozenset({"all", "episodes", "files", "exact"})
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


def normalize_policy(
    mode: object = "all",
    expression: object = "",
    tracking_mode: object = "watch",
    *,
    files: object = None,
    source_hash: object = None,
) -> dict[str, Any]:
    normalized_mode = str(mode or "all").strip().lower()
    lifecycle = str(tracking_mode or "watch").strip().lower()
    value = str(expression or "").strip()
    if normalized_mode not in MODES:
        raise SelectionError("selection.unknown_mode")
    if lifecycle not in TRACKING_MODES:
        raise SelectionError("selection.unknown_tracking")
    if len(value) > MAX_RULE_TEXT:
        raise SelectionError("selection.too_long", limit=MAX_RULE_TEXT)
    if normalized_mode in {"all", "exact"}:
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
    policy: dict[str, Any] = {"mode": normalized_mode, "value": value, "tracking_mode": lifecycle}
    if normalized_mode == "exact":
        policy["files"] = normalize_exact_files(files)
        if not isinstance(source_hash, str) or not re.fullmatch(r"[0-9a-fA-F]{40}|[0-9a-fA-F]{64}", source_hash):
            raise SelectionError("selection.exact_invalid")
        policy["source_hash"] = source_hash.upper()
    return policy


def normalize_exact_files(value: object) -> list[dict[str, Any]]:
    """Literal metadata paths and sizes, never patterns or client indices."""
    if not isinstance(value, list) or not 1 <= len(value) <= MAX_FILES:
        raise SelectionError("selection.exact_invalid")
    result: list[dict[str, Any]] = []
    seen: set[str] = set()
    for item in value:
        if not isinstance(item, dict) or set(item) != {"path", "size"}:
            raise SelectionError("selection.exact_invalid")
        path, size = item["path"], item["size"]
        if not isinstance(path, str) or type(size) is not int or not 0 <= size <= 2**63 - 1:
            raise SelectionError("selection.exact_invalid")
        try:
            normalized = validate_relative_path(path)
        except (ValueError, UnicodeError) as exc:
            raise SelectionError("selection.exact_invalid") from exc
        key = windows_path_key(normalized)
        if normalized != path or key in seen:
            raise SelectionError("selection.exact_invalid")
        seen.add(key)
        result.append({"path": path, "size": size})
    return sorted(result, key=lambda row: row["path"])


def stored_policy(policy: Mapping[str, Any]) -> dict[str, Any]:
    """The durable selection only; lifecycle remains the topic's separate setting."""
    result = {"mode": policy["mode"], "value": policy["value"]}
    if policy["mode"] == "exact":
        result.update(files=policy["files"], source_hash=policy["source_hash"])
    return result


def policy_from_topic(topic: Mapping[str, Any]) -> dict[str, Any]:
    raw = topic.get("selection")
    if not isinstance(raw, dict):
        raw = {}
    return normalize_policy(
        raw.get("mode", "all"),
        raw.get("value", ""),
        topic.get("tracking_mode", "watch"),
        files=raw.get("files"),
        source_hash=raw.get("source_hash"),
    )


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
    by_episode: dict[int, set[str]] = {}
    last_by_season: dict[int | None, int] = {}
    for label in available.values():
        by_episode.setdefault(label.episode, set()).add(label.key)
        last_by_season[label.season] = max(label.episode, last_by_season.get(label.season, 0))
    last_episode = max(last_by_season.values())
    last_season = max(season for season in last_by_season if season is not None) if None not in last_by_season else None
    wanted: set[str] = set()
    requested: list[str] = []
    seen: set[EpisodeLabel] = set()
    all_ahead = True
    for rule in rules:
        labels = _episode_range(rule)
        assert labels is not None
        for label in labels:
            # Keep the original first ten diagnostic labels, including repeats,
            # without retaining a string and object for every expanded request.
            if len(requested) < 10:
                requested.append(
                    str(label.episode) if label.season is None else f"S{label.season:02d}E{label.episode:02d}"
                )
            if label in seen:
                continue
            seen.add(label)
            wanted.update(_matching_episode_keys(label, available, by_episode, len(seasons) > 1))
            all_ahead = all_ahead and _is_ahead(label, last_by_season, last_episode, last_season)
    if not wanted:
        if all_ahead:
            raise SelectionPendingError("selection.not_out_yet", episodes=", ".join(requested))
        raise SelectionError("selection.absent", episodes=", ".join(requested))
    return wanted


def _matching_episode_keys(
    label: EpisodeLabel,
    available: Mapping[str, EpisodeLabel],
    by_episode: Mapping[int, set[str]],
    multiple_seasons: bool,
) -> set[str]:
    if label.season is not None:
        return {label.key} if label.key in available else set()
    candidates = by_episode.get(label.episode, set())
    if len(candidates) > 1 or (multiple_seasons and not candidates):
        raise SelectionError("selection.ambiguous", episode=label.episode)
    return candidates


def _is_ahead(
    label: EpisodeLabel,
    last_by_season: Mapping[int | None, int],
    last_episode: int,
    last_season: int | None,
) -> bool:
    """``label`` comes after everything the torrent has (a later episode or season)."""
    if label.season is None:
        return label.episode > last_episode
    if label.season in last_by_season:
        return label.episode > last_by_season[label.season]
    return last_season is not None and label.season > last_season


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
    policy: dict[str, Any],
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
    elif mode == "exact":
        wanted = normalize_exact_files(policy.get("files"))
        available = {(row.path, row.size): row for row in selectable}
        if any((item["path"], item["size"]) not in available for item in wanted):
            raise SelectionError("selection.exact_changed")
        identities = {(item["path"], item["size"]) for item in wanted}
        selected = [row for row in selectable if (row.path, row.size) in identities]
        for row in selected:
            if is_video_file(row.path):
                episode_keys.update(label.key for label in labels(row))
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
