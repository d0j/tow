"""A restored data copy never grants permission for another torrent-client operation."""

from __future__ import annotations

import json
from typing import Any

from tow.store import decode_json_bytes


def forget(state: dict[str, Any]) -> bool:
    """Discard archived requests, including one a restored Undo could bring back."""
    topics = state.get("topics")
    candidates = [topic for topic in topics if isinstance(topic, dict)] if isinstance(topics, list) else []
    undo = state.get("undo")
    if isinstance(undo, dict) and isinstance(undo.get("item"), dict):
        candidates.append(undo["item"])
    changed = False
    for topic in candidates:
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
