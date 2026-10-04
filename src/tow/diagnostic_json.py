"""Bounded, read-only decoding for small service records, not torrent stores."""

from __future__ import annotations

import json
import math
from datetime import UTC, datetime
from pathlib import Path
from typing import Any

from tow.clock import local_zone
from tow.store import decode_json_bytes

MAX_BYTES = 1024 * 1024
# Leave a day for local-zone conversion at the end of datetime's calendar.
MAX_EPOCH = 253402214400.0  # 9999-12-30 UTC


def read_object(path: Path, *, bom: bool = False) -> dict[str, Any]:
    with path.open("rb") as handle:
        raw = handle.read(MAX_BYTES + 1)
    if len(raw) > MAX_BYTES:
        raise ValueError("service record exceeds size limit")
    if bom and raw.startswith(b"\xef\xbb\xbf"):
        raw = raw[3:]
    value = decode_json_bytes(raw)
    if not isinstance(value, dict):
        raise TypeError("service record is not an object")
    return value


def encode_object(value: dict[str, Any]) -> str:
    text = json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False) + "\n"
    raw = text.encode("utf-8")
    if len(raw) > MAX_BYTES:
        raise ValueError("service record exceeds size limit")
    decode_json_bytes(raw)
    return text


def epoch(value: Any) -> float | None:
    """A finite, displayable epoch; booleans and overflow are not timestamps."""
    if isinstance(value, bool) or not isinstance(value, (int, float, str)):
        return None
    try:
        number = float(value)
    except ValueError, OverflowError:
        return None
    if not math.isfinite(number) or not 0 <= number <= MAX_EPOCH:
        return None
    try:
        datetime.fromtimestamp(number, UTC).astimezone(local_zone())
    except OSError, OverflowError, ValueError:
        return None
    return number


def check_epochs(value: dict[str, Any], fields: tuple[str, ...]) -> None:
    for field in fields:
        item = value.get(field)
        if item is not None and (not isinstance(item, (int, float)) or epoch(item) is None):
            raise ValueError("invalid service timestamp")


def check_types(value: dict[str, Any], fields: dict[str, type]) -> None:
    for field, expected in fields.items():
        item = value.get(field)
        if item is not None and type(item) is not expected:
            raise ValueError("invalid service field")
