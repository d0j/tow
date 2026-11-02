"""A completed check must not publish file recovery for an owner's newer context."""

from __future__ import annotations

from copy import deepcopy
from types import SimpleNamespace

import pytest

from tow import check, file_presence
from tow.check import reconcile, run
from tow.clients import factory
from tow.config import load_config, save_config
from tow.storage import locks
from tow.store import load_state, save_download_history, save_state


def setup_run(monkeypatch, tmp_path, transition):
    config = load_config()
    config.pop("clients", None)
    config["client"] = {"id": "main", "kind": "qbittorrent"}
    save_config(config)
    topic = {
        "id": "synthetic-topic",
        "title": "Synthetic Show",
        "hash": "a" * 40,
        "client_id": "main",
        "save_path": str(tmp_path),
        "selection": {"mode": "all"},
        "selection_hash": "a" * 40,
        "selection_verified": True,
    }
    if transition == "restored":
        file_presence.observe(topic, {"missing_files": 3, "unverified_files": 0}, client_id="main", at="first")
    save_state({"topics": [topic]})
    save_download_history({"schema_version": 1, "topics": {}})
    client = SimpleNamespace(client_id="main", client_kind="fake", ping=lambda: "ok")
    monkeypatch.setattr(factory, "from_secrets", lambda *_args, **_kwargs: client)
    monkeypatch.setattr(run, "remote_clients", lambda *_args: frozenset())
    monkeypatch.setattr(run, "load_trackers", lambda _cfg: {})
    monkeypatch.setattr(
        reconcile,
        "reconcile_topic",
        lambda *_args, **_kwargs: {
            "events": ["new_file"],
            "summary": {},
            "missing_files": 0 if transition == "restored" else 3,
            "unverified_files": 0,
        },
    )
    events = []
    monkeypatch.setattr(
        run,
        "log_event",
        lambda kind, **fields: events.append((kind, fields, getattr(locks._PROCESS_LOCK_STATE, "depth", 0))),
    )
    return topic, events


def mutate_owner(change):
    state = load_state()
    if change == "removed":
        state["topics"] = []
    else:
        topic = state["topics"][0]
        updates = {
            "generation": {"file_observation_revision": "new-endpoint-generation"},
            "selection": {"selection": {"mode": "episodes", "value": "4-5"}},
            "folder": {"save_path": str(topic["save_path"]) + "/new"},
            "hash": {"hash": "b" * 40},
        }
        topic.update(updates[change])
    save_state(state)


@pytest.mark.parametrize("transition", ["missing", "restored"])
@pytest.mark.parametrize(
    ("change", "when"),
    [
        ("generation", "before"),
        ("selection", "before"),
        ("folder", "before"),
        ("generation", "after"),
        ("selection", "after"),
        ("folder", "after"),
        ("hash", "after"),
        ("removed", "after"),
    ],
)
def test_actual_run_discards_only_presence_events_of_a_changed_context(monkeypatch, tmp_path, transition, change, when):
    _topic, events = setup_run(monkeypatch, tmp_path, transition)
    real_commit = run._commit_run_state

    def interleaved(*args, **kwargs):
        if when == "before":
            mutate_owner(change)
        real_commit(*args, **kwargs)
        if when == "after":
            mutate_owner(change)

    monkeypatch.setattr(run, "_commit_run_state", interleaved)

    check.run_check(apply=True, notify=False, progress_only=True, how="test")

    assert not any(kind in {"files_missing", "files_restored"} for kind, _fields, _depth in events)
    assert any(kind == "new_file" for kind, _fields, _depth in events)  # historical observations retain their contract
    assert all("observation_binding" not in fields for _kind, fields, _depth in events)


@pytest.mark.parametrize("transition", ["missing", "restored"])
def test_actual_run_publishes_current_presence_after_commit_under_the_data_lock(monkeypatch, tmp_path, transition):
    _topic, events = setup_run(monkeypatch, tmp_path, transition)

    check.run_check(apply=True, notify=False, progress_only=True, how="test")

    presence = [(kind, fields, depth) for kind, fields, depth in events if kind in {"files_missing", "files_restored"}]
    assert len(presence) == 1
    assert presence[0][0] == ("files_missing" if transition == "missing" else "files_restored")
    assert presence[0][2] > 0  # owner changes cannot fit between comparison and append
    assert "observation_binding" not in presence[0][1]


@pytest.mark.parametrize("transition", ["missing", "restored"])
def test_failed_commit_publishes_no_presence_transition(monkeypatch, tmp_path, transition):
    _topic, events = setup_run(monkeypatch, tmp_path, transition)
    monkeypatch.setattr(run, "save_state", lambda _state: (_ for _ in ()).throw(OSError("synthetic write refusal")))

    with pytest.raises(OSError, match="synthetic write refusal"):
        check.run_check(apply=True, notify=False, progress_only=True, how="test")

    assert not any(kind in {"files_missing", "files_restored"} for kind, _fields, _depth in events)


def test_changed_default_client_rejects_an_implicit_topics_old_client_event(monkeypatch, tmp_path):
    topic, _events = setup_run(monkeypatch, tmp_path, "missing")
    topic.pop("client_id")
    save_state({"topics": [topic]})
    staged = {
        "topic_id": topic["id"],
        "client_id": "main",
        "observation_binding": file_presence.binding(topic, "main"),
    }
    config = load_config()
    config["client"]["id"] = "another-default"
    save_config(config)
    output = []

    run._publish_reconcile_records([("files_missing", staged)], lambda kind, **fields: output.append((kind, fields)))

    assert output == []


def test_unreadable_current_state_suppresses_presence_but_keeps_other_records(monkeypatch, tmp_path):
    topic, _events = setup_run(monkeypatch, tmp_path, "missing")
    staged = {"topic_id": topic["id"], "client_id": "main", "observation_binding": file_presence.binding(topic, "main")}
    monkeypatch.setattr(run, "load_state", lambda **_kwargs: (_ for _ in ()).throw(OSError("synthetic read refusal")))
    output = []

    run._publish_reconcile_records(
        [("files_missing", staged), ("new_file", {"topic_id": topic["id"]})],
        lambda kind, **fields: output.append((kind, fields)),
    )

    assert output == [("new_file", {"topic_id": topic["id"]})]


def test_records_without_presence_keep_the_original_publication_path(monkeypatch):
    monkeypatch.setattr(run, "load_state", lambda **_kwargs: pytest.fail("no new state read for historical events"))
    original = [("new_file", {"topic_id": "synthetic-topic"})]
    output = []

    run._publish_reconcile_records(deepcopy(original), lambda kind, **fields: output.append((kind, fields)))

    assert output == original
