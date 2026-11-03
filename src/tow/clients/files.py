"""Matching a torrent's files to the files a client lists - shared by every client adapter.

A client may show ``Show/e01.mkv`` or ``e01.mkv``, may store ``a: b`` as ``a_ b`` on Windows,
and lists BEP 47 padding files the torrent marks as such. Files are matched by path AND size,
never by position: a client that reorders its list must not get the wrong file selected.
"""

from __future__ import annotations

import hashlib
import json
import ntpath
import posixpath
from collections.abc import Callable, Mapping
from pathlib import PurePosixPath, PureWindowsPath
from typing import Any

from tow.folders import is_windows_device_path, windows_shaped
from tow.torrent import TorrentFile, windows_path_key


def recovery_fingerprint(rows: list[Any]) -> str:
    """Exact native identities and priorities for a repair; cached progress grants no permission.

    Reordering is harmless, but a missing or ambiguous index, name, size or priority cannot
    authorize a client change. Includes skipped files, padding and every native priority level.
    """
    if not isinstance(rows, list) or not rows:
        raise ValueError("invalid client file snapshot")
    values: list[tuple[int, str, int, int]] = []
    indices = set()
    for row in rows:
        if not isinstance(row, Mapping) or not row:
            raise ValueError("invalid client file snapshot")
        index, name, size, priority = (row.get(key) for key in ("index", "name", "size", "priority"))
        if (
            type(index) is not int
            or index < 0
            or index in indices
            or not isinstance(name, str)
            or not name
            or "\0" in name
            or type(size) is not int
            or size < 0
            or type(priority) is not int
            or priority < 0
        ):
            raise ValueError("invalid client file snapshot")
        indices.add(index)
        values.append((index, name, size, priority))
    canonical = json.dumps(sorted(values), ensure_ascii=False, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _recovery_base(folder: str) -> PureWindowsPath | PurePosixPath:
    if not isinstance(folder, str) or not folder.strip() or is_windows_device_path(folder):
        raise ValueError("invalid client save folder")
    base = (
        PureWindowsPath(ntpath.normpath(folder))
        if windows_shaped(folder)
        else PurePosixPath(posixpath.normpath(folder))
    )
    if not base.is_absolute():
        raise ValueError("invalid client save folder")
    return base


def recovery_folders_overlap(first: str, second: str) -> bool:
    """Whether two absolute client folders can contain one shared file, without disk I/O."""
    a, b = _recovery_base(first), _recovery_base(second)
    return a.is_relative_to(b) or b.is_relative_to(a)


def recovery_paths(folder: str, rows: list[Any]) -> set[str]:
    """Absolute client paths of selected files; different root layouts compare correctly."""
    base = _recovery_base(folder)
    recovery_fingerprint(rows)
    wanted = set()
    for row in rows:
        if row["priority"] == 0:
            continue
        raw = row["name"].replace("\\", "/")
        parts = [part for part in raw.split("/") if part not in {"", "."}]
        if (
            not parts
            or raw.startswith("/")
            or PureWindowsPath(raw).drive
            or ".." in parts
            or (isinstance(base, PureWindowsPath) and any(":" in part for part in parts))
        ):
            raise ValueError("invalid client file path")
        absolute = str(base.joinpath(*parts))
        wanted.add(ntpath.normcase(absolute) if isinstance(base, PureWindowsPath) else absolute)
    return wanted


def normalize_path(value: object) -> str:
    """The comparable spelling of a path inside a torrent (Windows-sanitized, forward slashes)."""
    return windows_path_key(str(value or ""))


def padding_like(value: object) -> bool:
    return any(part == ".pad" or part.startswith("_____padding_file_") for part in normalize_path(value).split("/"))


def priorities(rows: list[dict[str, Any]], *, fail: Callable[[], Exception]) -> dict[int, int]:
    """A complete, unambiguous priority snapshot; missing flags are not skipped files."""
    result: dict[int, int] = {}
    for row in rows:
        index, priority = row.get("index"), row.get("priority")
        if type(index) is not int or index < 0 or index in result or type(priority) is not int or priority < 0:
            raise fail()
        result[index] = priority
    return result


def verify_selection(
    before: list[dict[str, Any]],
    after: list[dict[str, Any]],
    wanted: set[int],
    *,
    fail: Callable[[], Exception],
    ignored: set[int] | None = None,
) -> None:
    """Confirm every file identity and flag, even files the new selection skips."""
    initial = priorities(before, fail=fail)
    current = priorities(after, fail=fail)
    if initial.keys() != current.keys() or not wanted.issubset(initial):
        raise fail()

    def identities(rows: list[dict[str, Any]]) -> dict[int, tuple[str, Any]]:
        return {row["index"]: (normalize_path(row.get("name")), row.get("size")) for row in rows}

    if identities(before) != identities(after):
        raise fail()
    ignored_ids = ignored or set()
    for index, priority in current.items():
        if index not in ignored_ids and (priority > 0) != (index in wanted):
            raise fail()


def verify_identities(
    before: list[dict[str, Any]], after: list[dict[str, Any]], *, fail: Callable[[], Exception]
) -> None:
    """Confirm every file identity (index, path, size) whatever its flag: for a selection that
    is not applied yet."""

    def identities(rows: list[dict[str, Any]]) -> dict[int, tuple[str, Any]]:
        result: dict[int, tuple[str, Any]] = {}
        for row in rows:
            index = row.get("index")
            if type(index) is not int or index < 0 or index in result:
                raise fail()
            result[index] = (normalize_path(row.get("name")), row.get("size"))
        return result

    if not before or identities(before) != identities(after):
        raise fail()


def map_files(
    source_files: tuple[TorrentFile, ...],
    client_files: list[dict[str, Any]],
    root_name: str,
    *,
    fail: Callable[[str], Exception],
) -> dict[int, int]:
    """Torrent file index -> client file index; ``fail(path)`` builds the error for a file that
    has no single match."""
    lookup: dict[tuple[str, int | None], list[int]] = {}
    for row in client_files:
        index = row.get("index")
        if type(index) is not int or index < 0:
            continue
        size = row.get("size")
        lookup.setdefault((normalize_path(row.get("name")), int(size) if size is not None else None), []).append(index)
    root = normalize_path(root_name)
    mapping: dict[int, int] = {}
    used: set[int] = set()
    for source in source_files:
        if source.is_pad:
            continue
        wanted = normalize_path(source.path)
        candidates = list(
            dict.fromkeys(
                index
                for path in (wanted, f"{root}/{wanted}")
                for index in lookup.get((path, source.size), [])
                if index not in used
            )
        )
        if len(candidates) != 1:
            raise fail(source.path)
        mapping[source.index] = candidates[0]
        used.add(candidates[0])
    return mapping
