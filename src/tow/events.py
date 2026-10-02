from __future__ import annotations

from uuid import uuid4


def new_event_id() -> str:
    return uuid4().hex


def new_operation_id(prefix: str = "op") -> str:
    return f"{prefix}-{uuid4().hex[:16]}"
