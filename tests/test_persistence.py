import json
import multiprocessing
import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from helpers import reaped

from tow.store import StoreCorruptionError, load_json, persistence_lock, save_json


def _lock_probe(home: str, started, acquired) -> None:
    os.environ["TOW_HOME"] = home
    started.set()
    with persistence_lock():
        acquired.set()


def test_save_json_is_safe_for_concurrent_writers(tmp_path):
    path = tmp_path / "state.json"

    def write(index):
        save_json(path, {"writer": index, "values": list(range(index, index + 3))})

    with ThreadPoolExecutor(max_workers=8) as pool:
        list(pool.map(write, range(40)))

    payload = json.loads(path.read_text(encoding="utf-8"))
    assert payload["writer"] in range(40)
    assert list(path.parent.glob("state.json.*.tmp")) == []


def test_config_yaml_uses_atomic_write_and_reads_back(monkeypatch, tmp_path):
    from tow.config import load_config, save_config

    path = tmp_path / "config.yaml"
    monkeypatch.setenv("TOW_CONFIG", str(path))

    with ThreadPoolExecutor(max_workers=6) as pool:
        list(pool.map(lambda i: save_config({"bind": "127.0.0.1", "writer": i}), range(20)))

    loaded = load_config()
    assert loaded["writer"] in range(20)
    assert list(path.parent.glob(".config.yaml.*.tmp")) == []


def test_corrupt_json_is_quarantined_and_fails_closed(tmp_path):
    path = tmp_path / "state.json"
    path.write_text("{broken", encoding="utf-8")

    with pytest.raises(StoreCorruptionError, match="unreadable"):
        load_json(path, {})

    quarantined = list(path.parent.glob("state.json.corrupt-*"))
    assert len(quarantined) == 1
    assert Path(quarantined[0]).read_text(encoding="utf-8") == "{broken"


def test_corrupt_json_read_only_mode_does_not_quarantine(tmp_path):
    path = tmp_path / "state.json"
    path.write_text("{broken", encoding="utf-8")
    before = path.read_bytes()

    with pytest.raises(StoreCorruptionError, match="unreadable"):
        load_json(path, {}, quarantine=False)

    assert path.read_bytes() == before
    assert list(tmp_path.glob("state.json.corrupt-*")) == []


def test_persistence_lock_is_exclusive_across_processes(tmp_path, monkeypatch):
    home = tmp_path / "runtime"
    monkeypatch.setenv("TOW_HOME", str(home))
    context = multiprocessing.get_context("spawn")
    started = context.Event()
    acquired = context.Event()

    process = context.Process(target=_lock_probe, args=(str(home), started, acquired))
    with reaped(process):
        with persistence_lock():
            process.start()
            assert started.wait(10)
            assert not acquired.wait(0.5)

        assert acquired.wait(10)
        process.join(10)
        assert process.exitcode == 0


def test_persistence_lock_is_reentrant(tmp_path, monkeypatch):
    monkeypatch.setenv("TOW_HOME", str(tmp_path / "runtime"))

    with persistence_lock(), persistence_lock():
        save_json(tmp_path / "runtime" / "nested.json", {"ok": True})

    assert load_json(tmp_path / "runtime" / "nested.json", {}) == {"ok": True}
