"""The state file is parsed again only when it changed; every caller still gets its own copy."""

from pathlib import Path

import pytest

from tow import store
from tow.store import StoreCorruptionError, load_state, save_state, write_generation


def _count_parses(monkeypatch) -> list[Path]:
    parsed: list[Path] = []
    original = store.load_json

    def counting(path, *args, **kwargs):
        parsed.append(Path(path))
        return original(path, *args, **kwargs)

    monkeypatch.setattr(store, "load_json", counting)
    return parsed


def test_an_unchanged_state_file_is_parsed_once(monkeypatch):
    save_state({"topics": [{"id": "a"}]})
    parsed = _count_parses(monkeypatch)

    for _ in range(5):
        assert load_state()["topics"] == [{"id": "a"}]

    assert len(parsed) == 1


def test_every_caller_gets_its_own_copy():
    save_state({"topics": [{"id": "a", "selection": {"mode": "all"}}]})
    first = load_state()
    first["topics"][0]["selection"]["mode"] = "changed"
    first["topics"].append({"id": "b"})
    first["new"] = True

    second = load_state()

    assert second == {"topics": [{"id": "a", "selection": {"mode": "all"}}], "mirrors": {}}
    assert second["topics"] is not first["topics"]


def test_a_save_is_seen_by_the_next_read(monkeypatch):
    save_state({"topics": [{"id": "a"}]})
    load_state()
    parsed = _count_parses(monkeypatch)

    save_state({"topics": [{"id": "b"}]})

    assert load_state()["topics"] == [{"id": "b"}]
    assert len(parsed) == 1


def test_a_file_changed_in_place_is_read_again():
    save_state({"topics": [{"id": "a"}]})
    load_state()
    Path(store.state_path()).write_text('{"topics": [{"id": "edited-by-hand"}]}', encoding="utf-8")

    assert load_state()["topics"] == [{"id": "edited-by-hand"}]


def test_a_corrupt_file_after_a_cached_read_is_still_refused():
    save_state({"topics": [{"id": "a"}]})
    load_state()
    Path(store.state_path()).write_text("{not json", encoding="utf-8")

    with pytest.raises(StoreCorruptionError):
        load_state()


def test_another_data_folder_is_not_served_from_the_cache(monkeypatch, tmp_path):
    save_state({"topics": [{"id": "here"}]})
    load_state()
    other = tmp_path / "other"
    other.mkdir()
    monkeypatch.setenv("TOW_HOME", str(other))

    assert load_state()["topics"] == []


def test_the_write_counter_moves_with_every_written_file():
    before = write_generation()
    save_state({"topics": []})
    store.atomic_write_text(Path(store.state_path()).with_name("other.txt"), "x")

    assert write_generation() == before + 2
