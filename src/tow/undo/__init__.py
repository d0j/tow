"""Undo: the owner's last change, put back (``tow.undo.engine``; the kinds in ``tow.undo.kinds``).

stamp(state, "topic", item=topic, index=3)   # a change the owner can undo
apply(local=True) -> Outcome                  # put it back: what happened, in words
can_undo(state), undo_left_sec(state), undo_just_made(state), undo_label(state)
"""

from __future__ import annotations

from tow.undo import kinds as _kinds  # noqa: F401 - registers every kind
from tow.undo.engine import (
    KINDS,
    PENDING,
    Outcome,
    apply,
    can_undo,
    cleanup,
    cleanup_needed,
    invalidate,
    is_live,
    release,
    stamp,
    stamp_of,
    stamped_here,
    ttl_sec,
    undo_just_made,
    undo_label,
    undo_left_sec,
)
from tow.undo.kinds import TOPIC_EDIT_FIELDS
from tow.undo.records import SECRET_REFS, UndoRecord, secret_refs

__all__ = [
    "KINDS",
    "PENDING",
    "SECRET_REFS",
    "TOPIC_EDIT_FIELDS",
    "Outcome",
    "UndoRecord",
    "apply",
    "can_undo",
    "cleanup",
    "cleanup_needed",
    "invalidate",
    "is_live",
    "release",
    "secret_refs",
    "stamp",
    "stamp_of",
    "stamped_here",
    "ttl_sec",
    "undo_just_made",
    "undo_label",
    "undo_left_sec",
]
