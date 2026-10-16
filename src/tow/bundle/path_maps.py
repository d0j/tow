"""``--path-map OLD=NEW``: the folders of an imported state.json moved to this computer's."""

from __future__ import annotations

import copy
import ntpath
import re
from collections.abc import Iterable
from typing import Any

from tow.bundle.errors import ExportImportError
from tow.errors import Msg
from tow.folders import windows_shaped

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


def _split(value: str, windows: bool) -> tuple[str, list[re.Match[str]]]:
    """The leading separators of ``value`` and its names, each with where it ends."""
    separators = re.match(r"[\\/]*" if windows else r"/*", value)
    head = separators.group() if separators else ""
    return head, list(re.finditer(r"[^\\/]+" if windows else r"[^/]+", value))


def _rest_below(value: str, old: str) -> str | None:
    """What follows the folder ``old`` in ``value``, from its separator on and as written ("" for
    the folder itself); None when ``value`` is not that folder or below it. Compared the way
    OLD's own system compares: a Windows path (``D:\\Media``, ``\\\\nas\\share``) ignores case and
    takes either slash; a POSIX one is exact, and a backslash is a character of its names."""
    windows = windows_shaped(old)
    old_head, old_names = _split(old.rstrip("/\\") or old, windows)
    head, names = _split(value, windows)
    if windows:
        old_head, head = old_head.replace("/", "\\"), head.replace("/", "\\")
    if head != old_head or len(names) < len(old_names):
        return None
    for want, have in zip(old_names, names, strict=False):
        same = (
            ntpath.normcase(want.group()) == ntpath.normcase(have.group()) if windows else want.group() == have.group()
        )
        if not same:
            return None
    if old_names:
        return value[names[len(old_names) - 1].end() :]
    rest = value[len(head) :]  # OLD is a root ("/"): the whole path is below it
    return head[-1] + rest if rest else ""


def _map_one(value: str, mappings: list[tuple[str, str]]) -> tuple[str, bool]:
    for old, new in mappings:
        rest = _rest_below(value, old)
        if rest is None:
            continue
        if not rest:
            return new, True
        # The rest with the separator of NEW's system (never a case-folded copy of it).
        if windows_shaped(old) and not windows_shaped(new):
            rest = rest.replace("\\", "/")
        elif not windows_shaped(old) and windows_shaped(new):
            rest = rest.replace("/", "\\")
        return new.rstrip("/\\") + rest, True
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
