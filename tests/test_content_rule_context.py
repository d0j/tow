from __future__ import annotations

import pytest
from fastapi.testclient import TestClient
from helpers import multi_file_torrent

from tow import content
from tow.check import _selection_plan
from tow.selection import normalize_policy
from tow.store import load_state, save_state
from tow.torrent import parse_torrent_metadata
from tow.web import app, services

URL = "https://tracker.example/topic/1"


def blob():
    return multi_file_torrent(b"Show", [{b"path": [b"Show.E01.mkv"], b"length": 100}])


def preview(data, **fields):
    snapshot = content.prepare(data, URL, "main")
    client = TestClient(app, headers={"Origin": "http://127.0.0.1"})
    return client.post(
        "/content/resolve",
        data={"token": snapshot["token"], "url": URL, "client_id": "main", "mode": "episodes", **fields},
    )


@pytest.mark.parametrize(
    ("topic", "title", "context"),
    [
        (None, "Show - Season 2", "Show - Season 2"),
        ({"tracker_title": "Show - Season 2", "title": "Show"}, "Custom name S09", "Show - Season 2"),
        ({"title": "Old name S01"}, "Show - Season 2", "Show - Season 2"),
        ({"title": "Show - Season 2"}, "", "Show - Season 2"),
        ({"title": "Show - Season 2"}, "   ", "Show - Season 2"),
        ({"url": URL + "0", "tracker_title": "Old source S09"}, "Show - Season 2", "Show - Season 2"),
        ({"client_id": "other", "tracker_title": "Old client S09"}, "Show - Season 2", "Show - Season 2"),
    ],
)
def test_preview_uses_the_checks_title_context_without_network_or_state_writes(monkeypatch, topic, title, context):
    saved = {"id": "ctx", "url": URL, "client_id": "main", **(topic or {})}
    save_state({"topics": [saved] if topic is not None else []})
    before = load_state()
    monkeypatch.setattr(services, "prepare_content", lambda *_args: pytest.fail("preview fetched tracker"))
    monkeypatch.setattr(services, "run_check", lambda **_kwargs: pytest.fail("preview ran a check"))
    monkeypatch.setattr(services, "save_state", lambda *_args: pytest.fail("preview changed state"))
    data = blob()
    response = preview(data, topic_id="ctx" if topic is not None else "", title=title, value="S02E01")
    assert response.status_code == 200, response.json()
    plan = _selection_plan({}, parse_torrent_metadata(data), normalize_policy("episodes", "S02E01"), context, old="")
    assert response.json()["indices"] == list(plan.selected_indices) == [0]
    assert response.headers["cache-control"] == "no-store"
    assert load_state() == before


def test_deleted_topic_context_refuses_without_reviving_a_watch():
    save_state({"topics": []})
    response = preview(blob(), topic_id="deleted", title="Show Season 2", value="S02E01")
    assert response.status_code == 400
    assert response.json()["code"] == "content.changed"
    assert load_state()["topics"] == []


@pytest.mark.parametrize("lifecycle", ["watch", "once"])
def test_future_episode_preview_matches_watch_and_once_semantics(lifecycle):
    save_state({"topics": []})
    data = blob()
    response = preview(data, title="Show Season 2", value="S02E03", tracking_mode=lifecycle)
    if lifecycle == "watch":
        assert response.status_code == 200
        assert response.json()["indices"] == []
        assert response.json()["waiting"]
        row = {}
        assert (
            _selection_plan(
                row, parse_torrent_metadata(data), normalize_policy("episodes", "S02E03"), "Show Season 2", old=""
            )
            is None
        )
        assert row["status"] == "skipped"
    else:
        assert response.status_code == 400
        assert response.json()["code"] == "selection.not_out_yet"
    assert load_state()["topics"] == []


def test_preview_context_cannot_turn_multiseason_ambiguity_into_a_guess():
    data = multi_file_torrent(
        b"Show",
        [
            {b"path": [b"Show.S01E01.mkv"], b"length": 100},
            {b"path": [b"Show.S02E01.mkv"], b"length": 100},
        ],
    )
    response = preview(data, title="Show Season 3", value="1")
    assert response.status_code == 400
    assert response.json()["code"] == "selection.ambiguous"
    response = preview(data, title="Show Season 3", value="S02E01")
    assert response.status_code == 200
    assert response.json()["indices"] == [1]
