"""The history index and identity: which history item a client file is, across renames,
legacy keys and torrent revisions."""

from __future__ import annotations

from typing import Any

from tow.records import HistoryItem


def path_key(item: HistoryItem) -> str:
    return str(item.get("relative_path") or "").casefold()


class HistoryIndex:
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
            bucket = self._by_path.setdefault(path_key(item), [])
            if identity not in bucket:
                bucket.append(identity)

    def at_path(self, rel: str) -> list[tuple[str, HistoryItem]]:
        key = rel.casefold()
        found = []
        for identity in dict.fromkeys(self._by_path.get(key, ())):
            item = self._items.get(identity)
            if isinstance(item, dict) and path_key(item) == key:
                found.append((identity, item))
        return found


def adopt_previous_item(
    items: dict[str, HistoryItem],
    base_identity: str,
    *,
    rel: str,
    size: int | None,
    label: Any,
    coverage: tuple[Any, ...],
    current_source_hash: str,
    current_client_paths: set[str],
    index: HistoryIndex,
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
            and path_key(old_item) not in current_client_paths
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


def revision_identity(
    items: dict[str, HistoryItem], base_identity: str, rel: str, source_hash: str, index: HistoryIndex
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


def carry_completion(
    identity: str, rel: str, size: int | None, current_source_hash: str, index: HistoryIndex
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
