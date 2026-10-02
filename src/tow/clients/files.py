"""Matching a torrent's files to the files a client lists - shared by every client adapter.

A client may show ``Show/e01.mkv`` or ``e01.mkv``, may store ``a: b`` as ``a_ b`` on Windows,
and lists BEP 47 padding files the torrent marks as such. Files are matched by path AND size,
never by position: a client that reorders its list must not get the wrong file selected.
"""

from __future__ import annotations

from collections.abc import Callable
from typing import Any

from tow.torrent import TorrentFile, windows_path_key


def normalize_path(value: object) -> str:
    """The comparable spelling of a path inside a torrent (Windows-sanitized, forward slashes)."""
    return windows_path_key(str(value or ""))


def padding_like(value: object) -> bool:
    return any(part == ".pad" or part.startswith("_____padding_file_") for part in normalize_path(value).split("/"))


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
        if not isinstance(index, int):
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
