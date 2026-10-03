from __future__ import annotations

from copy import deepcopy

import pytest

from tow.clients import files


def fail() -> ValueError:
    return ValueError("selection not confirmed")


ROWS = [
    {"index": 4, "name": "Show/S01E01.mkv", "size": 10, "priority": 1},
    {"index": 9, "name": "Show/S01E02.mkv", "size": 20, "priority": 0},
]


@pytest.mark.parametrize("value", [None, True, -1, "0", 0.5])
@pytest.mark.parametrize("key", ["index", "priority"])
def test_invalid_or_missing_ids_and_flags_cannot_confirm_selection(key, value):
    rows = deepcopy(ROWS)
    rows[1][key] = value
    with pytest.raises(ValueError, match="selection not confirmed"):
        files.verify_selection(ROWS, rows, {4}, fail=fail)


@pytest.mark.parametrize("change", ["missing", "extra", "duplicate", "rename", "size", "swap"])
def test_incomplete_or_changed_file_identity_cannot_confirm_selection(change):
    rows = deepcopy(ROWS)
    if change == "missing":
        rows.pop()
    elif change == "extra":
        rows.append({"index": 10, "name": "extra.mkv", "size": 30, "priority": 0})
    elif change == "duplicate":
        rows.append(deepcopy(rows[1]))
    elif change == "rename":
        rows[1]["name"] = "Show/other.mkv"
    elif change == "size":
        rows[1]["size"] = 21
    else:
        rows[0]["index"], rows[1]["index"] = rows[1]["index"], rows[0]["index"]
    with pytest.raises(ValueError, match="selection not confirmed"):
        files.verify_selection(ROWS, rows, {4}, fail=fail)


def test_client_list_reordering_and_positive_priority_levels_are_supported():
    rows = deepcopy(ROWS)[::-1]
    rows[1]["priority"] = 7
    files.verify_selection(ROWS, rows, {4}, fail=fail)
