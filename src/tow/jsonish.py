"""Helpers for loosely typed JSON/YAML data read from TOW stores."""

from __future__ import annotations

from typing import Any


def as_dict(value: object) -> dict[str, Any]:
    """``value`` when it is a dict, otherwise an empty dict (never ``None``)."""
    return value if isinstance(value, dict) else {}
