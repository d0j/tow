"""Expected files: which client files the selection takes, the episodes they cover, the
selection's file rules and aliases, and how many episodes a topic should have."""

from __future__ import annotations

import hashlib
import json
import re
from typing import Any

from tow.clients.files import map_files, normalize_path
from tow.episodes import (
    expected_for_topic,
    is_video_file,
    normalize_episode_seasons,
    parse_season_hint,
    resolve_episode_coverages,
)
from tow.folders import paths_equal
from tow.jsonish import as_dict
from tow.records import HistoryRecord, Topic
from tow.selection import (
    SelectionError,
    SelectionPlan,
    normalize_exact_files,
    normalize_file_aliases,
    policy_from_topic,
    resolve_selection,
)
from tow.torrent import TorrentFile


def preferred_season_for(topic: Topic, selected_episode_keys: list[str]) -> int | None:
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


def _season_relative_expected(
    expected: dict[str, Any] | None, observed_episode_keys: list[str], preferred_season: int | None
) -> dict[str, Any] | None:
    """A title range like "13-24 из 24" or "73-176 серии из 176" whose files do not number
    from its first episode: S0xE01..E12 of one season are that season's 1..12 (season-relative);
    files that mostly lie below the range restart numbering (each season from E01, or the
    subset from 01), so the range cannot be mapped onto them and its length is counted instead
    (restarted). Return the corrected expectation, else None: the files fit the range, or most
    of them do (a stray recap E01 in E73..E176 must not complete a missing E176)."""
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
    episodes = [int(match.group(1)) for key in observed_episode_keys if (match := re.search(r"e(\d+)$", key))]
    below = sum(episode < int(expected["episode_min"]) for episode in episodes)
    if below * 2 > len(episodes):
        return {**expected, "numbering": "restarted"}
    return None


def selection_is_current(topic: Topic) -> bool:
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


def _current_file_aliases(topic: Topic) -> dict[str, Any]:
    aliases = normalize_file_aliases(topic["file_aliases"])
    if aliases["hash"] != str(topic.get("hash") or "").casefold():
        raise SelectionError("selection.file_map_changed")
    return aliases


def _legacy_alias_paths(topic: Topic) -> list[str] | None:
    """Only a complete, explicitly verified old selection is an original-name source."""
    if topic.get("selection_verified") is not True or not topic.get("selection_hash") or "selected_files" not in topic:
        return None
    paths = topic["selected_files"]
    if (
        not isinstance(paths, list)
        or not paths
        or not all(isinstance(path, str) for path in paths)
        or topic.get("selected_files_truncated")
        or type(topic.get("selected_file_count")) is not int
        or topic.get("selected_file_count") != len(paths)
    ):
        raise SelectionError("selection.file_map_changed")
    return paths


def _legacy_mask_rows(
    paths: list[str],
    native_rows: tuple[TorrentFile, ...],
    prepared_all: list[tuple[Any, str, int | None, bool]],
    root: str,
) -> tuple[TorrentFile, ...]:
    lookup: dict[str, list[TorrentFile]] = {}
    for row in native_rows:
        if prepared_all[row.index][2] is not None:
            lookup.setdefault(normalize_path(row.path), []).append(row)
    sources = []
    for path in paths:
        candidates = {
            row.index: row
            for spelling in {normalize_path(path), normalize_path(f"{root}/{path}")}
            for row in lookup.get(spelling, [])
        }
        if len(candidates) != 1:
            raise SelectionError("selection.file_map_changed")
        row = next(iter(candidates.values()))
        sources.append(TorrentFile(row.index, path, row.size))
    # Validate the old names as literal paths, without changing or persisting them.
    try:
        normalize_exact_files([{"path": row.path, "size": row.size} for row in sources])
    except SelectionError as exc:
        raise SelectionError("selection.file_map_changed") from exc
    return tuple(sources)


def _file_rule_evidence(
    topic: Topic,
    prepared_all: list[tuple[Any, str, int | None, bool]],
    preferred_season: int | None,
    *,
    content_path: str = "",
    save_path: str = "",
) -> tuple[SelectionPlan | None, dict[int, str]]:
    """Canonical rule and episode names, separate from native disk/history paths."""
    mode = str(as_dict(topic.get("selection")).get("mode") or "all")
    if not prepared_all or not selection_is_current(topic):
        return None, {}
    if mode not in {"files", "exact"} and (mode not in {"all", "episodes"} or "file_aliases" not in topic):
        return None, {}
    root = _client_content_root(content_path, save_path)
    native_rows = tuple(
        TorrentFile(index, rel, size if size is not None else 0)
        for index, (_row, rel, size, _selected) in enumerate(prepared_all)
        if (mode != "exact" or size is not None) and (size is None or 0 <= size <= 2**63 - 1)
    )
    policy = policy_from_topic(topic)
    canonical: dict[int, str] = {}
    if mode in {"all", "episodes", "files"} and "file_aliases" in topic:
        aliases = _current_file_aliases(topic)
        sources = tuple(TorrentFile(index, item["path"], item["size"]) for index, item in enumerate(aliases["files"]))
        mapping = map_files(
            sources,
            [{"index": row.index, "name": row.path, "size": prepared_all[row.index][2]} for row in native_rows],
            root,
            fail=lambda _path: SelectionError("selection.file_map_changed"),
        )
        canonical = {mapping[row.index]: row.path for row in sources}
    elif mode == "files" and (legacy_paths := _legacy_alias_paths(topic)) is not None:
        rows = _legacy_mask_rows(legacy_paths, native_rows, prepared_all, root)
        return resolve_selection(rows, policy, preferred_season=preferred_season), {row.index: row.path for row in rows}
    if mode not in {"files", "exact"}:
        return None, canonical
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
    return resolve_selection(rows, policy, preferred_season=preferred_season), {row.index: row.path for row in rows}


def selection_fingerprint(topic: Topic) -> str | None:
    mode = str(as_dict(topic.get("selection")).get("mode") or "all")
    if mode not in {"all", "files", "exact"} or not selection_is_current(topic):
        return None
    keys = [str(value) for value in topic.get("selected_episode_keys") or []]
    context: list[Any] = [
        str(topic.get("hash") or "").casefold(),
        str(topic.get("client_id") or ""),
        policy_from_topic(topic),
        preferred_season_for(topic, keys),
    ]
    if mode == "all":
        context.append(expected_for_topic(topic))
    if mode in {"all", "files"}:
        if "file_aliases" in topic:
            context.append(_current_file_aliases(topic))
        elif mode == "files" and (legacy_paths := _legacy_alias_paths(topic)) is not None:
            context.append(legacy_paths)
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
        not selection_is_current(topic) or (mode == "episodes" and not selected_episode_keys)
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


def selected_files(
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
    file_plan, canonical_paths = _file_rule_evidence(
        topic, prepared_all, preferred_season, content_path=content_path, save_path=save_path
    )
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
                "selection_fingerprint": selection_fingerprint(topic),
            }
        else:
            expected = None
            record.pop("expected", None)
    names = tuple(canonical_paths.get(index, rel) for index, (_row, rel, _size, _selected) in enumerate(prepared_all))
    all_coverages = normalize_episode_seasons(resolve_episode_coverages(names), preferred_season, names=names)
    selected_video_keys = {
        label.key
        for (_row, _rel, _size, selected), name, coverage in zip(prepared_all, names, all_coverages, strict=True)
        if selected and is_video_file(name)
        for label in coverage
    }
    prepared = [
        (
            (row, rel, size),
            coverage if is_video_file(name) else tuple(label for label in coverage if label.key in selected_video_keys),
        )
        for (row, rel, size, selected), name, coverage in zip(prepared_all, names, all_coverages, strict=True)
        if selected
    ]
    observed_episode_keys = sorted({entry.key for _file, coverage in prepared for entry in coverage})
    all_mode = str(as_dict(topic.get("selection")).get("mode") or "all") == "all"
    if all_mode:
        observed_episode_keys = sorted(
            {
                label.key
                for (_row, _rel, _size, _selected), name, coverage in zip(
                    prepared_all, names, all_coverages, strict=True
                )
                if is_video_file(name)
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
        expected = record["expected"] = {**expected, "selection_fingerprint": selection_fingerprint(topic)}
    return prepared, expected
