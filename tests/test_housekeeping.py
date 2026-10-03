"""Housekeeping: bounded import checkpoints, compact download history."""

import json

from tow import bundle as tow_bundle
from tow.paths import data_dir, download_history_path
from tow.store import load_download_history, save_download_history


def _checkpoint(name: str, status: str | None) -> None:
    path = data_dir() / "import-checkpoints" / name
    path.mkdir(parents=True)
    (path / "files").mkdir()
    manifest = {
        "format": tow_bundle.CHECKPOINT_FORMAT,
        "targets": [
            {"member": member, "target": str(target), "exists": False, "sha256": None}
            for member, target in tow_bundle._checkpoint_targets()
        ],
    }
    (path / "MANIFEST.json").write_text(json.dumps(manifest), encoding="utf-8")
    if status is not None:
        tow_bundle._write_import_transaction(path, status=status)


def test_prune_keeps_newest_and_never_touches_unfinished_checkpoints():
    for day in range(1, 9):
        _checkpoint(f"2026-09-0{day}T00-00-00-aaaa", "committed")
    _checkpoint("2026-08-01T00-00-00-prep", "prepared")
    _checkpoint("2026-08-02T00-00-00-noread", None)

    removed = tow_bundle.prune_import_checkpoints(keep=5)

    left = sorted(p.name for p in (data_dir() / "import-checkpoints").iterdir())
    assert removed == 3
    assert "2026-08-01T00-00-00-prep" in left
    assert "2026-08-02T00-00-00-noread" in left
    assert [n for n in left if n.startswith("2026-09")] == [f"2026-09-0{d}T00-00-00-aaaa" for d in range(4, 9)]


def test_download_history_is_written_compact_and_round_trips():
    data = {"schema_version": 1, "topics": {"t": {"items": {"a": {"label": "Серия 1"}}}}}
    save_download_history(data)
    raw = download_history_path().read_text(encoding="utf-8")
    assert "\n  " not in raw
    assert "Серия 1" in raw
    assert json.loads(raw) == data == load_download_history()
