"""One state with every kind of record TOW keeps, each made by the code that makes it, still gives
a restore point and its import preview: a field written under a name the restore point's
credential check refuses blocks every restore point, the update from Settings and every
restore (1.23.0's ``content_token``, 1.31.0's mirror ``token``)."""

from __future__ import annotations

import time

from fastapi.testclient import TestClient
from test_client_verification import _run, clock, world  # noqa: F401 - the fixtures are used by name

from tow import content, mirrors, restore_points
from tow.bundle import import_bundle
from tow.bundle.secret_scan import secret_field_path
from tow.config import load_config, save_config
from tow.notifiers import outbox
from tow.paths import download_history_path
from tow.store import load_download_history, load_state, save_state
from tow.web import app, services
from tow.web.routes_clients import _client_answered


def _prepared_topic(monkeypatch) -> str:
    """A topic added with the file picker, waiting for its first add (the web form)."""
    from test_content import blob

    from tow.clients.factory import client_configuration
    from tow.guess import canon_watch_url

    url = canon_watch_url("http://rutor.info/torrent/7654321/show-b")
    client_id = str(client_configuration(load_config(), None)["id"])
    monkeypatch.setattr(services, "run_check", lambda **_kwargs: {"results": []})
    monkeypatch.setattr("tow.web.services.guess_topic_title", lambda *_args, **_kwargs: "Show B")
    web = TestClient(app, headers={"Origin": "http://127.0.0.1"})
    token = content.prepare(blob(), url, client_id)["token"]
    form = {"url": url, "title": "Show B", "save_path": "D:/TV", "selection_mode": "exact", "selection_indices": "[1]"}
    web.post("/topics/add", data={**form, "content_token": token}, follow_redirects=False)
    added = next(topic for topic in load_state()["topics"] if topic.get("url") == url)
    assert added["hash"] is None
    return str(added["id"])


def test_a_state_with_every_kind_of_record_gives_a_restore_point(world, clock, monkeypatch):  # noqa: F811
    client, _tracker, _sent = world
    cfg = load_config()  # the web pages see the client the check's world names ("main")
    save_config({**cfg, "client": {**cfg["client"], "id": "main"}})
    # A torrent handed to its client that is still checking files (a receipt), then gone from the
    # client long enough to be blocked (missing_since, blocked).
    _run()
    assert load_state()["topics"][0]["verification_pending"]["receipt"]
    client.torrents.clear()
    for minutes in (0, 10):
        clock["minutes"] = minutes
        _run(space_only=True)
    pending = load_state()["topics"][0]["verification_pending"]
    assert pending.get("missing_since")
    assert pending.get("blocked")

    _prepared_topic(monkeypatch)

    # Mirrors: failures in a row (>= 2), a cooldown, a sign-in refusal, the answers' order.
    started = time.time()
    state = load_state()
    bucket = mirrors._bucket(state, "rutor")
    for n in range(3):
        mirrors._note_failure(bucket, "https://a.example", "transport", status=503, token=started + n, counts=True)
    mirrors._note_failure(bucket, "https://b.example", "auth", status=200, token=started + 4, counts=False)
    mirrors._observe(bucket, "https://c.example", "ok", 200, token=started + 5)
    save_state(state)
    assert bucket["cool"]
    assert bucket["fail"]["https://a.example"] >= 2

    # A messenger delivery holding its lease; a client Settings' "Check" found not answering.
    state = load_state()
    state["notify_outbox"] = {"ntfy": {"items": [{"id": "a", "text": "synthetic"}]}}
    save_state(state)
    assert outbox._claim("ntfy", {"topic": "synthetic"}, outbox._new_token())[0] == "send"
    _client_answered("main", answered=False)
    assert load_state()["health"].get("ping_failed")

    # A deleted topic's undo record.
    web = TestClient(app, headers={"Origin": "http://127.0.0.1"})
    removed = load_state()["topics"][-1]["id"]
    web.post(f"/topics/{removed}/delete", follow_redirects=False)
    assert load_state()["undo"]["kind"] == "topic"

    state = {"schema_version": 2, **load_state()}
    history = load_download_history() if download_history_path().is_file() else {}
    assert secret_field_path("state.json", state) is None
    assert secret_field_path("download_history.json", history) is None
    point = restore_points.create_restore_point()
    assert import_bundle(restore_points.point_path(point["id"]), restore_points._passphrase(), apply=False)
