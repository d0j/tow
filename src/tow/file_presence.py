"""Saved negative file evidence, bound to the exact observation it describes.

This is a diagnostic, never permission to recheck, start or download a torrent.
Unknown observations cannot resolve an earlier confirmed absence.
"""

from __future__ import annotations

import hashlib
import json
from collections.abc import Mapping
from typing import Any
from uuid import uuid4

from tow.clients import factory
from tow.records import Topic


def invalidate_client(state: dict[str, Any], config: Mapping[str, Any], client_id: str) -> None:
    """A changed endpoint invalidates local observations, including checks still in flight."""
    default_id = factory.default_client_id(dict(config))
    for topic in state.get("topics") or []:
        if isinstance(topic, dict) and str(topic.get("client_id") or default_id) == client_id:
            topic.pop("files_missing", None)
            topic["file_observation_revision"] = uuid4().hex


def binding(topic: Mapping[str, Any], client_id: str) -> str:
    fields = (
        "hash",
        "save_path",
        "selection",
        "selection_dirty",
        "selection_hash",
        "selection_verified",
        "file_aliases",
        "move_pending",
        "title",
        "tracker_title",
        "file_observation_revision",
    )
    values = {name: topic.get(name) for name in fields}
    values["client_id"] = client_id
    return hashlib.sha256(json.dumps(values, sort_keys=True, separators=(",", ":")).encode()).hexdigest()


def matches(topic: Mapping[str, Any], config: Mapping[str, Any], client_id: str, observed: object) -> bool:
    """A staged event still describes this topic and its currently selected client."""
    current = str(topic.get("client_id") or factory.default_client_id(dict(config)))
    return bool(observed) and current == client_id and observed == binding(topic, current)


def observe(topic: Topic, result: Mapping[str, Any], *, client_id: str, at: str) -> str:
    """Keep fresh missing-file evidence; clear it only after a fully known observation."""
    missing, unknown = result.get("missing_files"), result.get("unverified_files")
    if type(missing) is not int or type(unknown) is not int or missing < 0 or unknown < 0:
        return ""
    current_binding = binding(topic, client_id)
    raw_before = topic.get("files_missing")
    before = raw_before if isinstance(raw_before, Mapping) else {}
    prior_count = before.get("count")
    same_problem = before.get("binding") == current_binding and type(prior_count) is int and prior_count > 0
    if not missing:
        if not unknown:
            topic.pop("files_missing", None)
            return "restored" if same_problem else ""
        return ""
    if same_problem and prior_count == missing:
        return ""  # An unchanged problem keeps its identity and first observation time.
    topic["files_missing"] = {"binding": current_binding, "count": missing, "at": at}
    return "missing"


def count(topic: Mapping[str, Any], config: Mapping[str, Any]) -> int:
    """The saved absence count only while its torrent, client and owner intent still match."""
    evidence = topic.get("files_missing")
    if not isinstance(evidence, Mapping):
        return 0
    missing = evidence.get("count")
    if type(missing) is not int or missing <= 0 or not topic.get("hash"):
        return 0
    client_id = str(topic.get("client_id") or factory.default_client_id(dict(config)))
    return missing if evidence.get("binding") == binding(topic, client_id) else 0
