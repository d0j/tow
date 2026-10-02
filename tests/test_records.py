"""The typed records (tow.records) stay in step with the fields the code really uses."""

from __future__ import annotations

import typing

from tow import check_steps, records


def _fields(kind: type) -> set[str]:
    return set(typing.get_type_hints(kind))


def test_check_and_owner_fields_are_topic_fields():
    topic = _fields(records.Topic)
    assert set(check_steps._CHECK_OWNED_FIELDS) <= topic
    assert set(check_steps._CHECK_CLEARABLE_FIELDS) <= topic
    assert set(check_steps.OWNER_FIELDS) <= topic


def test_every_record_is_open_for_older_files():
    # A file written by an older TOW lacks newer fields: nothing may be required.
    for kind in (
        records.Topic,
        records.Health,
        records.MirrorState,
        records.NotifyStatus,
        records.HistoryItem,
        records.HistoryRecord,
        records.DownloadHistory,
        records.ErrorFields,
    ):
        assert not kind.__required_keys__, kind.__name__


def test_typed_views_of_a_loaded_state_are_the_same_dicts():
    topic = {"id": "1", "title": "Show"}
    state = {"topics": [topic, "broken"], "health": {"qbit_ok": True}, "mirrors": {"rutor": {"active": "x"}}}
    assert records.topics_of(state) == [topic]
    assert records.topics_of(state)[0] is topic
    assert records.health_of(state) is state["health"]
    assert records.mirror_of(state, "rutor") is state["mirrors"]["rutor"]
    assert records.mirror_of(state, "kinozal") is None


def test_typed_views_of_an_empty_or_broken_state():
    assert records.topics_of({}) == []
    assert records.topics_of({"topics": {"not": "a list"}}) == []
    assert records.health_of({}) == {}
    assert records.health_of({"health": "broken"}) == {}
    assert records.mirror_of({"mirrors": []}, "rutor") is None
