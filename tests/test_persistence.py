import json
import multiprocessing
import os
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

import pytest
from helpers import reaped

from tow.store import JSON_MAX_DEPTH, StoreCorruptionError, StoreReadError, load_json, persistence_lock, save_json


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


@pytest.mark.parametrize(
    "raw",
    [
        b'{"number":NaN}',
        b'{"number":Infinity}',
        b'{"number":-Infinity}',
        b'{"number":1e9999}',
        b'{"number":-1e9999}',
        b'{"number":' + b"1" * 5000 + b"}",
        b"[" * 20000 + b"]" * 20000,
    ],
    ids=["nan", "inf", "negative-inf", "overflow", "negative-overflow", "integer-limit", "deep"],
)
@pytest.mark.parametrize("quarantine", [False, True])
def test_invalid_json_numbers_and_parser_limits_fail_as_store_errors(tmp_path, raw, quarantine):
    path = tmp_path / "state.json"
    path.write_bytes(raw)
    with pytest.raises(StoreCorruptionError, match="unreadable"):
        load_json(path, {}, quarantine=quarantine)
    copies = list(tmp_path.glob("state.json.corrupt-*"))
    if quarantine:
        assert not path.exists()
        assert len(copies) == 1
        assert copies[0].read_bytes() == raw
    else:
        assert path.read_bytes() == raw
        assert not copies


@pytest.mark.parametrize("compact", [False, True])
@pytest.mark.parametrize("value", [float("nan"), float("inf"), float("-inf")])
def test_non_finite_json_cannot_overwrite_an_existing_store(tmp_path, compact, value):
    path = tmp_path / "state.json"
    save_json(path, {"keep": True})
    before = path.read_bytes()
    with pytest.raises(StoreCorruptionError, match="not serializable"):
        save_json(path, {"nested": [{"number": value}]}, compact=compact)
    assert path.read_bytes() == before
    assert not list(tmp_path.glob(".*.tmp"))


def test_transient_io_error_during_corruption_recheck_never_quarantines(monkeypatch, tmp_path):
    path = tmp_path / "state.json"
    path.write_bytes(b'{"keep": true}')
    original = Path.read_bytes
    reads = 0

    def racing_read(current):
        nonlocal reads
        if current == path:
            reads += 1
            if reads == 1:
                return b"{partial observation"
            raise PermissionError("transient lock")
        return original(current)

    with monkeypatch.context() as patched:
        patched.setattr(Path, "read_bytes", racing_read)
        with pytest.raises(StoreReadError):
            load_json(path, {})
    assert path.read_bytes() == b'{"keep": true}'
    assert not list(tmp_path.glob("state.json.corrupt-*"))


def test_store_replaced_with_valid_json_before_the_recheck_is_not_quarantined(monkeypatch, tmp_path):
    path = tmp_path / "state.json"
    path.write_bytes(b'{"keep": true, "number": 1.25}')
    original = Path.read_bytes
    reads = 0

    def racing_read(current):
        nonlocal reads
        if current == path:
            reads += 1
            if reads == 1:
                return b"{partial observation"
        return original(current)

    monkeypatch.setattr(Path, "read_bytes", racing_read)
    assert load_json(path, {}) == {"keep": True, "number": 1.25}
    assert path.exists()
    assert not list(tmp_path.glob("state.json.corrupt-*"))


def test_excessive_json_encoding_depth_never_overwrites_the_store(tmp_path):
    path = tmp_path / "state.json"
    save_json(path, {"keep": True})
    before = path.read_bytes()
    value = []
    for _ in range(20000):
        value = [value]
    with pytest.raises(StoreCorruptionError, match="not serializable"):
        save_json(path, value, compact=True)
    assert path.read_bytes() == before


def test_store_nesting_limit_is_explicit_and_identical_for_reading_and_writing(tmp_path):
    path = tmp_path / "state.json"
    value = 1
    for _ in range(JSON_MAX_DEPTH):
        value = [value]
    save_json(path, value, compact=True)
    assert load_json(path, {}) == value
    before = path.read_bytes()
    with pytest.raises(StoreCorruptionError, match="not serializable"):
        save_json(path, [value], compact=True)
    assert path.read_bytes() == before
    path.write_bytes(b"[" + before.strip() + b"]")
    with pytest.raises(StoreCorruptionError, match="unreadable"):
        load_json(path, {}, quarantine=False)


def _nested(depth, leaf, *, mapping=False):
    value = leaf
    for _ in range(depth):
        value = {"k": value, "n": 1} if mapping else [1, value, "x"]
    return value


@pytest.mark.parametrize("mapping", [False, True])
@pytest.mark.parametrize(
    ("depth", "leaf", "allowed"),
    [
        (JSON_MAX_DEPTH, 1, True),
        (JSON_MAX_DEPTH, [], True),  # an empty container at the limit holds nothing deeper
        (JSON_MAX_DEPTH, {}, True),
        (JSON_MAX_DEPTH, [1], False),
        (JSON_MAX_DEPTH, {"k": None}, False),
        (JSON_MAX_DEPTH + 1, [], False),
    ],
)
def test_store_nesting_limit_counts_every_value_and_container(depth, leaf, allowed, mapping):
    from tow.store import decode_json_bytes

    value = {"wide": [[1, 2]] * 50, "deep": _nested(depth - 1, leaf, mapping=mapping)}
    raw = json.dumps(value).encode()
    if allowed:
        assert decode_json_bytes(raw) == value
    else:
        with pytest.raises(ValueError, match="nesting"):
            decode_json_bytes(raw)


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
