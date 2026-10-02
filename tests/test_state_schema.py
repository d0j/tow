"""state.json carries its data version: a newer TOW's file is refused, an older one migrated."""

from __future__ import annotations

import argparse
import json

import pytest

from tow import cli, store
from tow.paths import state_path
from tow.store import STATE_SCHEMA_VERSION, StateVersionError, check_state_version, load_state, save_state


def _raw() -> dict:
    return json.loads(state_path().read_text(encoding="utf-8"))


def _write(data: dict) -> None:
    state_path().parent.mkdir(parents=True, exist_ok=True)
    state_path().write_text(json.dumps(data), encoding="utf-8")


def test_the_version_is_written_first_and_hidden_from_readers():
    save_state({"topics": [{"id": "1"}], "mirrors": {}})
    raw = _raw()
    assert next(iter(raw)) == "schema_version"
    assert raw["schema_version"] == STATE_SCHEMA_VERSION == 1
    assert load_state() == {"topics": [{"id": "1"}], "mirrors": {}}


def test_a_caller_cannot_write_another_version():
    save_state({"topics": [], "mirrors": {}, "schema_version": 99})
    assert _raw()["schema_version"] == STATE_SCHEMA_VERSION


def test_a_state_without_a_version_is_read_and_gets_one_on_the_next_save():
    _write({"topics": [{"id": "old"}], "mirrors": {}})  # TOW 1.18 and before
    state = load_state()
    assert state["topics"] == [{"id": "old"}]
    save_state(state)
    assert _raw()["schema_version"] == STATE_SCHEMA_VERSION


def test_a_newer_state_is_refused_and_left_as_it_is():
    newer = {"schema_version": STATE_SCHEMA_VERSION + 1, "topics": [{"id": "x", "new_field": 1}], "mirrors": {}}
    _write(newer)
    before = state_path().read_bytes()
    with pytest.raises(StateVersionError) as caught:
        load_state()
    assert caught.value.code == "store.state_newer"
    assert "newer TOW" in caught.value.text("en")
    assert isinstance(caught.value, store.StoreCorruptionError)  # every reader fails closed
    with pytest.raises(StateVersionError):
        check_state_version()
    assert state_path().read_bytes() == before
    assert not list(state_path().parent.glob("state.json.corrupt-*"))  # not quarantined either


@pytest.mark.parametrize("version", ["1", -1, True, 1.5, None])
def test_a_version_this_tow_cannot_read_is_refused(version):
    _write({"schema_version": version, "topics": [], "mirrors": {}})
    with pytest.raises(StateVersionError) as caught:
        load_state()
    assert caught.value.code == "store.state_bad_version"


def test_the_startup_check_leaves_a_missing_or_damaged_file_to_its_reader():
    check_state_version()  # no state.json yet
    state_path().parent.mkdir(parents=True, exist_ok=True)
    state_path().write_text("{ not json", encoding="utf-8")
    check_state_version()  # quarantine and restore points handle it where it is read
    assert state_path().read_text(encoding="utf-8") == "{ not json"


def test_tow_refuses_to_start_on_a_newer_state(monkeypatch, capsys):
    _write({"schema_version": STATE_SCHEMA_VERSION + 1, "topics": [], "mirrors": {}})
    started = []
    monkeypatch.setattr("tow.supervisor.run_supervisor", lambda *a, **k: started.append(True) or 0)
    with pytest.raises(StateVersionError):
        cli._cmd_run(argparse.Namespace())
    assert started == []
    assert cli.main(["check"]) == 3
    assert "tow check: StateVersionError: " in capsys.readouterr().err  # a sentence, not a traceback
    assert state_path().is_file()


def test_an_export_of_a_newer_tow_is_not_imported():
    from tow.bundle import ExportImportError, _validate_state_schema

    _validate_state_schema({"schema_version": STATE_SCHEMA_VERSION, "topics": [], "mirrors": {}})
    _validate_state_schema({"topics": [], "mirrors": {}})  # an older export
    with pytest.raises(ExportImportError):
        _validate_state_schema({"schema_version": STATE_SCHEMA_VERSION + 1, "topics": [], "mirrors": {}})
