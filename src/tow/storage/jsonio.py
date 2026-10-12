"""The JSON codec of the stores: finite numbers, bounded nesting, and the exact bytes written.

Hashes and signatures are taken over these bytes: ``encode_json`` must keep producing them.
"""

from __future__ import annotations

import json
import math
from typing import Any


def _finite_json_float(value: str) -> float:
    number = float(value)
    if not math.isfinite(number):
        raise ValueError("non-finite JSON number")
    return number


JSON_MAX_DEPTH = 128


def check_json_depth(value: Any) -> None:
    # Native parser/encoder stack limits differ by OS. Keep the store contract
    # explicit, and walk level by level so this check has no recursion limit.
    # Only containers are visited: a value inside a non-empty container at the
    # limit is one level too deep, whatever it is.
    level = [value] if isinstance(value, (dict, list, tuple)) else []
    depth = 0
    while level:
        deeper: list[Any] = []
        for item in level:
            children = item.values() if isinstance(item, dict) else item
            if not children:
                continue
            if depth >= JSON_MAX_DEPTH:
                raise ValueError("JSON nesting exceeds the store limit")
            deeper.extend([child for child in children if isinstance(child, (dict, list, tuple))])
        level = deeper
        depth += 1


def decode_json_bytes(raw: bytes) -> Any:
    """Decode a store or recovery journal with finite numbers and bounded nesting."""
    value = json.loads(raw.decode("utf-8"), parse_float=_finite_json_float, parse_constant=_finite_json_float)
    check_json_depth(value)
    return value


def encode_json(value: Any, *, compact: bool) -> bytes:
    """``value`` as UTF-8 JSON without a final newline: compact (no spaces) or indented by two.

    Non-ASCII stays as it is, a non-finite number or nesting deeper than ``JSON_MAX_DEPTH`` is
    refused; the error is what ``json.dumps`` or the encoder raises (TypeError, ValueError,
    UnicodeError, RecursionError).
    """
    check_json_depth(value)
    if compact:
        return json.dumps(value, ensure_ascii=False, separators=(",", ":"), allow_nan=False).encode("utf-8")
    return json.dumps(value, ensure_ascii=False, indent=2, allow_nan=False).encode("utf-8")
