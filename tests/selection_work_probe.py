"""Large selection scenarios in one isolated interpreter, each with the work it took.

Run by tests/test_selection_work_bounds.py: ``python -I -B selection_work_probe.py <selection.py>
[scenario...]`` (all of them without names).
Prints one JSON line per scenario: its name, ``ok`` or the error, and ``calls`` - every call
the code of tow.selection made while the scenario ran (``sys.monitoring`` CALL events: Python
and C functions, a regex match included). The count depends only on the code and the input,
not on how busy the machine is, so a work bound replaces a wall-clock deadline: a matcher that
tries every mask on every file makes millions of calls where the prepared one makes a few per
file.
"""

from __future__ import annotations

import json
import sys
import traceback
from collections.abc import Callable
from pathlib import Path

from tow import selection
from tow.torrent import MAX_FILES, MAX_TORRENT_BYTES, TorrentFile, parse_torrent_metadata

SCENARIOS: dict[str, Callable[[], None]] = {}


def _resolve(files: tuple[TorrentFile, ...], policy: dict[str, object]) -> tuple[object, str]:
    """(the plan, "") or (None, the code of the refusal)."""
    try:
        return selection.resolve_selection(files, policy), ""
    except selection.SelectionError as error:
        if isinstance(error, selection.SelectionPendingError):
            return error.params, error.code
        return None, error.code


# --- episode rules ---------------------------------------------------------------------------


def _episodes(kind: str) -> None:
    files = tuple(TorrentFile(i, f"Show.S01E{i + 1:04d}.mkv", 1) for i in range(1000))
    if kind == "metadata-limit":
        files = tuple(TorrentFile(i, f"Copy {i // 1000}/Show.S01E{i % 1000 + 1:04d}.mkv", 1) for i in range(MAX_FILES))
    if kind in {"repeated", "metadata-limit"}:
        expression = ",".join(["1-1000"] * 300)
    elif kind == "overlapping":
        expression = ",".join(f"{n}-{n + 600}" for n in range(1, 451))
    else:
        expression = ",".join(["S01E2000-E3000"] * 300)
    policy = selection.normalize_policy("episodes", expression)
    if kind == "future":
        params, code = _resolve(files, policy)
        assert code == "selection.not_out_yet", code
        assert isinstance(params, dict)
        assert params["episodes"] == ", ".join(f"S01E{n}" for n in range(2000, 2010))
        return
    plan = selection.resolve_selection(files, policy)
    assert plan.selected_indices == tuple(range(len(files)))
    assert len(plan.selected_episode_keys) == 1000
    assert plan.expression == expression


for _kind in ("repeated", "overlapping", "future", "metadata-limit"):
    SCENARIOS[f"episodes-{_kind}"] = lambda kind=_kind: _episodes(kind)


# --- file masks on flat names -----------------------------------------------------------------


def _flat_masks(kind: str) -> None:
    files = tuple(TorrentFile(i, f"Asset {i:05d}.bin", 1) for i in range(MAX_FILES))
    patterns = ["*never-matches*" if kind == "repeated" else f"*absent-{i:03d}*" for i in range(500)]
    expression = ",".join(patterns)
    policy = selection.normalize_policy("files", expression)
    assert policy["value"] == expression
    assert _resolve(files, policy) == (None, "selection.nothing_matched")  # unmatched masks select nothing


for _kind in ("repeated", "distinct"):
    SCENARIOS[f"files-{_kind}"] = lambda kind=_kind: _flat_masks(kind)


# --- file masks on parsed torrents with long paths ---------------------------------------------


def _encode(value: object) -> bytes:
    if isinstance(value, bytes):
        return str(len(value)).encode() + b":" + value
    if isinstance(value, int):
        return b"i" + str(value).encode() + b"e"
    if isinstance(value, list):
        return b"l" + b"".join(_encode(item) for item in value) + b"e"
    assert isinstance(value, dict)
    return b"d" + b"".join(_encode(key) + _encode(value[key]) for key in sorted(value)) + b"e"


_TORRENTS: dict[tuple[str, str], tuple[TorrentFile, ...]] = {}


def _parsed(layout: str, extra: str) -> tuple[TorrentFile, ...]:
    """The parsed fixture torrent; parsing is not what is measured, so it is shared."""
    if (layout, extra) not in _TORRENTS:
        file_count = 7000 if layout == "deep" else MAX_FILES
        entries = []
        for index in range(file_count):
            path = [("a" * 240 + extra + f"{index:05d}.bin").encode()]
            if layout == "nested":
                path = [b"b" * 200, b"c" * 200, *path]
            elif layout == "deep":
                path = [b"b" * 250 for _ in range(15)] + path
            entries.append({b"length": 1, b"path": path})
        pieces = b"x" * (20 * ((file_count + 16383) // 16384))
        torrent = _encode(
            {b"info": {b"name": b"Fixture", b"piece length": 16384, b"pieces": pieces, b"files": entries}}
        )
        assert len(torrent) < MAX_TORRENT_BYTES
        files = parse_torrent_metadata(torrent).files
        assert len(files) == file_count
        _TORRENTS[(layout, extra)] = files
    return _TORRENTS[(layout, extra)]


_TEMPLATES = {
    "literal": "*absent-{index:03d}*",
    "prefix": "*z{index:03d}*[.]bin",
    "suffix": "*[a]z{index:03d}*",
    "middle": "*[a]z{index:03d}[b]*",
    "malformed": "*z{index:03d}[",
    "present": "*aaaaaaaa*z{index:03d}*",
    "sparse": "*aaaaaa*z{index:03d}*x*",
    "sparse-middle": "*[a]z{index:03d}[b]*",
    "multi-class": "*[ab]z{index:03d}[bc]*",
    "ordering": "*z{index:03d}*aaaaaa*",
}
MASK_KINDS = (*_TEMPLATES, "no-fixed", "distinct-classes", "positive-last")
LAYOUTS = ("long", "nested", "deep")


def _patterns(kind: str) -> list[str]:
    if kind == "distinct-classes":
        return [f"*[a{chr(0x1000 + index)}][x{chr(0x1200 + index)}]*" for index in range(500)]
    if kind == "no-fixed":
        return [f"*[a{chr(0x1000 + index)}][xy]*" for index in range(500)]
    if kind == "positive-last":
        return [f"*absent-{index:03d}*" for index in range(499)] + ["*[[]*"]
    return [_TEMPLATES[kind].format(index=index) for index in range(500)]


def _long_paths(layout: str, kind: str) -> None:
    sparse = kind.startswith("sparse") or kind in {"multi-class", "ordering"}
    files = _parsed(layout, "z000" if sparse else "[" if kind == "positive-last" else "")
    expression = ",".join(_patterns(kind))
    policy = selection.normalize_policy("files", expression)
    plan, code = _resolve(files, policy)
    if kind == "positive-last":
        assert code == "", code
        assert isinstance(plan, selection.SelectionPlan)
        assert plan.selected_indices == tuple(range(len(files)))
    else:
        assert code == "selection.nothing_matched", code
    assert policy["value"] == expression


for _layout in LAYOUTS:
    for _kind in MASK_KINDS:
        SCENARIOS[f"paths-{_layout}-{_kind}"] = lambda layout=_layout, kind=_kind: _long_paths(layout, kind)


# --- the run ----------------------------------------------------------------------------------

_TOOL = sys.monitoring.PROFILER_ID
_SELECTION = selection.__file__
_calls = 0


def _count(code: object, *_args: object) -> object:
    """Count a call made by tow.selection; a call site anywhere else is switched off for good."""
    global _calls
    if getattr(code, "co_filename", None) != _SELECTION:
        return sys.monitoring.DISABLE
    _calls += 1
    return None


def measure(function: Callable[[], None]) -> dict[str, object]:
    global _calls
    _calls = 0
    sys.monitoring.set_events(_TOOL, sys.monitoring.events.CALL)
    try:
        function()
    except Exception as error:  # noqa: BLE001 - reported to the test, which fails on it
        sys.monitoring.set_events(_TOOL, 0)
        return {"ok": False, "error": "".join(traceback.format_exception_only(error)).strip(), "calls": _calls}
    sys.monitoring.set_events(_TOOL, 0)
    return {"ok": True, "calls": _calls}


def main() -> None:
    """``selection_work_probe.py <selection.py> [scenario...]``: the named scenarios, or all."""
    assert Path(selection.__file__).resolve() == Path(sys.argv[1])
    names = sys.argv[2:] or list(SCENARIOS)
    unknown = sorted(set(names) - set(SCENARIOS))
    assert not unknown, f"unknown scenarios: {unknown}"
    sys.monitoring.use_tool_id(_TOOL, "tow-selection-work")
    sys.monitoring.register_callback(_TOOL, sys.monitoring.events.CALL, _count)
    for layout in LAYOUTS:  # build the fixtures outside the measured work
        for extra in ("", "z000", "["):
            _parsed(layout, extra)
    for name in names:
        print(json.dumps({"name": name, **measure(SCENARIOS[name])}), flush=True)


if __name__ == "__main__":
    main()
