"""Recent messages shared by every signed-in device (tow.messages_feed, GET /messages.json, POST
/messages/read and /messages/clear): the event -> message matrix, cursors in append order across
rotation, the bounded feed, the shared markers in data/ui.json and their storage policy, the
scrubbed projection a device on the network gets, and update results reconciled from the job."""

from __future__ import annotations

import importlib.util
import json
import uuid
from contextlib import suppress
from datetime import UTC, datetime, timedelta
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from helpers import open_network

from tow import log, messages_feed
from tow.paths import data_dir, ui_state_path
from tow.web import app

ROOT = Path(__file__).resolve().parents[1]
BASE = "http://127.0.0.1:8787"
LOCAL = {"Origin": BASE}


def _raw(kind: str, *, ago: float = 0.0, **fields: object) -> str:
    """One event as another process appends it, at a chosen time."""
    moment = (datetime.now(UTC) - timedelta(seconds=ago)).isoformat()
    event_id = uuid.uuid4().hex
    record = {"ts": moment, "created_at": moment, "event_id": event_id, "kind": kind, **fields}
    with log.log_path().open("a", encoding="utf-8") as handle:
        handle.write(json.dumps(record, ensure_ascii=False) + "\n")
    return event_id


def _feed(lang: str = "en") -> dict:
    snapshot = messages_feed.feed().refresh()
    return messages_feed.render(snapshot.projection, lang=lang)


def _ids(payload: dict) -> list[str]:
    return [item["id"] for item in payload["items"]]


def _rotate() -> None:
    log.rotate_files(log.log_path(), log.BACKUPS)


# --------------------------------------------------------------------------- the append primitive


def test_append_event_returns_the_event_id_and_log_event_stays_boolean():
    event_id = log.append_event("topic_add", topic="t1", title="Show A", how="manual")
    assert isinstance(event_id, str)
    assert len(event_id) == 32
    assert log.read_events(limit=1)[0]["event_id"] == event_id
    assert log.log_event("topic_pause", topic="t1", how="manual") is True


def test_a_failed_append_says_so_without_raising(monkeypatch):
    def broken():
        raise OSError("disk full")

    monkeypatch.setattr(log, "_log_file_lock", broken)
    assert log.append_event("topic_add", topic="t1") is None
    assert log.log_event("topic_add", topic="t1") is False


# --------------------------------------------------------------------------- the matrix


@pytest.mark.parametrize(
    ("kind", "fields", "category", "severity"),
    [
        ("client_added", {"topic": "t1"}, "downloads", "ok"),
        ("episode_completed", {"topic": "t1"}, "downloads", "ok"),
        ("qbit_move", {"topic": "t1", "status": "unconfirmed"}, "downloads", "warn"),
        ("client_removed", {"topic": "t1"}, "downloads", "warn"),
        ("client_add_failed", {"topic": "t1", "cls": "qbit"}, "downloads", "err"),
        ("check_fail", {"topic": "t1", "tracker": "rutor", "cls": "tracker"}, "checks", "warn"),
        ("check_fail", {"topic": "t1", "tracker": "rutor", "cls": "qbit"}, "checks", "err"),
        # Typed severity: words that read like a transport problem never make a client error amber.
        ("check_fail", {"topic": "t1", "cls": "qbit", "error": "все зеркала: all hosts failed"}, "checks", "err"),
        ("check_blocked", {"reason": "secret_gate"}, "checks", "err"),
        ("check_site_skipped", {"tracker": "rutor", "topics": 3}, "checks", "warn"),
        ("topic_edit", {"topic": "t1", "how": "manual"}, "actions", "ok"),
        ("undo", {"how": "manual"}, "actions", "ok"),
        ("undo_expired", {"how": "manual"}, "actions", "warn"),
        ("undo_fail", {"status": "restored"}, "actions", "err"),
        ("browser_auth", {"topic": "t1", "status": "verified"}, "actions", "ok"),
        ("watchdog_alert", {"alerts": ["checks are late"]}, "operations", "warn"),
        ("backup_failed", {}, "operations", "err"),
        ("settings_update_started", {"operation_id": "a" * 32, "target_version": "1.33.0"}, "updates", "info"),
        ("settings_update_refused", {"target_version": "1.33.0"}, "updates", "err"),
        ("bot_delivery_failed", {"integration_id": "telegram", "status": "queued"}, "delivery", "warn"),
        ("bot_delivery_failed", {"integration_id": "telegram", "status": "dropped"}, "delivery", "err"),
    ],
)
def test_each_message_kind_has_its_category_and_typed_severity(kind, fields, category, severity):
    _raw(kind, **fields)
    (item,) = _feed()["items"]
    assert (item["category"], item["kind"]) == (category, severity)


@pytest.mark.parametrize(
    ("kind", "fields"),
    [
        ("check", {"n": 3, "ok": 3, "how": "auto"}),  # a routine run
        ("tracker_checked", {"topic": "t1"}),  # a routine success
        ("client_add_started", {"topic": "t1"}),  # an attempt
        ("site_login", {"tracker": "rutor"}),  # a follow-up check may still put the old login back
        ("browser_auth", {"topic": "t1", "status": "session_saved"}),
        ("portable_import_applied", {}),  # a restore point may still roll it back
        ("undo_expired", {"how": "automatic"}),
        ("bot_delivery_succeeded", {"integration_id": "telegram"}),  # every successful delivery
        ("bot_delivery_started", {"integration_id": "telegram"}),
        ("protection_attempt", {"tracker": "rutor"}),
        ("content_cache_failed", {"topic": "t1"}),
        ("site_prefer", {"tracker": "rutor"}),  # also logged when nothing changed
    ],
)
def test_routine_events_attempts_and_diagnostics_are_history_only(kind, fields):
    _raw(kind, **fields)
    assert _feed()["items"] == []


def test_a_success_after_a_failure_is_a_recovery_and_only_then():
    _raw("tracker_checked", topic="t1")
    _raw("check_fail", topic="t1", tracker="rutor", cls="tracker")
    _raw("tracker_checked", topic="t2")  # another topic: routine
    recovered = _raw("tracker_checked", topic="t1")
    _raw("tracker_checked", topic="t1")  # once
    _raw("backup_failed")
    _raw("backup_created")
    _raw("backup_created")
    items = _feed()["items"]
    kinds = [(item["category"], item["kind"]) for item in items]
    assert kinds == [("operations", "ok"), ("operations", "err"), ("checks", "ok"), ("checks", "warn")]
    assert items[2]["id"] == recovered


def test_repeated_failures_of_one_site_are_one_entry():
    for topic in ("t1", "t2", "t3"):
        _raw("check_fail", ago=3000, topic=topic, tracker="rutor", cls="tracker", error="all hosts failed", how="auto")
    _raw("check_fail", ago=3000, topic="t4", tracker="kinozal", cls="tracker", how="auto")
    payload = _feed()
    assert len(payload["items"]) == 2
    rutor = payload["items"][1]
    assert rutor["subject"] == "rutor"
    assert "3 topics" in rutor["text"]
    assert rutor["href"] == "/history?group=errors"
    # A failure much later is a new problem, not the same entry.
    _raw("check_fail", topic="t1", tracker="rutor", cls="tracker", how="auto")
    assert len(_feed()["items"]) == 3


# --------------------------------------------------------------------------- the bounded feed and cursors


def test_the_feed_is_at_most_fifty_entries_of_seven_days_newest_first():
    _raw("topic_add", ago=8 * 86400, topic="old")
    first = _raw("topic_add", topic="t0")
    newest = [_raw("topic_pause", topic=f"t{n}") for n in range(1, 61)]
    payload = _feed()
    assert len(payload["items"]) == 50
    assert _ids(payload) == list(reversed(newest))[:50]
    assert first not in _ids(payload)
    assert payload["unread"] == 50


def test_an_entry_expires_without_another_event(monkeypatch):
    _raw("topic_add", ago=7 * 86400 - 60, topic="t1")
    before = _feed()
    assert len(before["items"]) == 1
    later = datetime.now(UTC).timestamp() + 120
    monkeypatch.setattr(messages_feed.time, "time", lambda: later)
    messages_feed.feed()._snapshot = None  # the next reader pass, as a later poll gets it
    after = _feed()
    assert after["items"] == []
    assert after["revision"] != before["revision"]


def test_a_cursor_follows_its_event_across_rotation_in_append_order():
    a = _raw("topic_add", topic="a")
    b = _raw("topic_add", topic="b")
    payload = _feed()
    messages_feed.feed().mark(payload["items"][0]["cursor"], clear=False)  # read up to b
    _rotate()
    c = _raw("topic_add", topic="c")
    _rotate()
    d = _raw("topic_add", topic="d")
    after = _feed()
    assert _ids(after) == [d, c, b, a]
    assert [item["read"] for item in after["items"]] == [False, False, True, True]
    assert after["unread"] == 2
    assert after["reset"] is False


def test_more_than_fifty_events_after_the_read_marker_are_all_unread():
    first = _raw("topic_add", topic="t0")
    messages_feed.feed().mark(_feed()["items"][0]["cursor"], clear=False)
    for n in range(70):
        _raw("topic_pause", topic=f"t{n}")
    payload = _feed()
    assert len(payload["items"]) == 50
    assert payload["unread"] == 50
    assert first not in _ids(payload)


def test_a_marker_whose_file_rotated_away_is_older_than_everything_retained():
    _raw("topic_add", topic="a")
    messages_feed.feed().mark(_feed()["items"][0]["cursor"], clear=True)
    for n in range(log.BACKUPS + 1):
        _rotate()
        _raw("topic_pause", topic=f"t{n}")
    payload = _feed()
    assert payload["reset"] is False
    assert payload["unread"] == log.BACKUPS + 1  # every retained one is newer than the marker


def test_a_log_that_was_replaced_says_reset_and_shows_the_current_window():
    _raw("topic_add", topic="a")
    messages_feed.feed().mark(_feed()["items"][0]["cursor"], clear=False)
    log.log_path().unlink()
    fresh = _raw("topic_add", topic="b")
    payload = _feed()
    assert payload["reset"] is True
    assert _ids(payload) == [fresh]
    assert payload["unread"] == 1


def test_a_clock_that_went_back_skips_nothing():
    _raw("topic_add", topic="a")
    messages_feed.feed().mark(_feed()["items"][0]["cursor"], clear=False)
    later_but_earlier_clock = _raw("topic_add", ago=3600, topic="b")
    payload = _feed()
    assert _ids(payload)[0] == later_but_earlier_clock  # append order, not time order
    assert payload["items"][0]["read"] is False


# --------------------------------------------------------------------------- shared markers over HTTP


def test_read_and_clear_are_shared_and_a_concurrent_event_survives_a_clear():
    phone = TestClient(app, base_url=BASE, headers=LOCAL)
    computer = TestClient(app, base_url=BASE, headers=LOCAL)
    _raw("topic_add", topic="a")
    shown = _raw("topic_add", topic="b")
    on_phone = phone.get("/messages.json").json()
    assert on_phone["unread"] == 2
    arrived = _raw("topic_pause", topic="c")  # after the phone drew its list
    cleared = phone.post("/messages/clear", data={"cursor": on_phone["cursor"]})
    assert cleared.status_code == 200
    assert cleared.headers["cache-control"] == "no-store"
    seen = computer.get("/messages.json").json()
    assert _ids(seen) == [arrived]
    assert seen["unread"] == 1
    stored = json.loads(ui_state_path().read_text(encoding="utf-8"))
    assert set(stored) == {"schema", "read_upto", "cleared_upto"}
    assert stored["cleared_upto"]["event"] == shown


def test_a_poll_never_reads_and_markers_only_move_forward():
    client = TestClient(app, base_url=BASE, headers=LOCAL)
    _raw("topic_add", topic="a")
    older = client.get("/messages.json").json()["cursor"]
    _raw("topic_add", topic="b")
    newer = client.get("/messages.json").json()
    assert newer["unread"] == 2
    assert not ui_state_path().exists()  # polling marks nothing read and writes nothing
    assert client.post("/messages/read", data={"cursor": newer["cursor"]}).json()["unread"] == 0
    assert client.post("/messages/read", data={"cursor": older}).json()["unread"] == 0  # never back


def test_a_stale_cursor_is_refused_and_writes_nothing():
    client = TestClient(app, base_url=BASE, headers=LOCAL)
    _raw("topic_add", topic="a")
    for cursor in ("", "nope", messages_feed.Pos("0" * 32, 5, "1" * 32).cursor()):
        answer = client.post("/messages/read", data={"cursor": cursor})
        assert answer.status_code == 409
        assert answer.json()["ok"] is False
    assert not ui_state_path().exists()


def test_reading_and_clearing_need_this_origin():
    _raw("topic_add", topic="a")
    cursor = TestClient(app).get("/messages.json").json()["cursor"]
    for path in ("/messages/read", "/messages/clear"):
        assert TestClient(app, base_url=BASE).post(path, data={"cursor": cursor}).status_code == 403
        refused = TestClient(app, base_url=BASE, headers={"Origin": "http://evil.example"}).post(
            path, data={"cursor": cursor}
        )
        assert refused.status_code == 403
    assert not ui_state_path().exists()


def test_the_feed_is_conditional_private_and_revisioned():
    client = TestClient(app)
    _raw("topic_add", topic="a")
    first = client.get("/messages.json")
    assert first.status_code == 200
    assert first.headers["cache-control"] == "private, no-cache"
    assert first.headers["etag"] == f'"{first.json()["revision"]}"'
    again = client.get("/messages.json", headers={"If-None-Match": first.headers["etag"]})
    assert again.status_code == 304
    assert again.headers["etag"] == first.headers["etag"]
    _raw("topic_pause", topic="a")
    assert client.get("/messages.json", headers={"If-None-Match": first.headers["etag"]}).status_code == 200


def test_a_device_on_the_network_signs_in_before_any_304(monkeypatch):
    open_network(monkeypatch, token="t" * 32)
    _raw("topic_add", topic="a")
    etag = TestClient(app).get("/messages.json").headers["etag"]
    lan = TestClient(app, client=("192.168.1.7", 50000), base_url="http://192.168.1.2:8787")
    refused = lan.get("/messages.json", headers={"If-None-Match": etag, "Accept": "application/json"})
    assert refused.status_code == 401
    assert "items" not in refused.text


def test_a_device_on_the_network_gets_the_scrubbed_projection(monkeypatch):
    open_network(monkeypatch, token="t" * 32)
    _raw(
        "check_fail",
        topic="t1",
        title="Show A / Сериал А",
        url="http://tracker.example/topic?id=1&pk=secretpasskey",
        path="D:\\TV\\Show A",
        hash="A" * 40,
        tracker="rutor",
        cls="no_path",
        error="cannot write D:\\TV\\Show A\\e01.mkv or /srv/media/show/e01.mkv via https://user:pw@host.example/x",
        how="auto",
    )
    lan = TestClient(app, client=("192.168.1.7", 50000), base_url="http://192.168.1.2:8787")
    assert lan.post(
        "/login", data={"password": "t" * 32}, headers={"Origin": "http://192.168.1.2:8787"}
    ).status_code in (
        200,
        303,
    )
    answer = lan.get("/messages.json", headers={"Accept": "application/json"})
    assert answer.status_code == 200
    (item,) = answer.json()["items"]
    words = f"{item['subject']} {item['text']}"
    for leak in ("TV", "/srv/", "secretpasskey", "user:pw", "AAAAAAAA", "tracker.example/topic", "e01.mkv", "pw@"):
        assert leak not in words, leak
    assert "host.example" in words  # a link keeps its host
    assert set(item) == {"id", "cursor", "kind", "category", "subject", "text", "at", "href", "read"}
    assert item["href"] == "/#row-t1"
    assert item["subject"] == "Show A"


def test_markers_that_cannot_be_saved_are_reported(monkeypatch):
    client = TestClient(app, base_url=BASE, headers=LOCAL)
    _raw("topic_add", topic="a")
    cursor = client.get("/messages.json").json()["cursor"]

    def full_disk(_content: bytes) -> None:
        raise OSError("disk full")

    monkeypatch.setattr(messages_feed, "_write_ui_state", full_disk)
    answer = client.post("/messages/read", data={"cursor": cursor})
    assert answer.status_code == 503
    assert answer.json()["ok"] is False
    assert answer.json()["error"]
    assert client.get("/messages.json").json()["unread"] == 1


# --------------------------------------------------------------------------- data/ui.json


def test_a_lost_or_damaged_sidecar_falls_back_without_a_rewrite():
    client = TestClient(app, base_url=BASE, headers=LOCAL)
    _raw("topic_add", topic="a")
    cursor = client.get("/messages.json").json()["cursor"]
    client.post("/messages/clear", data={"cursor": cursor})
    assert client.get("/messages.json").json()["items"] == []
    ui_state_path().write_bytes(b"{not json")
    damaged = client.get("/messages.json").json()
    assert damaged["markers"] == "damaged"
    assert damaged["unread"] == 1  # cleared and read come back, as documented
    assert ui_state_path().read_bytes() == b"{not json"  # a GET never repairs
    ui_state_path().unlink()
    assert client.get("/messages.json").json()["markers"] == "missing"
    assert not ui_state_path().exists()
    assert client.post("/messages/read", data={"cursor": cursor}).status_code == 200
    assert json.loads(ui_state_path().read_text(encoding="utf-8"))["schema"] == 1


@pytest.mark.parametrize(
    "content",
    [
        {"schema": 2, "read_upto": None, "cleared_upto": None},  # a later version's
        {"schema": 1, "read_upto": None, "cleared_upto": None, "text": "a message"},  # more than markers
        {"schema": 1, "read_upto": {"segment": "x", "offset": 1, "event": "y"}, "cleared_upto": None},
    ],
)
def test_only_the_exact_schema_counts(content):
    ui_state_path().write_text(json.dumps(content), encoding="utf-8")
    assert messages_feed.load_markers().status == "damaged"


def test_downgrade_and_upgrade_again_keep_working():
    """1.32 ignores data/ui.json and keeps logging; back on this version the markers still say
    what they said, or, for a file rotated away meanwhile, that everything retained is newer."""
    client = TestClient(app, base_url=BASE, headers=LOCAL)
    _raw("topic_add", topic="a")
    client.post("/messages/read", data={"cursor": client.get("/messages.json").json()["cursor"]})
    marker = ui_state_path().read_bytes()
    messages_feed._FEEDS.clear()  # the old version's server
    for n in range(3):
        _raw("topic_pause", topic=f"t{n}")
        _rotate()
    messages_feed._FEEDS.clear()  # this version again, a new process
    payload = client.get("/messages.json").json()
    assert ui_state_path().read_bytes() == marker
    assert payload["reset"] is False
    assert payload["unread"] == 3
    assert [item["read"] for item in payload["items"]] == [False, False, False, True]


def test_the_sidecar_is_never_in_a_copy_of_the_data_and_survives_a_restore(monkeypatch):
    from tow.bundle import archive
    from tow.restore_points import create_restore_point, restore_from_point
    from tow.snapshots import members

    _raw("topic_add", topic="a")
    client = TestClient(app, base_url=BASE, headers=LOCAL)
    client.post("/messages/read", data={"cursor": client.get("/messages.json").json()["cursor"]})
    assert "ui.json" not in archive.ALLOWED_MEMBERS
    assert all(name != "ui.json" for name, _path in members.fixed_members())
    point = create_restore_point()
    _raw("topic_add", topic="b")
    client.post("/messages/clear", data={"cursor": client.get("/messages.json").json()["cursor"]})
    after_clear = ui_state_path().read_bytes()
    restore_from_point(point["id"])
    assert ui_state_path().read_bytes() == after_clear  # the destination's read/clear state stays
    spec = importlib.util.spec_from_file_location("tow_update_for_feed", ROOT / "scripts" / "update.py")
    assert spec
    assert spec.loader
    update = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(update)
    assert "ui.json" in update.SKIP_FILES
    assert "ui.json" not in update.data_files(data_dir())


def test_undo_does_not_touch_the_sidecar():
    from tow.store import load_state, save_state

    save_state(
        {"topics": [{"id": "t1", "title": "Show A", "url": "http://rutor.info/torrent/1/x", "save_path": "D:/TV"}]}
    )
    client = TestClient(app, base_url=BASE, headers=LOCAL)
    assert client.post("/topics/t1/delete", follow_redirects=False).status_code == 303
    client.post("/messages/read", data={"cursor": client.get("/messages.json").json()["cursor"]})
    marker = ui_state_path().read_bytes()
    stamp = load_state()["undo"]["ts"]
    assert client.post("/undo", data={"stamp": stamp}, follow_redirects=False).status_code == 303
    assert ui_state_path().read_bytes() == marker
    assert client.get("/messages.json").json()["unread"] == 1  # the undo itself is a new message


# --------------------------------------------------------------------------- update results


def _job(status: str, **extra: object) -> dict:
    return {"id": "f" * 32, "status": status, "target": "1.33.0", "previous": "1.32.3", **extra}


def test_an_update_result_is_logged_once_per_job_from_its_record():
    assert messages_feed.reconcile_update(_job("ok")) is True
    assert messages_feed.reconcile_update(_job("ok")) is False
    messages_feed._FEEDS.clear()  # a restarted server finds it in the log
    assert messages_feed.reconcile_update(_job("ok")) is False
    results = [event for event in log.read_events() if event["kind"] == "settings_update_result"]
    assert len(results) == 1
    assert results[0]["operation_id"] == "f" * 32
    (item,) = _feed()["items"]
    assert (item["category"], item["kind"]) == ("updates", "ok")
    assert "1.33.0" in item["text"]


@pytest.mark.parametrize(("status", "severity"), [("failed", "err"), ("rolled_back", "warn"), ("refused", "err")])
def test_a_failed_update_is_an_error_and_a_running_one_is_nothing_yet(status, severity):
    assert messages_feed.reconcile_update(_job("installing")) is False
    assert messages_feed.reconcile_update(_job(status)) is True
    assert _feed()["items"][0]["kind"] == severity


def test_a_lost_append_of_an_update_result_is_tried_again(monkeypatch):
    monkeypatch.setattr(log, "append_event", lambda *_a, **_k: None)
    assert messages_feed.reconcile_update(_job("ok")) is False
    monkeypatch.undo()
    assert messages_feed.reconcile_update(_job("ok")) is True


def test_the_web_reconciles_from_the_finished_job(monkeypatch):
    from tow.web import messages

    monkeypatch.setattr("tow.web.services.finished_web_update", lambda: _job("ok"))
    assert messages.reconcile_update_results() is True
    assert messages.reconcile_update_results() is False


# --------------------------------------------------------------------------- truthful outcomes


def test_a_lost_log_line_never_turns_a_saved_action_into_a_failure(monkeypatch):
    from tow.store import load_state, save_state

    save_state(
        {"topics": [{"id": "t1", "title": "Show A", "url": "http://rutor.info/torrent/1/x", "save_path": "D:/TV"}]}
    )

    def broken():
        raise OSError("disk full")

    monkeypatch.setattr(log, "_log_file_lock", broken)
    client = TestClient(app, base_url=BASE, headers=LOCAL)
    answer = client.post("/topics/t1/pause", follow_redirects=False)
    assert answer.status_code == 303
    assert load_state()["topics"][0]["paused"] is True
    feed = client.get("/messages.json")
    assert feed.status_code == 200
    assert feed.json()["items"] == []  # the list never promises a lossless history


def test_scrubbing_after_formatting_keeps_the_words_and_drops_paths_and_links():
    text = (
        r"cannot write D:\TV\Show A\e01.mkv or /srv/media/My Show/e01.mkv via "
        r"https://user:pw@host.example/x?pk=1 and \\nas\share\Show B\x.mkv · rutor: all hosts failed (HTTP 404)"
    )
    shown = messages_feed.scrub_display(text)
    assert shown == "cannot write … or … via host.example and … · rutor: all hosts failed (HTTP 404)"
    assert messages_feed.scrub_display("and/or 1/2 episodes") == "and/or 1/2 episodes"


# --------------------------------------------------------------------------- rotation while the log is read


def _live(source: object) -> bool:
    """The reader is at the live log file (given as a path or an open file)."""
    return str(getattr(source, "name", source)).endswith(log.log_path().name)


def _rotation_at(monkeypatch, moment: str) -> list[str]:
    """Another process rotates the log once and appends to the new file, at ``moment`` of the
    reader's pass: between two files, or before or after the live file's first line, or before its
    records. A file the reader holds open may refuse the rotation (Windows): the appender then
    appends to the live file, as ``log.append_event`` does. The new event's id lands in the list."""
    fired: list[str] = []

    def fire() -> None:
        if fired:
            return
        with suppress(OSError):  # held open by the reader: not rotated now
            _rotate()
        fired.append(_raw("topic_pause", topic="late"))

    if moment == "between_files":
        files = messages_feed.LogReader._files

        class Files(list):
            def __iter__(self):
                for path in list.__iter__(self):
                    if _live(path):
                        fire()
                    yield path

        monkeypatch.setattr(messages_feed.LogReader, "_files", lambda self: Files(files(self)))
    elif moment in ("before_first_line", "after_first_line"):
        first_line = messages_feed._first_line

        def wrapped(source, *args, **kwargs):
            if moment == "before_first_line" and _live(source):
                fire()
            found = first_line(source, *args, **kwargs)
            if moment == "after_first_line" and _live(source):
                fire()
            return found

        monkeypatch.setattr(messages_feed, "_first_line", wrapped)
    else:
        parse = messages_feed.LogReader._parse

        def parsing(self, source, *args, **kwargs):
            if _live(source):
                fire()
            return parse(self, source, *args, **kwargs)

        monkeypatch.setattr(messages_feed.LogReader, "_parse", parsing)
    return fired


def _seen(snapshot) -> list[tuple[str, object]]:
    return [(rec["event_id"], rec["_pos"]) for rec in snapshot.window.records]


@pytest.mark.parametrize("moment", ["between_files", "before_first_line", "after_first_line", "before_parse"])
def test_a_rotation_while_the_log_is_read_never_binds_records_to_another_file(monkeypatch, moment):
    a = _raw("topic_add", topic="a")
    b = _raw("topic_add", topic="b")
    _rotate()
    c = _raw("topic_add", topic="c")
    messages_feed.feed().mark(_feed()["items"][0]["cursor"], clear=False)  # read up to c, in the live file
    reader = messages_feed.Feed(log.log_path())  # another process's reader, started now
    late = _rotation_at(monkeypatch, moment)
    first = reader.refresh()
    assert late
    assert first.projection.reset is False  # the marker's file is never missed
    ids = [event for event, _pos in _seen(first)]
    assert ids in ([a, b, c], [a, b, c, late[0]])
    final = reader.refresh()
    fresh = messages_feed.Feed(log.log_path()).refresh()  # a reader started after it all
    assert _seen(final) == _seen(fresh)
    assert [event for event, _pos in _seen(final)] == [a, b, c, late[0]]
    assert _ids(messages_feed.render(final.projection, lang="en")) == [late[0], c, b, a]


def test_a_log_that_keeps_rotating_while_read_keeps_the_last_good_snapshot(monkeypatch):
    _raw("topic_add", topic="a")
    feed = messages_feed.feed()
    before = feed.refresh()
    _raw("topic_add", topic="b")
    turns = iter(range(10**6))
    # Every look at the files finds them moved on: the inventory never holds still.
    monkeypatch.setattr(messages_feed.LogReader, "_inventory", lambda self: (next(turns),), raising=False)
    assert feed.refresh() is before  # never a window of files that moved while they were read
    with pytest.raises(messages_feed.MarkerRefused):
        feed.mark(before.projection.entries[0]["pos"].cursor(), clear=True)
    assert not ui_state_path().exists()
