"""The clearance store (data/protection.enc) refuses a damaged, oversized or redirected store
without changing it, and its reset refuses anything it cannot move back intact."""

from __future__ import annotations

import json
import os
import sys
import time
from pathlib import Path
from types import SimpleNamespace

import pytest
from test_protection_providers import ORIGIN, settings

from tow.paths import data_dir
from tow.protection import store
from tow.store import master_fernet

KEY = "c" * 64
BINDING = "a" * 64
RESERVATION = {"attempt_at": 1000, "lease_until": 1100, "settings": BINDING, "attempt_id": "b" * 32}
SERVICE = {
    "attempt_at": 1000,
    "lease_until": 1000,
    "settings": BINDING,
    "service_status": "unavailable",
    "service_failures": 1,
    "service_retry_at": 1030,
}


def _write(value) -> Path:
    path = data_dir() / "protection.enc"
    path.write_bytes(master_fernet().encrypt(json.dumps(value).encode("utf-8")))
    return path


def _stored(records: dict) -> Path:
    return _write({"version": 2, "records": records})


def _refused(path: Path, message: str) -> None:
    before = path.read_bytes()
    with pytest.raises(ValueError, match=message):
        store.read("site", ORIGIN, settings(), now=time.time())
    assert path.read_bytes() == before


def test_a_sound_store_is_read():
    """The control for the damage below: these records pass as they are."""
    _stored({KEY: RESERVATION, "d" * 64: SERVICE})
    assert set(store._load()) == {KEY, "d" * 64}


@pytest.mark.parametrize(
    ("record", "message"),
    [
        (5, "invalid clearance record"),
        ({**RESERVATION, "unknown": 1}, "invalid clearance record"),
        ({key: value for key, value in RESERVATION.items() if key != "settings"}, "invalid clearance record"),
        ({**RESERVATION, "clearance": {"attempt_id": "B" * 32}}, "invalid clearance identity"),
        ({**RESERVATION, "attempt_at": -1}, "invalid clearance reservation"),
        ({**RESERVATION, "attempt_at": True}, "invalid clearance reservation"),
        ({**RESERVATION, "lease_until": 999}, "invalid clearance reservation"),
        ({**RESERVATION, "attempt_at": 253402300800, "lease_until": 253402300800}, "invalid clearance reservation"),
        ({**RESERVATION, "settings": "A" * 64}, "invalid clearance configuration binding"),
        ({**RESERVATION, "settings": "a" * 63}, "invalid clearance configuration binding"),
        ({**RESERVATION, "attempt_id": "b" * 31}, "invalid clearance attempt"),
        ({**RESERVATION, "provider_cursor": "Not-A-Cursor"}, "invalid clearance attempt"),
        ({**SERVICE, "service_status": "fine"}, "invalid clearance service observation"),
        ({**SERVICE, "service_failures": 9}, "invalid clearance service observation"),
        ({**SERVICE, "service_failures": 1.0}, "invalid clearance service observation"),
        ({**SERVICE, "service_failures": 0}, "invalid clearance service observation"),
        ({**SERVICE, "service_retry_at": 1000 + 86401}, "invalid clearance service observation"),
        ({**SERVICE, "service_retry_at": 999}, "invalid clearance service observation"),
        ({**SERVICE, "service_status": "succeeded", "service_failures": 1}, "invalid clearance service observation"),
        ({**SERVICE, "service_status": "succeeded", "service_failures": 0}, "invalid clearance service observation"),
    ],
    ids=lambda value: value if isinstance(value, str) else None,
)
def test_a_damaged_record_is_refused_and_left_as_it_is(record, message):
    _refused(_stored({KEY: record}), message)


@pytest.mark.parametrize(
    ("value", "message"),
    [
        ([], "invalid clearance storage"),
        ({"version": 3, "records": {}}, "invalid clearance storage"),
        ({"version": True, "records": {}}, "invalid clearance storage"),
        ({"version": 2, "records": []}, "invalid clearance storage"),
        ({"version": 2, "records": {"C" * 64: RESERVATION}}, "invalid clearance binding"),
        ({"version": 2, "records": {"c" * 63: RESERVATION}}, "invalid clearance binding"),
        ({"version": 2, "records": {f"{n:064x}": RESERVATION for n in range(257)}}, "clearance storage limit"),
    ],
    ids=[
        "not_an_object",
        "unknown_version",
        "boolean_version",
        "records_not_an_object",
        "upper_key",
        "short_key",
        "257",
    ],
)
def test_a_damaged_store_is_refused_and_left_as_it_is(value, message):
    _refused(_write(value), message)


def test_a_store_over_one_mebibyte_is_not_read():
    path = data_dir() / "protection.enc"
    path.write_bytes(b"x" * (1024 * 1024 + 1))
    before = path.read_bytes()
    with pytest.raises(ValueError, match="size"):
        store.read("site", ORIGIN, settings(), now=time.time())
    assert path.read_bytes() == before


def test_a_store_that_would_pass_its_limits_is_never_written():
    path = _stored({KEY: RESERVATION})
    before = path.read_bytes()
    with pytest.raises(ValueError, match="clearance storage limit"):
        store._save({f"{n:064x}": RESERVATION for n in range(257)})
    # 256 records that each carry a large clearance: more than 1 MiB once encrypted.
    large = {**RESERVATION, "clearance": {"cookies": {"cf_clearance": "x" * 4096}}}
    with pytest.raises(ValueError, match="clearance storage limit"):
        store._save({f"{n:064x}": large for n in range(256)})
    assert path.read_bytes() == before


def test_a_redirected_data_folder_is_refused(tmp_path, monkeypatch):
    elsewhere = tmp_path / "elsewhere"
    elsewhere.mkdir()
    link = tmp_path / "redirected"
    if sys.platform == "win32":
        import _winapi

        _winapi.CreateJunction(str(elsewhere), str(link))
    else:
        try:
            os.symlink(elsewhere, link, target_is_directory=True)
        except (OSError, NotImplementedError) as exc:
            pytest.skip(f"symbolic links unavailable: {exc}")
    monkeypatch.setenv("TOW_HOME", str(link))
    with pytest.raises(ValueError, match="redirected clearance storage"):
        store.read("site", ORIGIN, settings(), now=time.time())
    with pytest.raises(ValueError, match="redirected clearance storage"):
        store.reserve("site", ORIGIN, settings(), now=time.time())
    assert not (elsewhere / "protection.enc").exists()


@pytest.mark.parametrize("now", [float("nan"), float("inf"), -1.0, 253402300800.0])
def test_an_impossible_observation_time_is_refused(now):
    provider = settings()["providers"][0]
    with pytest.raises(ValueError, match="invalid clearance observation time"):
        store.correct_clock("site", ORIGIN, [provider], now=now)
    with pytest.raises(ValueError, match="invalid clearance observation time"):
        store.service_states([provider], now=now)
    with pytest.raises(ValueError, match="invalid clearance service observation"):
        store.note_service(provider, "busy", now=now)
    with pytest.raises(ValueError, match="invalid clearance retry interval"):
        store.note_service(provider, "busy", now=1000, retry_after=now if now != -1.0 else -1)
    assert not (data_dir() / "protection.enc").exists()


def _full(age: float, now: float) -> dict:
    return {
        f"{n:064x}": {**SERVICE, "attempt_at": now - age, "lease_until": now - age, "service_retry_at": now - age + 30}
        for n in range(256)
    }


def test_a_full_store_makes_room_only_by_dropping_records_older_than_a_day():
    now = 1_800_000_000.0
    provider = settings()["providers"][0]
    path = _stored(_full(10, now))
    before = path.read_bytes()
    store.note_service(provider, "busy", now=now)  # every record is recent: nothing is dropped
    assert path.read_bytes() == before
    assert store.service_states([provider], now=now)["first"]["status"] == "unknown"
    _stored(_full(86400, now))
    store.note_service(provider, "busy", now=now)
    assert len(store._load()) == 1
    assert store.service_states([provider], now=now)["first"]["status"] == "busy"


def _reset_refused(message: str) -> None:
    path = data_dir() / "protection.enc"
    before = path.read_bytes()
    with pytest.raises(ValueError, match=message):
        store.reset()
    assert path.read_bytes() == before


def test_reset_refuses_a_temp_folder_outside_the_data_folder(tmp_path, monkeypatch):
    _stored({KEY: RESERVATION})
    monkeypatch.setattr(store, "tmp_dir", lambda create=False: tmp_path / "other" / "tmp")
    _reset_refused("clearance reset folder refused")


def test_reset_refuses_a_temp_folder_that_is_a_file():
    _stored({KEY: RESERVATION})
    (data_dir() / "tmp").write_bytes(b"not a folder")
    _reset_refused("clearance reset folder refused")


def test_reset_refuses_a_store_that_changed_while_it_was_read(monkeypatch):
    path = _stored({KEY: RESERVATION})
    real = store.read_regular_bytes

    def read_then_change(target, limit, **kwargs):
        raw = real(target, limit, **kwargs)
        if Path(target) == path:
            _stored({KEY: {**RESERVATION, "attempt_at": 1001}})
        return raw

    monkeypatch.setattr(store, "read_regular_bytes", read_then_change)
    with pytest.raises(ValueError, match="clearance reset source changed"):
        store.reset()
    assert path.exists()
    assert not any((data_dir() / "tmp").glob("protection-reset-*"))


def test_reset_refuses_a_kept_copy_that_does_not_read_back(monkeypatch):
    path = _stored({KEY: RESERVATION})
    before = path.read_bytes()
    published = []

    def publish_other_bytes(source, target):
        target.write_bytes(b"something else")
        published.append(target)

    monkeypatch.setattr(store, "current_platform", lambda: SimpleNamespace(publish_exclusive=publish_other_bytes))
    with pytest.raises(ValueError, match="clearance reset read-back failed"):
        store.reset()
    assert path.read_bytes() == before  # the store stays: its copy is not the same
    assert len(published) == 1
