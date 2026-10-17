"""One state on every open page (tow.web.sync, routes_sync): the revisions /sync.json publishes,
what they are built from, when they change by the clock alone, how they are asked (conditional,
signed in, within a rate) and that asking reads only - never a lock a check or the log holds,
never a recovery, a quarantine or a folder made."""

from __future__ import annotations

import concurrent.futures
import os
import re
import threading
import time
from pathlib import Path

import pytest
from fastapi.testclient import TestClient
from helpers import manual_check_consumers, open_network

from tow import releases
from tow.config import load_config, save_config
from tow.store import load_state, persistence_lock, save_download_history, save_state
from tow.sync_sources import Reading
from tow.web import app, check_jobs, services, sync

KEY = "k" * 40
LAN = ("192.168.1.7", 50000)
ORIGIN = {"Origin": "http://127.0.0.1"}
HEX = re.compile(r"[0-9a-f]{32}")


def _topic(tid: str, title: str = "Show A", **fields):
    number = sum(ord(char) * 31**place for place, char in enumerate(tid)) % 10**7 + 1
    return {"id": tid, "title": title, "url": f"http://rutor.info/torrent/{number}/x", "save_path": "M:/a", **fields}


class Clock:
    def __init__(self) -> None:
        self.now = 1000.0

    def __call__(self) -> float:
        return self.now

    def tick(self, seconds: float = sync.SAMPLE_SEC) -> None:
        self.now += seconds


@pytest.fixture(autouse=True)
def clock(monkeypatch):
    """A fresh sampler on a clock the test moves (one sampling cycle: ``clock.tick()``)."""
    ticking = Clock()
    monkeypatch.setattr(sync, "SAMPLER", sync.Sampler(clock=ticking))
    monkeypatch.setattr(sync, "AREAS_MEMO", sync.Areas())
    with sync._BUCKETS_LOCK:
        sync._BUCKETS.clear()
    yield ticking
    with sync._BUCKETS_LOCK:
        sync._BUCKETS.clear()


def _seed(*topics):
    save_state({"topics": list(topics or [_topic("t1"), _topic("t2", "Show B")])})


def _poll(client: TestClient, have: dict | None = None, **params):
    query = {"have": ",".join(f"{k}:{v}" for k, v in (have or {}).items()), **params}
    return client.get("/sync.json", params={k: v for k, v in query.items() if v})


def _revs(client: TestClient, **params) -> dict[str, str]:
    response = _poll(client, **params)
    assert response.status_code == 200, response.text
    return response.json()["rev"]


def _write_elsewhere(path: Path, content: bytes) -> None:
    """Another process writes: no write in this process, nothing for its caches to know."""
    with path.open("r+b" if path.exists() else "wb") as handle:
        handle.seek(0)
        handle.write(content)
        handle.truncate()


def _lan(monkeypatch) -> TestClient:
    open_network(monkeypatch, token=KEY)
    lan = TestClient(app, client=LAN, headers=ORIGIN)
    assert lan.post("/login", data={"password": KEY}, follow_redirects=False).status_code == 303
    return lan


# --------------------------------------------------------------------------- the answer


def test_the_answer_names_a_revision_per_area_and_is_conditional():
    _seed()
    client = TestClient(app)
    first = _poll(client)
    assert first.status_code == 200
    body = first.json()
    assert body["ok"] is True
    assert body["schema"] == sync.SCHEMA
    assert set(body["rev"]) == set(sync.AREAS) - {"page"}
    assert all(HEX.fullmatch(rev) for rev in body["rev"].values())
    assert set(body["areas"]) == set(body["rev"])  # no ``have``: every area
    assert body["areas"]["topics"]["full"] is True
    assert body["areas"]["topics"]["order"] == ["t1", "t2"]
    assert first.headers["cache-control"] == "private, no-cache"
    assert "Cookie" in first.headers["vary"]
    assert "Accept-Language" in first.headers["vary"]
    assert first.headers["date"]
    etag = first.headers["etag"]
    assert etag.startswith('W/"')

    again = client.get("/sync.json", headers={"If-None-Match": etag})
    assert again.status_code == 304
    assert again.headers["etag"] == etag
    assert again.headers["cache-control"] == "private, no-cache"
    assert "Accept-Language" in again.headers["vary"]
    assert again.headers["date"]
    assert again.content == b""


def test_another_have_is_another_answer_then_its_own_304():
    _seed()
    client = TestClient(app)
    first = _poll(client)
    revs = first.json()["rev"]
    current = client.get(
        "/sync.json",
        params={"have": ",".join(f"{k}:{v}" for k, v in revs.items())},
        headers={"If-None-Match": first.headers["etag"]},
    )
    assert current.status_code == 200  # the ETag names the exact request: another have, another answer
    assert current.json()["areas"] == {}  # nothing differs from what the page has
    repeat = client.get(
        "/sync.json",
        params={"have": ",".join(f"{k}:{v}" for k, v in revs.items())},
        headers={"If-None-Match": current.headers["etag"]},
    )
    assert repeat.status_code == 304


@pytest.mark.parametrize(
    "query",
    [
        {"have": "topics:xyz"},
        {"have": "nope:" + "a" * 32},
        {"have": "topics:" + "a" * 32 + ",topics:" + "b" * 32},
        {"have": "x" * 2000},
        {"page": "/elsewhere"},
    ],
)
def test_a_malformed_question_is_refused(query):
    _seed()
    response = TestClient(app).get("/sync.json", params=query)
    assert response.status_code == 400
    assert response.json() == {"ok": False, "error": "bad_request"}


def test_a_check_another_process_committed_reaches_the_poll_after_one_second(clock):
    # The scheduled check runs in another process: only the shared sample sees its write. With
    # a 5 s poll a page shows it within 6 s (a 5 s sample made that 10 s).
    _seed()
    client = TestClient(app)
    first = _poll(client)
    path = services.sync_source_paths()["state"]
    _write_elsewhere(path, path.read_bytes().replace(b"Show B", b"Show Q"))
    clock.tick(1.0)
    assert _poll(client, first.json()["rev"]).json()["areas"]["topics"]["changed"] == ["t2"]


def test_a_change_made_in_one_tab_is_a_changed_row_for_another_device(monkeypatch, clock):
    _seed()
    lan = _lan(monkeypatch)
    local = TestClient(app, headers=ORIGIN)
    before = _revs(lan)
    assert local.post("/topics/t2/pause", follow_redirects=False).status_code in (302, 303)
    clock.tick()
    after = _poll(lan, before).json()
    topics = after["areas"]["topics"]
    assert topics["full"] is False
    assert topics["changed"] == ["t2"]
    assert topics["removed"] == []
    rows = lan.get("/topics/rows", params={"ids": "t2", "rev": topics["rev"]}).json()
    assert rows["rev"] == topics["rev"]
    assert rows["stale"] is False
    assert "is-paused" in rows["rows"][0]["html"]


def test_a_removed_topic_is_in_the_diff_and_an_unknown_base_gets_the_whole_manifest(clock):
    _seed()
    client = TestClient(app)
    before = _revs(client)
    _seed(_topic("t1"), _topic("t3", "Show C"))
    clock.tick()
    topics = _poll(client, before).json()["areas"]["topics"]
    assert topics["removed"] == ["t2"]
    assert topics["changed"] == ["t3"]
    assert topics["order"] == ["t1", "t3"]
    unknown = _poll(client, {"topics": "0" * 32}).json()["areas"]["topics"]
    assert unknown["full"] is True
    assert set(unknown["rows"]) == {"t1", "t3"}


# --------------------------------------------------------------------------- revisions and their sources


def test_a_same_size_rewrite_under_the_same_stamp_is_seen_and_no_page_shows_the_old_one(clock):
    """Coarse timestamps, the same second, the same size, the same file: the stamp is the same,
    the content is not. The digest decides - and the pages' parsed cache is dropped with it."""
    _seed(_topic("t1", "Show A"))
    path = services.sync_source_paths()["state"]
    client = TestClient(app)
    before = _revs(client)
    assert load_state()["topics"][0]["title"] == "Show A"  # the pages' cache holds it
    info = path.stat()
    content = path.read_bytes()
    _write_elsewhere(path, content.replace(b"Show A", b"Show Z"))
    os.utime(path, ns=(info.st_atime_ns, info.st_mtime_ns))
    assert path.stat().st_mtime_ns == info.st_mtime_ns
    assert path.stat().st_size == info.st_size
    assert load_state()["topics"][0]["title"] == "Show A"  # the stamp hides it from the cache
    clock.tick()
    after = _revs(client)
    assert after["topics"] != before["topics"]
    assert load_state()["topics"][0]["title"] == "Show Z"  # and now from no page either


def test_a_config_rewritten_under_the_same_stamp_reaches_every_page(clock):
    _seed()
    path = services.sync_source_paths()["config"]
    client = TestClient(app)
    before = _revs(client)
    assert load_config()["language"] == "ru"
    info = path.stat()
    _write_elsewhere(path, path.read_bytes().replace(b"language: ru", b"language: en"))
    os.utime(path, ns=(info.st_atime_ns, info.st_mtime_ns))
    assert load_config()["language"] == "ru"  # the stamp hides it from the parsed cache
    clock.tick()
    after = _poll(client).json()
    assert after["rev"]["config"] != before["config"]
    assert after["areas"]["config"]["language"] == "en"
    assert load_config()["language"] == "en"


def test_the_automatic_update_checks_preference_reaches_every_settings_page(clock):
    # Settings' "Check for new versions automatically" was in no area: another device's change never
    # reached an open Settings page, which kept showing (and saving against) the old value.
    _seed()
    client = TestClient(app)
    first = _poll(client).json()
    assert first["areas"]["config"]["check_updates"] is True
    cfg = load_config()
    cfg["check_updates"] = False
    save_config(cfg)
    clock.tick()
    after = _poll(client, have={"config": first["rev"]["config"]}).json()
    assert after["rev"]["config"] != first["rev"]["config"]
    assert after["areas"]["config"]["check_updates"] is False


def test_the_header_follows_the_clients_health_and_not_the_rows(clock):
    _seed()
    client = TestClient(app)
    before = _revs(client)
    state = load_state()
    state["health"] = {"qbit_ok": True}
    save_state(state)
    after = _poll(client, before).json()
    assert after["areas"]["header"]["qbit_tone"] == "ok"
    assert after["rev"]["topics"] == before["topics"]
    assert set(after["areas"]) >= {"header"}
    assert "topics" not in after["areas"]


def test_a_rewrite_of_the_same_content_with_a_new_stamp_changes_no_revision(clock):
    _seed()
    path = services.sync_source_paths()["state"]
    client = TestClient(app)
    before = _revs(client)
    content = path.read_bytes()
    _write_elsewhere(path, content)
    os.utime(path, ns=(time.time_ns() + 10**9, time.time_ns() + 10**9))
    clock.tick()
    assert _revs(client) == before


def test_a_wall_clock_set_back_changes_no_revision_and_sampling_goes_on(monkeypatch, clock):
    _seed()
    client = TestClient(app)
    before = _revs(client)
    real = time.time
    monkeypatch.setattr(time, "time", lambda: real() - 7 * 24 * 3600)
    clock.tick()
    assert _revs(client) == before
    save_state({"topics": [_topic("t1")]})  # sampled at once: this process wrote
    assert _revs(client)["topics"] != before["topics"]


def test_a_store_that_disappears_and_comes_back_has_its_revisions_back(clock):
    _seed()
    path = services.sync_source_paths()["state"]
    client = TestClient(app)
    before = _revs(client)
    content = path.read_bytes()
    path.unlink()
    clock.tick()
    gone = _poll(client).json()
    assert gone["rev"]["topics"] != before["topics"]
    assert gone["areas"]["topics"]["order"] == []  # missing: no topics, as every page says
    _write_elsewhere(path, content)
    clock.tick()
    assert _revs(client) == before  # the same content, the same revisions


def test_a_damaged_store_is_unavailable_without_quarantine_or_lock(clock):
    _seed()
    path = services.sync_source_paths()["state"]
    client = TestClient(app)
    before = _revs(client)
    content = path.read_bytes()
    _write_elsewhere(path, b"{ not json")
    clock.tick()
    with _locks_held():
        response = _within(lambda: _poll(client))
    assert response.status_code == 503
    assert response.json()["error"] == "data_unavailable"
    assert response.headers["retry-after"] == str(sync.RETRY_AFTER_SEC)
    assert response.headers["cache-control"] == "no-store"
    assert path.read_bytes() == b"{ not json"  # left in place: no quarantine from a poll
    assert not [entry for entry in path.parent.iterdir() if ".corrupt-" in entry.name]
    _write_elsewhere(path, content)
    clock.tick()
    assert _revs(client) == before


def test_a_restart_keeps_the_revisions_and_sends_the_whole_manifest(monkeypatch):
    _seed()
    client = TestClient(app)
    older = _revs(client)
    _seed(_topic("t1"), _topic("t3", "Show C"))
    current = _poll(client, older)
    assert current.json()["areas"]["topics"]["full"] is False
    monkeypatch.setattr(sync, "SAMPLER", sync.Sampler())  # another process
    monkeypatch.setattr(sync, "AREAS_MEMO", sync.Areas())
    monkeypatch.setattr(sync, "SERVER_EPOCH", "another-process")
    after = client.get(
        "/sync.json",
        params={"have": ",".join(f"{k}:{v}" for k, v in older.items())},
        headers={"If-None-Match": current.headers["etag"]},
    )
    assert after.status_code == 200  # an ETag of the earlier process never matches
    assert after.json()["rev"] == current.json()["rev"]  # content, not time or process, makes a revision
    topics = after.json()["areas"]["topics"]
    assert topics["full"] is True
    assert set(topics["rows"]) == {"t1", "t3"}
    assert "topics" not in _poll(client, after.json()["rev"]).json()["areas"]


def test_a_file_unreadable_now_is_not_a_missing_file(monkeypatch, clock):
    _seed()
    client = TestClient(app)
    before = _revs(client)
    real = services.sync_read
    state_path = services.sync_source_paths()["state"]

    def busy(path, limit, **kwargs):
        return Reading("error", error="PermissionError") if path == state_path else real(path, limit, **kwargs)

    monkeypatch.setattr(services, "sync_read", busy)
    clock.tick()
    assert _revs(client) == before  # the last good reading holds; never "no topics"
    monkeypatch.setattr(sync, "SAMPLER", sync.Sampler(clock=clock))
    response = _poll(client)  # nothing read before: unavailable, not empty
    assert response.status_code == 503
    assert response.json()["error"] == "data_unavailable"


def test_a_reading_that_saw_the_file_change_is_taken_again(monkeypatch, tmp_path):
    from tow import sync_sources
    from tow.storage.atomic import RegularReadError

    path = tmp_path / "x.json"
    path.write_bytes(b"{}")
    calls = []
    real = sync_sources.read_regular_bytes

    def flaky(target, limit, **kwargs):
        calls.append(target)
        if len(calls) == 1:
            raise RegularReadError("changed")
        return real(target, limit, **kwargs)

    monkeypatch.setattr(sync_sources, "read_regular_bytes", flaky)
    reading = sync_sources.read(path, 1024)
    assert reading.status == "ok"
    assert reading.content == b"{}"
    assert len(calls) == 2
    assert sync_sources.read(tmp_path / "none.json", 1024).status == "missing"
    assert sync_sources.read(tmp_path, 1024).status == "error"  # a folder: there, not a file


# --------------------------------------------------------------------------- what each area is built from


def test_a_site_title_in_the_config_changes_the_rows_and_the_language_every_text(clock):
    _seed()
    client = TestClient(app)
    before = _revs(client)
    cfg = load_config()
    cfg["trackers"]["rutor"]["title"] = "Rutor Renamed"
    save_config(cfg)
    titled = _revs(client)
    assert titled["topics"] != before["topics"]
    cfg["language"] = "en"
    save_config(cfg)
    english = _revs(client)
    for area in ("topics", "header", "attention", "config"):
        assert english[area] != titled[area], area
    assert english["version"] == titled["version"]
    assert english["jobs"] == titled["jobs"]


def test_the_download_history_changes_the_rows(clock):
    _seed()
    client = TestClient(app)
    before = _revs(client)
    save_download_history(
        {
            "schema_version": 1,
            "topics": {
                "t1": {
                    "items": {"a": {"label": "S01E01", "completed_observed_at": "2026-01-01T00:00:00+00:00"}},
                    "last_event": {"kind": "file_completed", "label": "S01E01", "at": "2026-01-01T00:00:00+00:00"},
                }
            },
        }
    )
    after = _revs(client)
    assert after["topics"] != before["topics"]
    assert after["header"] == before["header"]


def test_the_supervisors_plan_changes_the_timers_and_a_due_timer_changes_by_the_clock(monkeypatch, clock):
    _seed(_topic("t1", check_interval_min=30))
    client = TestClient(app)
    plans = {}
    monkeypatch.setattr(
        services,
        "topic_timer_status",
        lambda state: {"t1": {"minutes": 30, "state": "scheduled", "next_at": plans.get("at", 0), "now": time.time()}},
    )
    before = _revs(client)
    status = services.sync_source_paths()["status"]
    status.parent.mkdir(parents=True, exist_ok=True)
    plans["at"] = time.time() + 600
    _write_elsewhere(status, b'{"topic_timers": {}}')
    clock.tick()
    planned = _poll(client).json()
    assert planned["rev"]["timers"] != before["timers"]
    assert planned["areas"]["timers"]["timers"]["t1"]["next_at"] == plans["at"]
    assert "now" not in planned["areas"]["timers"]["timers"]["t1"]  # a page counts down itself
    assert planned["rev"]["topics"] == before["topics"]  # the row has no countdown


def test_a_row_projection_holds_everything_the_row_template_reads():
    template = (Path(sync.__file__).parent.parent / "templates" / "_topic_row.html").read_text(encoding="utf-8")
    read = set(re.findall(r"(?<![\w.'])topic\.([a-z_]+)", template))
    assert read - {"timer", "manual_job"} <= set(sync.ROW_FIELDS)
    row = {name: f"v-{name}" for name in sync.ROW_FIELDS} | {"last_check": "a", "timer": {"minutes": 5, "now": 1.0}}
    same = sync.row_projection(row, site_title="S", day="d")
    later = sync.row_projection(
        {**row, "last_check": "b", "timer": {"minutes": 5, "now": 2.0}}, site_title="S", day="d"
    )
    assert same == later  # a check's stamp and the clock change no row


def test_a_new_day_rewrites_the_date_labels_without_a_write(clock):
    _seed(_topic("t1"), _topic("t2", "Show B"))
    save_download_history(
        {
            "schema_version": 1,
            "topics": {
                "t1": {
                    "items": {},
                    "last_event": {"kind": "file_completed", "label": "x", "at": "2026-01-01T10:00:00+00:00"},
                }
            },
        }
    )
    request = _request()
    today = time.time()
    first = sync.answer(request, {}, "", now=today)
    tomorrow = sync.answer(request, {"topics": first.revs["topics"]}, "", now=sync._next_midnight(today) + 1)
    assert tomorrow.revs["topics"] != first.revs["topics"]
    assert tomorrow.body()["areas"]["topics"]["changed"] == ["t1"]  # only the row with a date label


def test_needs_attention_becomes_late_by_the_clock_alone(monkeypatch, clock):
    started = time.time()
    _seed()
    state = load_state()
    state["health"] = {"auto_at_ts": started, "check_ok": True}
    save_state(state)
    client = TestClient(app)
    before = _poll(client).json()
    assert before["areas"]["attention"]["count"] == 0
    later = started + 4 * 3600 + 3600
    monkeypatch.setattr(time, "time", lambda: later)
    after = _poll(client, before["rev"]).json()  # the same sample: no file was written
    assert after["rev"]["attention"] != before["rev"]["attention"]
    assert any(entry["kind"] == "check_stale" for entry in after["areas"]["attention"]["entries"])


def test_an_undo_is_published_with_its_fixed_end_and_goes_by_the_clock(monkeypatch, clock):
    from datetime import UTC, datetime

    _seed()
    state = load_state()
    stamped = datetime.now(UTC)
    state["undo"] = {"kind": "topic_pause", "ts": stamped.isoformat(), "topic": "t1", "paused": False}
    save_state(state)
    client = TestClient(app)
    first = _poll(client).json()
    undo = first["areas"]["undo"]["undo"]
    ttl = load_config().get("flash_ttl_sec") or 60
    assert undo["stamp"] == stamped.isoformat()
    assert undo["until_ts"] == pytest.approx(stamped.timestamp() + int(ttl))
    second = _poll(client).json()
    assert second["rev"]["undo"] == first["rev"]["undo"]  # no seconds-left bucket
    real = time.time
    monkeypatch.setattr(time, "time", lambda: real() + int(ttl) + 1)
    gone = _poll(client, first["rev"]).json()
    assert gone["areas"]["undo"]["undo"] is None


def test_an_access_undo_the_network_may_not_apply_is_local_only_there(monkeypatch, clock):
    from datetime import UTC, datetime

    _seed()
    lan = _lan(monkeypatch)
    cfg = load_config()
    state = load_state()
    state["undo"] = {
        "kind": "settings_access",
        "ts": datetime.now(UTC).isoformat(),
        "old_bind": "127.0.0.1",
        "old_allow_lan": False,
        "bind": cfg["bind"],
        "allow_lan": True,
    }
    save_state(state)
    assert _poll(lan).json()["areas"]["undo"]["undo"]["local_only"] is True
    assert _poll(TestClient(app)).json()["areas"]["undo"]["undo"]["local_only"] is False


def test_the_event_log_changes_the_messages_revision(clock):
    _seed()
    client = TestClient(app)
    before = _revs(client)
    assert services.log_event("topic_pause", topic="t1", how="manual") is True
    clock.tick()
    after = _revs(client)
    assert after["messages"] != before["messages"]
    assert after["topics"] == before["topics"]


def test_a_routine_log_line_that_is_no_message_leaves_the_messages_revision(clock):
    # The revision is the shared feed's own: every open page would otherwise fetch the feed
    # again after each routine check line.
    _seed()
    client = TestClient(app)
    before = _revs(client)
    assert services.log_event("tracker_checked", topic="t1", how="schedule") is True
    clock.tick()
    assert _revs(client)["messages"] == before["messages"]


def test_the_settings_page_follows_its_backup_inventory(monkeypatch, clock):
    _seed()
    client = TestClient(app)
    # A copy as the page lists it (the revision is what the page draws: tow.web.page_sync).
    copies = [{"name": "a", "created_at": "2026-10-16T10:00:00+00:00", "bytes": 10}]
    monkeypatch.setattr(services, "list_snapshots", lambda limit=None: list(copies))
    before = _revs(client, page="/settings")
    copies.append({"name": "b", "created_at": "2026-10-16T11:00:00+00:00", "bytes": 10})
    clock.tick()
    after = _revs(client, page="/settings")
    assert after["page"] != before["page"]
    assert after["topics"] == before["topics"]


def test_the_history_page_follows_the_names_it_shows(clock):
    _seed()
    client = TestClient(app)
    before = _revs(client, page="/history")
    _seed(_topic("t1", "Renamed"), _topic("t2", "Show B"))
    assert _revs(client, page="/history")["page"] != before["page"]


# --------------------------------------------------------------------------- jobs


def test_every_job_transition_changes_the_jobs_revision_also_for_a_check_of_every_topic(monkeypatch):
    _seed()
    consumers = manual_check_consumers(monkeypatch)
    client = TestClient(app)
    seen = [_revs(client)["jobs"]]
    gate = threading.Event()
    calls = []

    def perform(job):
        calls.append(job["id"])
        if len(calls) == 1:
            raise check_jobs.CheckBusyError("busy")  # waits for the check lock once
        job["status"] = "done"

    monkeypatch.setattr(check_jobs, "_pause", lambda seconds: gate.set())
    everything, _added = check_jobs.submit(topic_id="", apply=True, lang="ru", perform=perform)
    seen.append(_revs(client)["jobs"])  # made (queued)
    body = _poll(client).json()["areas"]["jobs"]
    assert body["waiting"][0]["topic_id"] == ""
    assert body["waiting"][0]["status"] == "queued"
    second, _ = check_jobs.submit(topic_id="t1", apply=True, lang="ru", perform=perform)
    seen.append(_revs(client)["jobs"])
    assert check_jobs.cancel(second["id"])
    seen.append(_revs(client)["jobs"])  # cancelled
    generation = check_jobs.generation()
    consumers[0].run()  # running, back to waiting for the check lock, running, done
    assert check_jobs.generation() >= generation + 4
    seen.append(_revs(client)["jobs"])
    ended = _poll(client).json()["areas"]["jobs"]
    assert ended["waiting"] == []
    assert {item["id"] for item in ended["ended"]} == {everything["id"], second["id"]}
    with check_jobs.LOCK:
        check_jobs.JOBS[everything["id"]]["finished_at"] -= check_jobs.RESULT_TTL + 1
    check_jobs.submit(topic_id="t2", apply=False, lang="ru", perform=perform)  # forgets the expired one
    seen.append(_revs(client)["jobs"])
    assert len(set(seen)) == len(seen)


# --------------------------------------------------------------------------- who may ask


def test_authentication_comes_before_a_304(monkeypatch):
    _seed()
    lan = _lan(monkeypatch)
    signed = _poll(lan)
    assert signed.status_code == 200
    stranger = TestClient(app, client=LAN, headers=ORIGIN)
    refused = stranger.get("/sync.json", headers={"If-None-Match": signed.headers["etag"], "Accept": "text/html"})
    assert refused.status_code == 401
    assert refused.headers["content-type"].startswith("application/json")
    assert refused.headers["cache-control"] == "no-store"
    rows = stranger.get("/topics/rows?ids=t1", headers={"Accept": "text/html"}, follow_redirects=False)
    assert rows.status_code == 401
    assert "location" not in rows.headers
    assert rows.headers["content-type"].startswith("application/json")


def test_a_foreign_host_and_a_closed_network_are_refused_as_json(monkeypatch):
    _seed()
    client = TestClient(app)
    etag = _poll(client).headers["etag"]
    foreign = client.get("/sync.json", headers={"Host": "evil.example", "If-None-Match": etag})
    assert foreign.status_code == 403
    assert foreign.json()["ok"] is False
    closed = TestClient(app, client=LAN).get("/sync.json", headers={"If-None-Match": etag})
    assert closed.status_code == 403
    assert closed.json()["ok"] is False


def test_local_and_remote_language_and_session_are_separate_variants(monkeypatch):
    _seed()
    lan = _lan(monkeypatch)
    local = TestClient(app)
    assert _poll(local).headers["etag"] != _poll(lan).headers["etag"]
    other = TestClient(app, client=LAN, headers=ORIGIN)
    assert other.post("/login", data={"password": KEY}, follow_redirects=False).status_code == 303
    assert _poll(other).headers["etag"] != _poll(lan).headers["etag"]  # another session
    cfg = load_config()
    cfg["language"] = "auto"
    save_config(cfg)
    english = local.get("/sync.json", headers={"Accept-Language": "en"})
    russian = local.get("/sync.json", headers={"Accept-Language": "ru"})
    assert english.json()["areas"]["config"]["language_resolved"] == "en"
    assert russian.json()["areas"]["config"]["language_resolved"] == "ru"
    assert english.json()["rev"]["topics"] != russian.json()["rev"]["topics"]
    assert english.headers["etag"] != russian.headers["etag"]
    assert (
        local.get("/sync.json", headers={"Accept-Language": "en", "If-None-Match": russian.headers["etag"]}).status_code
        == 200
    )


def test_a_rows_post_needs_this_sites_origin_and_json():
    _seed()
    bare = TestClient(app)
    assert bare.post("/topics/rows", json={"ids": ["t1"]}).status_code == 403
    client = TestClient(app, headers=ORIGIN)
    assert client.post("/topics/rows", data={"ids": "t1"}).status_code == 415
    answer = client.post("/topics/rows", json={"ids": ["t1", "t1", "gone"]})
    assert answer.status_code == 200
    assert answer.headers["cache-control"] == "no-store"
    body = answer.json()
    assert [row["id"] for row in body["rows"]] == ["t1"]
    assert body["missing"] == ["gone"]
    assert body["order"] == ["t1", "t2"]


def test_rate_limits_are_per_session_with_retry_after(monkeypatch):
    _seed()
    first = _lan(monkeypatch)
    second = TestClient(app, client=LAN, headers=ORIGIN)
    assert second.post("/login", data={"password": KEY}, follow_redirects=False).status_code == 303
    monkeypatch.setattr(sync, "RATE_BURST", 2.0)
    monkeypatch.setattr(sync, "RATE_PER_SEC", 0.01)
    with sync._BUCKETS_LOCK:
        sync._BUCKETS.clear()
    assert _poll(first).status_code == 200
    assert _poll(first).status_code == 200
    limited = _poll(first)
    assert limited.status_code == 429
    assert int(limited.headers["retry-after"]) >= 1
    assert limited.json()["error"] == "rate_limited"
    assert first.get("/topics/rows?ids=t1").status_code == 429
    assert _poll(second).status_code == 200  # the same address, another session: its own bucket


# --------------------------------------------------------------------------- read-only, no waiting


class _locks_held:
    """The persistence lock, the check lock and the log's lock, held by another thread meanwhile."""

    def __enter__(self):
        from tow.log import _log_file_lock
        from tow.store import check_run_lock

        self._held, self._release = threading.Event(), threading.Event()

        def hold():
            with persistence_lock(), check_run_lock(), _log_file_lock():
                self._held.set()
                self._release.wait(60)

        self._thread = threading.Thread(target=hold, daemon=True)
        self._thread.start()
        assert self._held.wait(10)
        return self

    def __exit__(self, *exc):
        self._release.set()
        self._thread.join(10)


def _within(call, seconds: float = 10.0):
    """``call()`` in a thread; a poll that waits for a held lock fails the test instead of hanging it."""
    with concurrent.futures.ThreadPoolExecutor(1) as pool:
        future = pool.submit(call)
        try:
            return future.result(timeout=seconds)
        except concurrent.futures.TimeoutError:
            pytest.fail("the poll waited for a lock another thread holds")


def test_polls_never_wait_for_the_checkers_or_the_logs_lock(clock):
    _seed()
    client = TestClient(app, headers=ORIGIN)
    first = _poll(client)
    path = services.sync_source_paths()["state"]
    with _locks_held():
        unchanged = _within(lambda: client.get("/sync.json", headers={"If-None-Match": first.headers["etag"]}))
        assert unchanged.status_code == 304
        _write_elsewhere(path, path.read_bytes().replace(b"Show B", b"Show Q"))  # a check committed
        clock.tick()
        changed = _within(lambda: _poll(client, first.json()["rev"]))
        assert changed.status_code == 200
        assert changed.json()["areas"]["topics"]["changed"] == ["t2"]
        rows = _within(lambda: client.post("/topics/rows", json={"ids": ["t2"]}))
        assert rows.status_code == 200
        assert "Show Q" in rows.json()["rows"][0]["html"]


def test_a_transaction_in_progress_is_503_and_its_recovery_runs_apart(monkeypatch, clock):
    _seed()
    client = TestClient(app)
    journal = services.sync_source_paths()["journal"]
    recovered = threading.Event()

    def recover():
        with persistence_lock():  # the usual recovery: under the data lock
            if journal.exists():
                journal.rmdir()
        recovered.set()

    monkeypatch.setattr(services, "recover_store_transaction", recover)
    with _locks_held():  # (taking the data lock recovers a journal already there: made after it)
        journal.mkdir(parents=True)
        response = _within(lambda: _poll(client))
        assert response.status_code == 503
        assert response.json()["error"] == "recovering"
        assert response.headers["retry-after"] == str(sync.RETRY_AFTER_SEC)
        assert not recovered.is_set()  # it waits for the lock in its own thread, not in the poll
        # Recent messages are polled by every open page too: the same read-only path, not a
        # recovery under the data lock inside the request.
        messages = _within(lambda: client.get("/messages.json"))  # at once: it reads the log, not the stores
        assert messages.status_code == 200
    assert recovered.wait(10)
    sync._recovery.join(10)
    clock.tick()
    assert _poll(client).status_code == 200


RAW_ERROR = r"cannot write D:\TV\Show A\e01.mkv (see /home/owner/tow/state.json) via http://tracker.example/dl.php?uk=SECRET1&pk=SECRET2"


def test_a_network_device_never_sees_a_rows_raw_error(monkeypatch, clock):
    """Final browser QA 1.33.0 Q5: a Home row's error tooltip (also /topics/rows and the row's
    panel) carried local paths and a link's secret query to network devices, while Recent
    messages scrub them. A network device gets the same scrubbed words; this PC keeps them all."""
    _seed(_topic("t1", last_error=RAW_ERROR, last_error_class="disk"), _topic("t2", "Show B"))
    lan = _lan(monkeypatch)
    local = TestClient(app, headers=ORIGIN)
    secrets = ("SECRET1", "SECRET2", r"D:\TV", "/home/owner", "e01.mkv")

    def row_of(html: str) -> str:  # Home's row t1 (the header's Needs attention is not the row's)
        start = html.index('id="row-t1"')
        return html[start : html.index("</details>", start)]

    for address in ("/", "/topics/rows?ids=t1", "/topics/t1/edit-panel", "/topics/t1/row"):
        page = lan.get(address).text
        shown = row_of(page) if address == "/" else page
        assert "tracker.example" in shown, address  # the link's host stays
        assert not [secret for secret in secrets if secret in shown], address
    assert r"D:\TV\Show A\e01.mkv" in row_of(local.get("/").text)
    # The rows' digests follow what each one is shown: another for a network device.
    remote_rows = _poll(lan).json()["areas"]["topics"]["rows"]
    local_rows = _poll(local).json()["areas"]["topics"]["rows"]
    assert remote_rows["t1"] != local_rows["t1"]
    assert remote_rows["t2"] == local_rows["t2"]


def test_settings_and_history_polls_never_wait_for_the_data_or_the_logs_lock(clock):
    """Final review 1.33.0 #8: the Settings inventory read the night copies' cleanup under the
    data lock (whose taking runs the recovery steps), and History's lines took the log's lock -
    even for a poll that ends unchanged."""
    _seed()
    client = TestClient(app, headers=ORIGIN)
    first = _poll(client, page="/settings")
    history = _poll(client, page="/history")
    with _locks_held():
        clock.tick()  # a new sample: the inventory and the fragments are read again
        again = _within(lambda: _poll(client, page="/settings"))
        assert again.status_code == 200
        assert again.json()["rev"]["page"] == first.json()["rev"]["page"]
        fragments = _within(lambda: client.get("/page/fragments", params={"page": "/settings"}))
        assert fragments.status_code == 200
        lines = _within(lambda: client.get("/page/fragments", params={"page": "/history"}))
        assert lines.status_code == 200
        assert _within(lambda: _poll(client, page="/history")).json()["rev"]["page"] == history.json()["rev"]["page"]


def test_historys_lock_free_snapshot_leaves_out_a_line_still_being_appended():
    import json as json_module

    from tow.log import history_events, log_path

    whole = json_module.dumps({"kind": "new_file", "topic": "t1", "ts": "2026-10-17T10:00:00+00:00"})
    half = json_module.dumps({"kind": "new_file", "topic": "t2", "ts": "2026-10-17T10:01:00+00:00"})[:20]
    path = log_path()
    path.write_text(whole + "\n" + half, encoding="utf-8")
    assert [event["topic"] for event in history_events(lock_live=False)] == ["t1"]
    path.write_text(whole + "\n", encoding="utf-8")
    assert [event["topic"] for event in history_events(lock_live=False)] == ["t1"]


def test_a_poll_never_quarantines_a_damaged_state(clock):
    """``_context.state()`` read on a poll's path (Recent messages name topics through it) never
    takes the data lock to set a damaged file aside: a poll changes nothing."""
    from tow.web import _context

    _seed()
    path = services.sync_source_paths()["state"]
    path.write_bytes(b"{not json")
    services.forget_parsed(path)

    def read():  # in the poll's thread: its request context and its read-only paths
        with services.read_only_paths():
            _context.begin()
            try:
                _context.state()
            except Exception as exc:  # noqa: BLE001 - the error is the answer
                return exc
            return None

    with _locks_held():
        assert _within(read) is not None
    assert path.read_bytes() == b"{not json"  # still there, not set aside
    assert not list(path.parent.glob(f"{path.name}.corrupt-*"))


def _check_journal() -> Path:
    from tow import check_transaction

    return check_transaction._journal().root


def _night_restore_marker() -> Path:
    from tow.paths import data_dir
    from tow.snapshots import restore

    return data_dir() / restore._MARKER


def _import_checkpoint(status: str) -> Path:
    import json

    from tow.bundle import checkpoint
    from tow.paths import data_dir

    folder = data_dir() / "import-checkpoints" / "20261017-100000-test"
    folder.mkdir(parents=True, exist_ok=True)
    # As write_import_transaction writes it, without its atomic writer's lock (held by the test).
    record = {"format": checkpoint.TRANSACTION_FORMAT, "checkpoint": str(folder), "status": status}
    (folder / "TRANSACTION.json").write_text(json.dumps(record), encoding="utf-8")
    return folder


def _remove(path: Path) -> None:
    import shutil

    if path.is_dir():
        shutil.rmtree(path)
    else:
        path.unlink(missing_ok=True)


# Final review 1.33.0 #4: only the site transaction was looked at. A check's commit (history,
# then state), an import or a restore point (import checkpoints) and a night-copy restore have
# markers of their own; while one is there the stores may not belong together.
def _made_check() -> Path:
    _check_journal().mkdir(parents=True)
    return _check_journal()


def _made_night_restore() -> Path:
    _night_restore_marker().write_text("{}", encoding="utf-8")
    return _night_restore_marker()


_MARKERS = {
    "check": _made_check,
    "night_restore": _made_night_restore,
    "import": lambda: _import_checkpoint("prepared"),
}


@pytest.mark.parametrize("marker", sorted(_MARKERS))
def test_every_transaction_marker_is_503_and_its_recovery_runs_apart(monkeypatch, clock, marker):
    _seed()
    client = TestClient(app)
    taken = threading.Event()

    class _lock:  # the recovery's own data lock (whose hooks recover): recorded, not taken here
        def __enter__(self):
            taken.set()

        def __exit__(self, *exc):
            return False

    monkeypatch.setattr(services, "persistence_lock", _lock)
    monkeypatch.setattr(services, "recover_store_transaction", lambda: None)
    with _locks_held():
        made = _MARKERS[marker]()
        try:
            response = _within(lambda: _poll(client))
            assert response.status_code == 503
            assert response.json()["error"] == "recovering"
            assert response.headers["retry-after"] == str(sync.RETRY_AFTER_SEC)
        finally:
            _remove(made)
    sync._recovery.join(10)
    assert taken.is_set()  # the recovery took the data lock, whose hooks recover every journal
    clock.tick()
    assert _poll(client).status_code == 200


def test_a_transaction_that_begins_while_the_stores_are_read_is_503(monkeypatch, clock):
    """Checked before and after the reading: a check's commit started meanwhile."""
    _seed()
    client = TestClient(app)
    reads = []
    real = services.sync_read

    def reading(path, limit):
        reads.append(path)
        if len(reads) == 2:  # between the state and the history
            _check_journal().mkdir(parents=True)
        return real(path, limit)

    monkeypatch.setattr(services, "sync_read", reading)
    monkeypatch.setattr(sync, "recover_later", lambda: None)
    try:
        response = _poll(client)
    finally:
        _remove(_check_journal())
    assert response.status_code == 503
    assert response.json()["error"] == "recovering"


def test_a_finished_import_is_no_transaction(clock):
    _seed()
    _import_checkpoint("committed")
    assert _poll(TestClient(app)).status_code == 200


def test_a_poll_makes_no_folder_and_runs_no_cleanup(monkeypatch, tmp_path):
    from datetime import UTC, datetime, timedelta

    _seed()
    state = load_state()
    state["undo"] = {
        "kind": "site",
        "ts": (datetime.now(UTC) - timedelta(days=1)).isoformat(),
        "secret_undo_ref": "x" * 32,
    }
    save_state(state)
    path = services.sync_source_paths()["state"]
    content = path.read_bytes()
    assert services.undo_cleanup_needed(load_state())
    response = TestClient(app).get("/sync.json")
    assert response.status_code == 200
    assert path.read_bytes() == content  # the cleanup is the next page's, never the poll's

    empty = tmp_path / "fresh-data"
    monkeypatch.setenv("TOW_HOME", str(empty))
    fresh = TestClient(app).get("/sync.json")
    assert fresh.status_code == 200
    assert fresh.json()["areas"]["topics"]["order"] == []
    assert not empty.exists()


# --------------------------------------------------------------------------- Home rows


def test_rows_are_the_rows_home_draws_and_bounded():
    from bs4 import BeautifulSoup

    _seed()
    client = TestClient(app)
    home = BeautifulSoup(client.get("/").text, "html.parser").select_one("#row-t1")
    body = client.get("/topics/rows", params={"ids": "t1,t2"}).json()
    drawn = BeautifulSoup(body["rows"][0]["html"], "html.parser").select_one("#row-t1")
    assert str(drawn) == str(home)
    assert body["rows"][0]["hash"] == _poll(client).json()["areas"]["topics"]["rows"]["t1"]
    assert client.get("/topics/rows", params={"ids": ",".join(f"t{n}" for n in range(201))}).status_code == 400
    assert client.get("/topics/rows", params={"ids": "t1,<b>"}).status_code == 400
    assert client.get("/topics/rows", params={"ids": "t1", "rev": "nope"}).status_code == 400
    stale = client.get("/topics/rows", params={"ids": "t1", "rev": "0" * 32}).json()
    assert stale["stale"] is True


def test_rows_past_the_byte_cap_are_deferred(monkeypatch):
    _seed()
    monkeypatch.setattr(sync, "ROWS_BYTE_CAP", 10)
    body = TestClient(app).get("/topics/rows", params={"ids": "t1,t2"}).json()
    assert [row["id"] for row in body["rows"]] == ["t1"]
    assert body["deferred"] == ["t2"]


# --------------------------------------------------------------------------- release discovery


def test_the_producer_keeps_discovery_going_and_the_poll_publishes_it(monkeypatch, clock):
    _seed()
    checker = releases.ReleaseChecker()
    monkeypatch.setattr(releases, "checker", checker)
    monkeypatch.setattr(releases, "_fetch_latest", lambda: "99.0.0")
    monkeypatch.setattr(services, "release_cached", releases.cached)
    client = TestClient(app)
    before = _revs(client)
    asked = threading.Event()

    def check():
        releases.release_status()
        asked.set()

    producer = releases.ReleaseProducer(check, interval=0.01, first_delay=0)
    producer.start()
    try:
        assert asked.wait(10)
    finally:
        producer.stop()
    after = _poll(client, before).json()
    assert after["areas"]["release"]["latest"] == "99.0.0"
    assert after["areas"]["release"]["available"] is True


# --------------------------------------------------------------------------- cost at 2,000 topics


def test_the_cost_at_two_thousand_topics_is_bounded(clock, capsys):
    """Measured on the whole request (middleware included), with a download history of ten files
    per topic and every tenth topic failing: printed, and bounded loosely for slow runners."""
    topics = [
        _topic(
            f"t{n:04d}",
            f"Show {n} / Сериал {n}",
            last_ok=n % 10 != 0,
            last_check="2026-01-01T00:00:00+00:00",
            hash=f"{n:040x}",
            **({"last_error": "timeout", "last_error_class": "tracker"} if n % 10 == 0 else {}),
        )
        for n in range(2000)
    ]
    save_state({"topics": topics})
    at = "2026-01-01T10:00:00+00:00"
    save_download_history(
        {
            "schema_version": 1,
            "topics": {
                topic["id"]: {
                    "items": {f"e{k}": {"label": f"S01E{k:02d}", "completed_observed_at": at} for k in range(10)},
                    "last_event": {"kind": "file_completed", "label": "S01E09", "at": at},
                }
                for topic in topics
            },
        }
    )
    client = TestClient(app)
    started = time.perf_counter()
    first = _poll(client)
    cold = time.perf_counter() - started
    assert first.status_code == 200
    etag = first.headers["etag"]
    started = time.perf_counter()
    for _ in range(20):
        assert client.get("/sync.json", headers={"If-None-Match": etag}).status_code == 304
    unchanged = (time.perf_counter() - started) / 20
    started = time.perf_counter()
    for _ in range(10):
        clock.tick()  # a new sampling cycle: every file read and hashed again, nothing changed
        assert client.get("/sync.json", headers={"If-None-Match": etag}).status_code == 304
    resampled = (time.perf_counter() - started) / 10
    path = services.sync_source_paths()["state"]
    _write_elsewhere(path, path.read_bytes().replace(b"Show 1999 ", b"Show 9999 "))
    clock.tick()
    started = time.perf_counter()
    changed = _poll(client, first.json()["rev"])
    one_change = time.perf_counter() - started
    assert changed.json()["areas"]["topics"]["changed"] == ["t1999"]
    ids = [topic["id"] for topic in topics[:200]]
    started = time.perf_counter()
    rows = client.post("/topics/rows", json={"ids": ids}, headers=ORIGIN)
    batch = time.perf_counter() - started
    assert rows.status_code == 200
    assert len(rows.json()["rows"]) == 200
    sizes = (path.stat().st_size, services.sync_source_paths()["history"].stat().st_size)
    with capsys.disabled():
        print(
            f"\n2,000 topics (state {sizes[0]} B, history {sizes[1]} B): cold {cold * 1000:.0f} ms,"
            f" 304 {unchanged * 1000:.1f} ms, 304 after a new sample {resampled * 1000:.1f} ms,"
            f" one change {one_change * 1000:.0f} ms ({len(changed.content)} B),"
            f" 200 rows {batch * 1000:.0f} ms ({len(rows.content)} B)"
        )
    assert unchanged < 0.25
    assert resampled < 1
    assert one_change < 10
    assert cold < 15
    assert batch < 10


def _request():
    from starlette.requests import Request

    return Request(
        {
            "type": "http",
            "method": "GET",
            "path": "/sync.json",
            "headers": [],
            "query_string": b"",
            "client": ("127.0.0.1", 50000),
            "server": ("127.0.0.1", 80),
            "scheme": "http",
        }
    )
