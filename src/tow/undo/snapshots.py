"""The encrypted secret snapshots an undo restores from, and how much of them it puts back."""

from __future__ import annotations

import copy
from typing import Any

from tow.store import SecretStoreError


def is_store_snapshot(value: object) -> bool:
    """A snapshot of the whole secrets store (settings, access, clients), not a site's."""
    if not isinstance(value, dict):
        return False
    if {"name", "present", "value"}.issubset(value):
        return False
    return all(isinstance(key, str) and isinstance(item, dict) for key, item in value.items())


def is_site_snapshot(value: object, name: str) -> bool:
    """A site's login snapshot ``{"name", "present", "value"}`` for the site ``name``."""
    if not isinstance(value, dict) or value.get("name") != name or not isinstance(value.get("present"), bool):
        return False
    return not value["present"] or isinstance(value.get("value"), dict)


def site_snapshot(secrets: dict[str, Any], name: str) -> dict[str, Any]:
    trackers = secrets.get("trackers")
    trackers = trackers if isinstance(trackers, dict) else {}
    return {"name": name, "present": name in trackers, "value": copy.deepcopy(trackers.get(name))}


def restore_scoped(current: dict[str, Any], snapshot: dict[str, Any], scope: object) -> dict[str, Any]:
    """``current`` with the part ``scope`` names put back from ``snapshot``: a later, unrelated
    change of another secret is kept."""
    if scope is None:  # undo points written by older TOW versions: the whole store
        return copy.deepcopy(snapshot)
    if not isinstance(scope, list) or len(scope) > 2 or any(not isinstance(key, str) or not key for key in scope):
        raise SecretStoreError("invalid settings secret undo scope")
    restored = copy.deepcopy(current)
    if not scope:
        return restored
    source: Any = snapshot
    target = restored
    for key in scope[:-1]:
        source = source.get(key) if isinstance(source, dict) else None
        if not isinstance(source, dict):
            source = {}
        target = target.setdefault(key, {})
        if not isinstance(target, dict):
            raise SecretStoreError("invalid settings secret undo target")
    key = scope[-1]
    if isinstance(source, dict) and key in source:
        target[key] = copy.deepcopy(source[key])
    else:
        target.pop(key, None)
    return restored
