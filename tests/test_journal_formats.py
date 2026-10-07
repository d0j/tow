"""Journals left by an interrupted write of an earlier version are still recovered.

``tests/journal_fixtures/<case>/home`` is what a crash left in the data folder, written by the
journal code of v1.24.1 (before the check, store, import and night-restore journals shared
``tow.journal``); ``expected.json`` holds the live files before the write ("before", null for a
file that did not exist) and the half-written ones the crash left ("mixed"). ``{TOW_HOME}/<path>``
in a manifest stands for the install the journal was written in. Whichever process takes the
data lock next must put "before" back - or, for a write that had committed, keep "mixed".
"""

from __future__ import annotations

import json
import re
import shutil
from pathlib import Path
from typing import Any

import pytest

from tow.store import persistence_lock

FIXTURES = Path(__file__).parent / "journal_fixtures"
_PLACEHOLDER = re.compile(r'"\{TOW_HOME\}((?:/[^"/]+)*)"')


def _install(case: str, home: Path) -> dict[str, Any]:
    """The fixture's journal in this test's data folder, the live files as the crash left them."""
    source = FIXTURES / case / "home"
    for path in sorted(source.rglob("*")):
        if path.is_dir():
            continue
        target = home.joinpath(*path.relative_to(source).parts)
        target.parent.mkdir(parents=True, exist_ok=True)
        if path.suffix == ".json":
            text = _PLACEHOLDER.sub(
                lambda match: json.dumps(str(home.resolve().joinpath(*match.group(1).split("/")[1:]))),
                path.read_text(encoding="utf-8"),
            )
            target.write_text(text, encoding="utf-8", newline="")
        else:
            shutil.copyfile(path, target)
    expected: dict[str, Any] = json.loads((FIXTURES / case / "expected.json").read_text(encoding="utf-8"))
    for name, content in expected["mixed"].items():
        target = home.joinpath(*name.split("/"))
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_bytes(content.encode("utf-8"))
    return expected


def _live(home: Path, names: list[str]) -> dict[str, str | None]:
    files = {name: home.joinpath(*name.split("/")) for name in names}
    return {name: path.read_bytes().decode("utf-8") if path.is_file() else None for name, path in files.items()}


def _next_process_takes_the_lock() -> None:
    with persistence_lock():
        pass


@pytest.mark.parametrize("status", ["prepared", "history_committed", "committed"])
def test_a_check_transaction_of_v1_24_1_is_recovered(tmp_path, status):
    expected = _install(f"check-transaction-v1-{status}", tmp_path)

    _next_process_takes_the_lock()

    final = expected["mixed"] if status == "committed" else expected["before"]
    assert _live(tmp_path, list(final)) == final
    assert not (tmp_path / ".tow-check-transaction").exists()


@pytest.mark.parametrize("status", ["prepared", "committed"])
def test_a_store_transaction_of_v1_24_1_is_recovered(tmp_path, status):
    expected = _install(f"site-transaction-v1-{status}", tmp_path)

    _next_process_takes_the_lock()

    final = expected["mixed"] if status == "committed" else expected["before"]
    assert _live(tmp_path, list(final)) == final
    assert not (tmp_path / ".tow-site-transaction").exists()


@pytest.mark.parametrize("status", ["prepared", "rollback-prepared", "committed"])
def test_an_import_checkpoint_of_v1_24_1_is_recovered(tmp_path, status):
    expected = _install(f"import-checkpoint-v1-{status}", tmp_path)
    (checkpoint,) = (tmp_path / "import-checkpoints").iterdir()

    _next_process_takes_the_lock()

    final = expected["mixed"] if status == "committed" else expected["before"]
    assert _live(tmp_path, list(final)) == final
    transaction = json.loads((checkpoint / "TRANSACTION.json").read_text(encoding="utf-8"))
    assert transaction["status"] == ("committed" if status == "committed" else "rolled_back")
    assert transaction.get("recovered") is (None if status == "committed" else True)
    assert (checkpoint / "MANIFEST.json").is_file()  # a checkpoint is kept after its import


@pytest.mark.parametrize("status", ["prepared", "committed"])
def test_a_night_copy_restore_of_v1_24_1_is_recovered(tmp_path, status):
    expected = _install(f"night-restore-v1-{status}", tmp_path)
    (safety,) = tmp_path.glob("before-restore-*")

    _next_process_takes_the_lock()

    final = expected["mixed"] if status == "committed" else expected["before"]
    assert _live(tmp_path, list(final)) == final
    assert not (tmp_path / ".tow-night-restore.json").exists()
    journal = json.loads((safety / "RESTORE.json").read_text(encoding="utf-8"))
    assert journal["status"] == ("committed" if status == "committed" else "rolled_back")
