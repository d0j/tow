"""Store resilience: transient read errors, quarantine safety, Windows replace retries."""

from pathlib import Path

import pytest

from tow import store
from tow.store import StoreCorruptionError, StoreReadError, load_state, save_state


def _state_file() -> Path:
    return Path(store.state_path())


def test_transient_read_error_does_not_quarantine(monkeypatch):
    save_state({"topics": [{"id": "keep"}]})
    original = Path.read_bytes

    def flaky(self):
        if self.name == "state.json":
            raise PermissionError("sharing violation")
        return original(self)

    monkeypatch.setattr(Path, "read_bytes", flaky)
    with pytest.raises(StoreReadError):
        load_state()
    monkeypatch.setattr(Path, "read_bytes", original)

    assert _state_file().is_file()
    assert load_state()["topics"] == [{"id": "keep"}]


def test_corrupt_json_is_quarantined_and_never_replaced_by_empty_default():
    save_state({"topics": [{"id": "keep"}]})
    _state_file().write_text("{not json", encoding="utf-8")

    with pytest.raises(StoreCorruptionError):
        load_state()
    assert not _state_file().exists()
    assert any(p.name.startswith("state.json.corrupt-") for p in _state_file().parent.iterdir())

    with pytest.raises(StoreCorruptionError, match="quarantined"):
        load_state()


def test_missing_store_without_quarantine_still_uses_default():
    assert load_state()["topics"] == []


def test_replace_retries_while_target_is_briefly_locked(monkeypatch):
    calls = {"n": 0}
    real_replace = store.os.replace

    def busy_then_ok(src, dst):
        calls["n"] += 1
        if calls["n"] < 3:
            raise PermissionError("in use")
        return real_replace(src, dst)

    monkeypatch.setattr(store.os, "replace", busy_then_ok)
    monkeypatch.setattr(store.time, "sleep", lambda _s: None)
    save_state({"topics": [{"id": "written"}]})

    assert calls["n"] == 3
    assert load_state()["topics"] == [{"id": "written"}]
