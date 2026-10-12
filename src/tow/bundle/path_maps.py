"""``--path-map OLD=NEW``: the folders of an imported state.json moved to this computer's."""

from __future__ import annotations

import copy
import re
from collections.abc import Iterable
from typing import Any

from tow.bundle.errors import ExportImportError
from tow.errors import Msg

_DRIVE_PATH = re.compile(r"^[A-Za-z]:[\\/]")


def parse_path_maps(values: Iterable[str] | None) -> list[tuple[str, str]]:
    def refuse(message: str) -> ExportImportError:
        return ExportImportError(message, owner_text=Msg("cli.bundle.path_map"))

    result = []
    for raw in values or ():
        if not isinstance(raw, str) or "=" not in raw:
            raise refuse("path map must use OLD=NEW")
        old, new = raw.split("=", 1)
        old, new = old.strip(), new.strip()
        if not old or not new:
            raise refuse("path map must contain non-empty OLD and NEW")
        if old == new:
            raise refuse("path map OLD and NEW must differ")
        result.append((old, new))
    if len({old for old, _ in result}) != len(result):
        raise refuse("duplicate path map source")
    return result


def _looks_absolute(value: str) -> bool:
    return bool(_DRIVE_PATH.match(value) or value.startswith(("/", "\\\\")))


def _map_one(value: str, mappings: list[tuple[str, str]]) -> tuple[str, bool]:
    for old, new in mappings:
        old_clean = old.rstrip("/\\")
        if value == old_clean:
            return new, True
        for separator in ("/", "\\"):
            prefix = old_clean + separator
            if value.startswith(prefix):
                return new.rstrip("/\\") + value[len(old_clean) :], True
    return value, False


def apply_path_maps(state: dict[str, Any], mappings: list[tuple[str, str]]) -> tuple[dict[str, Any], list[str]]:
    result = copy.deepcopy(state)
    warnings: list[str] = []
    roots = result.get("save_roots")
    if isinstance(roots, list):
        for index, value in enumerate(roots):
            if isinstance(value, str):
                mapped, matched = _map_one(value, mappings)
                result["save_roots"][index] = mapped
                if _looks_absolute(value) and not matched:
                    warnings.append(f"unmapped save root: {value}")
    for topic in result.get("topics") or []:
        if not isinstance(topic, dict):
            continue
        value = topic.get("save_path")
        if isinstance(value, str):
            mapped, matched = _map_one(value, mappings)
            topic["save_path"] = mapped
            if _looks_absolute(value) and not matched:
                warnings.append(f"unmapped topic path: {value}")
    return result, warnings
