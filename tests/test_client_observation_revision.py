"""Changing a connection under the same client id cannot retain that endpoint's evidence."""

from __future__ import annotations

from copy import deepcopy

import pytest
from fastapi.testclient import TestClient

from tow import check_steps, file_presence
from tow.config import load_config, save_config
from tow.store import load_secrets, load_state, save_secrets, save_state
from tow.web import app

CONNECTION = {"host": "old.example", "port": 8080, "username": "owner", "password": "synthetic-password"}


def seed(*, legacy=False):
    config = load_config()
    if legacy:
        config.pop("clients", None)
        config["client"] = {"id": "default", "kind": "qbittorrent"}
        ident = "default"
        save_secrets({"qbittorrent": deepcopy(CONNECTION)})
    else:
        ident = "main"
        config["clients"] = [
            {"id": ident, "kind": "qbittorrent", "default": True},
            {"id": "other", "kind": "qbittorrent"},
        ]
        save_secrets({"clients": {ident: deepcopy(CONNECTION), "other": {"host": "other.example"}}})
    save_config(config)
    topics = []
    for name, selected in (("explicit", ident), ("implicit", None), ("other", "other")):
        topic = {
            "id": name,
            "title": "Synthetic Show",
            "hash": "a" * 40,
            "save_path": "/downloads/show",
            "selection": {"mode": "all"},
        }
        if selected is not None:
            topic["client_id"] = selected
        file_presence.observe(
            topic,
            {"missing_files": 3, "unverified_files": 0},
            client_id=selected or ident,
            at="2026-09-12T20:00:00+00:00",
        )
        topics.append(topic)
    save_state({"topics": topics})
    return ident, deepcopy(topics)


def save_connection(client, ident, **changes):
    return client.post(
        "/settings/client",
        data={"client_id": ident, "kind": "qbittorrent", **CONNECTION, **changes},
        follow_redirects=False,
    )


@pytest.mark.parametrize("changes", [{"host": "new.example"}, {"port": 8081}, {"username": "other-owner"}])
def test_endpoint_change_invalidates_only_explicit_and_default_topics(changes):
    ident, before = seed()
    client = TestClient(app, headers={"Origin": "http://127.0.0.1"})

    response = save_connection(client, ident, **changes)

    assert response.status_code == 303
    state = load_state()
    assert state["undo"]["file_observation_client_id"] == ident
    for topic in state["topics"][:2]:
        assert "files_missing" not in topic
        assert topic["file_observation_revision"]
        assert file_presence.count(topic, load_config()) == 0
    assert state["topics"][2] == before[2]


def test_password_only_save_preserves_current_presence_evidence():
    ident, before = seed()
    client = TestClient(app, headers={"Origin": "http://127.0.0.1"})

    assert save_connection(client, ident, password="new-synthetic-password").status_code == 303

    assert load_state()["topics"] == before
    assert "file_observation_client_id" not in load_state()["undo"]
    assert load_secrets()["clients"][ident]["password"] == "new-synthetic-password"


def test_legacy_default_connection_invalidates_implicit_topics_too():
    ident, before = seed(legacy=True)
    client = TestClient(app, headers={"Origin": "http://127.0.0.1"})

    assert save_connection(client, ident, host="new.example").status_code == 303

    assert all("files_missing" not in topic for topic in load_state()["topics"][:2])
    assert load_state()["topics"][2] == before[2]
    assert load_secrets()["qbittorrent"]["host"] == "new.example"


def test_old_inflight_check_cannot_restore_endpoint_evidence_after_connection_save():
    ident, before = seed()
    client = TestClient(app, headers={"Origin": "http://127.0.0.1"})
    started = {topic["id"]: check_steps.owner_fields(topic) for topic in before}
    assert save_connection(client, ident, host="new.example").status_code == 303
    disk = load_state()
    revisions = [topic.get("file_observation_revision") for topic in disk["topics"]]
    edited = {topic["id"]: check_steps.changed_owner_fields(started[topic["id"]], topic) for topic in disk["topics"]}

    dropped = check_steps.merge_check_results(disk, {"topics": before}, edited=edited)

    assert dropped == {"explicit", "implicit"}
    assert [topic.get("file_observation_revision") for topic in disk["topics"]] == revisions
    assert all("files_missing" not in topic for topic in disk["topics"][:2])
    assert disk["topics"][2] == before[2]


def test_undo_changes_the_generation_again_and_discards_the_intermediate_endpoints_evidence():
    ident, before = seed()
    client = TestClient(app, headers={"Origin": "http://127.0.0.1"})
    assert save_connection(client, ident, host="new.example").status_code == 303
    intermediate = load_state()
    revisions = [topic.get("file_observation_revision") for topic in intermediate["topics"]]
    for topic in intermediate["topics"][:2]:
        file_presence.observe(
            topic,
            {"missing_files": 1, "unverified_files": 0},
            client_id=ident,
            at="2026-09-12T20:01:00+00:00",
        )
    save_state(intermediate)
    started = {topic["id"]: check_steps.owner_fields(topic) for topic in intermediate["topics"]}

    assert client.post("/undo", follow_redirects=False).status_code == 303

    restored = load_state()
    assert load_secrets()["clients"][ident] == CONNECTION
    for index, topic in enumerate(restored["topics"][:2]):
        assert "files_missing" not in topic
        assert topic["file_observation_revision"] != revisions[index]
    assert restored["topics"][2] == before[2]
    edited = {
        topic["id"]: check_steps.changed_owner_fields(started[topic["id"]], topic) for topic in restored["topics"]
    }
    assert check_steps.merge_check_results(restored, intermediate, edited=edited) == {"explicit", "implicit"}
    assert all("files_missing" not in topic for topic in restored["topics"][:2])


def test_failed_connection_save_rolls_back_both_endpoint_and_absence_evidence(monkeypatch):
    ident, before = seed()
    secrets_before = load_secrets()
    client = TestClient(app, headers={"Origin": "http://127.0.0.1"})

    def disk_full(_state):
        raise OSError("synthetic disk full")

    with monkeypatch.context() as patch:
        patch.setattr("tow.store.save_state", disk_full)
        response = save_connection(client, ident, host="new.example")

    assert response.status_code == 303
    assert load_secrets() == secrets_before
    assert load_state()["topics"] == before
