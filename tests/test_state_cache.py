"""The state and download history files are parsed again only when they changed; every caller
still gets its own copy."""

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


class _State:
    path = staticmethod(store.state_path)
    load = staticmethod(load_state)

    @staticmethod
    def save(name):
        save_state({"topics": [{"id": name}]})

    @staticmethod
    def names(data):
        return [topic["id"] for topic in data["topics"]]

    hand_edit = '{"topics": [{"id": "edited-by-hand"}]}'


class _History:
    path = staticmethod(store.download_history_path)
    load = staticmethod(store.load_download_history)

    @staticmethod
    def save(name):
        store.save_download_history({"schema_version": 1, "topics": {name: {"items": {}}}})

    @staticmethod
    def names(data):
        return list(data["topics"])

    hand_edit = '{"topics": {"edited-by-hand": {"items": {}}}}'


STORES = [pytest.param(_State, id="state"), pytest.param(_History, id="history")]


@pytest.mark.parametrize("kind", STORES)
def test_an_unchanged_file_is_parsed_once(monkeypatch, kind):
    kind.save("a")
    parsed = _count_parses(monkeypatch)

    for _ in range(5):
        assert kind.names(kind.load()) == ["a"]

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


def test_every_history_reader_gets_its_own_copy():
    store.save_download_history({"schema_version": 1, "topics": {"a": {"items": {"x": {"status": "new"}}}}})
    first = store.load_download_history()
    first["topics"]["a"]["items"]["x"]["status"] = "changed"
    first["topics"]["b"] = {}

    second = store.load_download_history()

    assert second == {"schema_version": 1, "topics": {"a": {"items": {"x": {"status": "new"}}}}}


@pytest.mark.parametrize("kind", STORES)
def test_a_save_is_seen_by_the_next_read(monkeypatch, kind):
    kind.save("a")
    kind.load()
    parsed = _count_parses(monkeypatch)

    kind.save("b")

    assert kind.names(kind.load()) == ["b"]
    assert len(parsed) == 1


@pytest.mark.parametrize("kind", STORES)
def test_a_file_changed_in_place_is_read_again(kind):
    kind.save("a")
    kind.load()
    Path(kind.path()).write_text(kind.hand_edit, encoding="utf-8")

    assert kind.names(kind.load()) == ["edited-by-hand"]


@pytest.mark.parametrize("kind", STORES)
def test_a_corrupt_file_after_a_cached_read_is_still_refused(kind):
    kind.save("a")
    kind.load()
    Path(kind.path()).write_text("{not json", encoding="utf-8")

    with pytest.raises(StoreCorruptionError):
        kind.load()


@pytest.mark.parametrize("kind", STORES)
def test_another_data_folder_is_not_served_from_the_cache(monkeypatch, tmp_path, kind):
    kind.save("here")
    kind.load()
    other = tmp_path / "other"
    other.mkdir()
    monkeypatch.setenv("TOW_HOME", str(other))

    assert kind.names(kind.load()) == []


def test_the_write_counter_moves_with_every_written_file():
    before = write_generation()
    save_state({"topics": []})
    store.atomic_write_text(Path(store.state_path()).with_name("other.txt"), "x")

    assert write_generation() == before + 2
