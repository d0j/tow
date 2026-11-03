"""A restored data copy never grants permission for another torrent-client operation."""

from __future__ import annotations

import json
from copy import deepcopy
from typing import Any
from uuid import uuid4

from tow.store import decode_json_bytes


def _freeze_handover(topic: dict[str, Any]) -> bool:
    verification = topic.get("verification_pending")
    space = topic.get("waiting_space")
    changed = bool(space)
    if space:
        topic.pop("waiting_space")
    if not verification and not space:
        return changed
    if isinstance(verification, dict) and verification:
        waiting = verification
    else:
        old_space = space if isinstance(space, dict) else {}
        # The four-field owner format is read by releases since 1.30.5. The marker is
        # deliberately blocked, not a fabricated record of a start being attempted.
        waiting = {
            "hash": str(old_space.get("hash") or topic.get("hash") or ""),
            "client": str(old_space.get("client") or topic.get("client_id") or ""),
            "path": str(topic.get("save_path") or ""),
            "owner": [repr(topic.get(key)) for key in ("url", "save_path", "client_id", "selection")],
            "since": str(old_space.get("since") or ""),
            "kind": str(old_space.get("kind") or "added"),
            "selection_update": False,
            "completion_event": "client_started",
            "resume_after_check": True,
            "restored_waiting_space": deepcopy(space),
        }
        topic["verification_pending"] = waiting
        changed = True
    freezing = (
        bool(space) or waiting.get("blocked") != "check.restored_confirmation" or waiting.get("repair_only") is not True
    )
    if freezing:
        topic["file_observation_revision"] = uuid4().hex
    for key, value in {"blocked": "check.restored_confirmation", "blocked_params": {}, "repair_only": True}.items():
        if waiting.get(key) != value:
            waiting[key] = value
            changed = True
    return changed


def forget(state: dict[str, Any]) -> bool:
    """Discard archived requests, including one a restored Undo could bring back."""
    topics = state.get("topics")
    candidates = [topic for topic in topics if isinstance(topic, dict)] if isinstance(topics, list) else []
    undo = state.get("undo")
    if isinstance(undo, dict) and isinstance(undo.get("item"), dict):
        candidates.append(undo["item"])
    changed = False
    for topic in candidates:
        changed = _freeze_handover(topic) or changed
        for key in ("repair_pending", "repair_request_revision"):
            if key in topic:
                topic.pop(key)
                changed = True
    return changed


def without_authorizations(content: bytes) -> bytes:
    """Preserve untouched bytes when possible; this is never transaction compensation."""
    state = decode_json_bytes(content)
    if not isinstance(state, dict) or not forget(state):
        return content
    return (json.dumps(state, ensure_ascii=False, separators=(",", ":"), allow_nan=False) + "\n").encode("utf-8")
