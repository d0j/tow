"""Personal cadence, durable dispatch, edits, limits and compact timer UI."""

import copy
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest
from bs4 import BeautifulSoup
from fastapi.testclient import TestClient
from test_supervisor import T0, make

from tow import check, cli, topic_timers
from tow.bundle import ExportImportError, _validate_state_topic
from tow.check import reconcile as check_reconcile
from tow.check import run as check_run
from tow.check_steps import merge_check_results, owner_fields
from tow.clients import factory as client_factory
from tow.errors import TowError
from tow.store import load_state, save_state, validate_state_bytes
from tow.supervisor import layout
from tow.supervisor.schedule import Schedule
from tow.web import app


def topic(tid="t1", minutes=10, at=T0, revision="a", **extra):
    return {
        "id": tid,
        "title": "Show A",
        "url": "http://rutor.info/torrent/1234567/show-a",
        "save_path": "D:/TV",
        "check_interval_min": minutes,
        "check_timer_set_at_ts": at,
        "check_timer_revision": revision,
        **extra,
    }


@pytest.mark.parametrize(
    ("raw", "expected"), [("", None), ("  ", None), ("1", 1), (" 37 ", 37), ("10080", 10080), ("0007", 7)]
)
def test_minute_input(raw, expected):
    assert topic_timers.parse_interval(raw) == expected


@pytest.mark.parametrize("raw", ["0", "10081", "-1", "1.5", "1e2", "+7", "true", "NaN", "７", "7\n8", "9" * 5000])
def test_bad_minute_input_is_bounded(raw):
    with pytest.raises(TowError):
        topic_timers.parse_interval(raw)


@pytest.mark.parametrize("value", [True, False, 0, -1, 10081, 2.5, "10", [], {}])
def test_bad_stored_minutes_rejected(value):
    with pytest.raises(TowError):
        topic_timers.interval_of({"check_interval_min": value})


@pytest.mark.parametrize(
    "extra",
    [
        {"check_timer_set_at_ts": True},
        {"check_timer_set_at_ts": float("nan")},
        {"check_timer_set_at_ts": -1},
        {"check_timer_set_at_ts": "123"},
        {"check_timer_revision": "a" * 129},
        {"check_timer_revision": []},
        {"check_timer_revision": "<script>"},
    ],
)
def test_bad_metadata_rejected(extra):
    with pytest.raises(ValueError, match="invalid timer"):
        topic_timers.interval_of(topic(**extra))


def test_policy_change_reanchors_but_unrelated_save_does_not(monkeypatch):
    item = topic()
    before = copy.deepcopy(item)
    assert not topic_timers.set_interval(item, 10)
    assert item == before
    monkeypatch.setattr(topic_timers.time, "time", lambda: T0 + 15)
    assert topic_timers.set_interval(item, 37)
    assert item["check_timer_set_at_ts"] == T0 + 15
    assert item["check_timer_revision"] != "a"
    assert topic_timers.set_interval(item, None)
    assert topic_timers.policy(item) is None


def test_only_active_custom_topics_enter_schedule():
    state = {"topics": [topic(), topic("default", None), topic("pause", paused=True), topic("done", once_done=True)]}
    assert list(topic_timers.active_policies(state)) == ["t1"]


def test_legacy_timer_has_stable_policy():
    assert topic_timers.policy({"check_interval_min": 7}) == topic_timers.policy({"check_interval_min": 7})


@pytest.mark.parametrize(
    "record",
    [
        None,
        [],
        {},
        {"revision": "wrong", "minutes": 10, "started_at": T0},
        {"revision": "a", "minutes": 9, "started_at": T0},
        {"revision": "a", "minutes": True, "started_at": T0},
        {"revision": "a", "minutes": 10, "started_at": float("inf")},
    ],
)
def test_unbound_attempt_is_not_used(record):
    assert topic_timers.reserved_at(topic_timers.policy(topic()), record) == 0


def test_new_deadline_and_durable_attempt():
    schedule = Schedule(started_at=T0)
    schedule.topic_timers = topic_timers.active_policies({"topics": [topic(minutes=37)]})
    assert schedule.timer_due(T0) == {"t1": T0 + 37 * 60}
    schedule.timer_attempts = {"t1": {"revision": "a", "minutes": 37, "started_at": T0 + 37 * 60}}
    assert schedule.timer_due(T0 + 40 * 60) == {"t1": T0 + 74 * 60}
    schedule.interval_sec = 60
    assert schedule.timer_due(T0 + 40 * 60) == {"t1": T0 + 74 * 60}


def test_real_start_after_backward_clock_replaces_future_policy_anchor():
    schedule = Schedule(started_at=T0 - 86400)
    schedule.topic_timers = topic_timers.active_policies({"topics": [topic(minutes=1, at=T0)]})
    assert schedule.timer_due(T0 - 86400)["t1"] == T0 - 86400 + 60
    started = T0 - 86400 + 60
    schedule.timer_attempts = {"t1": {"revision": "a", "minutes": 1, "started_at": started}}
    assert schedule.timer_due(started)["t1"] == started + 60
    assert schedule.timer_due(started + 1)["t1"] == started + 60


def test_other_batches_preserve_paused_topics_recent_attempt(tmp_path):
    world, sup = make(tmp_path)
    items = {"topics": [topic("paused", minutes=10, at=T0 - 3600, paused=True), topic(minutes=1, at=T0 - 60)]}
    world.facts.update(
        topic_timers=topic_timers.active_policies(items), timer_policies=topic_timers.all_policies(items)
    )
    sup.schedule.timer_attempts = {"paused": {"revision": "a", "minutes": 10, "started_at": T0 - 60}}
    sup._start_job("timer", T0, world.clock.mono)
    assert layout.read_json(layout.schedule_path())["timer_attempts"]["paused"]["started_at"] == T0 - 60
    items["topics"][0]["paused"] = False
    sup.schedule.topic_timers = topic_timers.active_policies(items)
    assert sup.schedule.timer_due(T0)["paused"] == T0 + 540


@pytest.mark.parametrize("jump", [60, 3600, 86400, 365 * 86400])
@pytest.mark.parametrize("source", ["policy", "attempt"])
def test_backward_clock_pinned_once(jump, source):
    schedule = Schedule(started_at=T0 - jump)
    schedule.topic_timers = topic_timers.active_policies(
        {"topics": [topic(minutes=7, at=T0 if source == "policy" else T0 - jump)]}
    )
    if source == "attempt":
        schedule.timer_attempts = {"t1": {"revision": "a", "minutes": 7, "started_at": T0}}
    for elapsed in [0, 1, 419, 420, 1000]:
        assert schedule.timer_due(T0 - jump + elapsed)["t1"] == T0 - jump + 420
    schedule.topic_timers = {}
    assert schedule.timer_due(T0) == {}
    assert not any(key.startswith("timer:") for key in schedule._corrected)


def test_supervisor_coalesces_sleep_and_persists_batch(tmp_path):
    world, sup = make(tmp_path)
    world.facts["topic_timers"] = topic_timers.active_policies(
        {"topics": [topic(minutes=1), topic("t2", 7), topic("t3", 10080)]}
    )
    sup.tick()
    world.clock.doze(3600)
    sup.tick()  # global job has priority
    sup.job.child.code = 0
    sup.tick()
    sup.tick()
    assert sup.job.name == "progress"
    sup.job.child.code = 0
    sup.tick()
    sup.tick()
    assert sup.job.name == "timer"
    assert "--timer-only" in sup.job.child.argv
    durable = layout.read_json(layout.schedule_path())
    assert set(durable["timer_batch"]) == {"t1", "t2"}
    assert durable["timer_attempts"]["t1"]["started_at"] == world.clock.wall
    assert sup.schedule.timer_due(world.clock.wall)["t1"] == world.clock.wall + 60
    new = type(sup)(world.deps(), python="py", home=tmp_path / "logs")
    new.tick()
    assert new.schedule.timer_due(world.clock.wall)["t1"] == world.clock.wall + 60


def test_only_one_timer_job_runs_and_spawn_failure_is_not_hot_retried(tmp_path):
    world, sup = make(tmp_path)
    world.facts["topic_timers"] = topic_timers.active_policies({"topics": [topic(minutes=1, at=T0 - 60)]})
    sup.tick()
    assert sup.job.name == "timer"
    world.clock.advance(180)
    sup.tick()
    assert len(world.jobs()) == 1
    sup.job.child.code = 1
    sup.tick()

    def refused(*_args):
        raise OSError("sensitive detail")

    sup.deps.spawn = refused
    sup._start_job("timer", world.clock.wall, world.clock.mono)
    assert sup.job is None
    assert sup.schedule.timer_due(world.clock.wall)["t1"] > world.clock.wall


def test_reservation_failure_never_spawns(tmp_path, monkeypatch):
    world, sup = make(tmp_path)
    world.facts["topic_timers"] = topic_timers.active_policies({"topics": [topic(minutes=1, at=T0 - 60)]})
    monkeypatch.setattr(layout, "write_json", lambda *_args: (_ for _ in ()).throw(OSError("disk")))
    sup._start_job("timer", T0, world.clock.mono)
    assert not world.jobs()
    assert sup.schedule.topic_timers == {}


def test_batch_rechecks_pause_delete_and_changed_policy():
    items = [topic(), topic("pause", paused=True), topic("done", once_done=True), topic("changed", revision="b")]
    records = {
        tid: {"revision": "a", "minutes": 10, "started_at": T0} for tid in ["t1", "pause", "done", "changed", "deleted"]
    }
    assert topic_timers.batch_ids({"topics": items}, {"timer_batch": records}) == ["t1"]


def fake_pipeline(monkeypatch, items):
    save_state(
        {
            "topics": items,
            "mirrors": {},
            "health": {"auto_at_ts": 100, "auto_ok_at_ts": 99},
            "daily_limit": {"rutor": check_run._today()},
        }
    )
    seen = []

    def row(item, run):
        seen.append((item["id"], set(run.quota)))
        return {"id": item["id"], "ok": True}

    monkeypatch.setattr(check_run, "check_topic", row)
    monkeypatch.setattr(check_run, "_progress_only_row", lambda item: {"id": item["id"], "ok": True})
    monkeypatch.setattr(client_factory, "from_secrets", lambda *_a, **_k: SimpleNamespace(ping=lambda: "ok"))
    monkeypatch.setattr(check_reconcile, "reconcile_topic", lambda *_a, **_k: {"events": []})
    return seen


def test_global_custom_manual_and_progress_scopes(monkeypatch):
    seen = fake_pipeline(monkeypatch, [topic("default", None), topic()])
    layout.write_json(
        layout.schedule_path(), {"timer_batch": {"t1": {"revision": "a", "minutes": 10, "started_at": T0}}}
    )
    assert [row["id"] for row in check.run_check(apply=True, notify=False, scheduled_scope="global")["results"]] == [
        "default"
    ]
    assert [
        row["id"] for row in check.run_check(apply=True, notify=False, scheduled_scope="timer", how="timer")["results"]
    ] == ["t1"]
    assert seen[-1][1] == {"rutor"}
    assert [row["id"] for row in check.run_check(apply=True, notify=False, how="manual")["results"]] == [
        "default",
        "t1",
    ]
    assert [
        row["id"] for row in check.run_check(apply=True, notify=False, progress_only=True, how="progress")["results"]
    ] == ["default", "t1"]


def test_empty_batch_does_not_check_all_and_dry_run_writes_nothing(monkeypatch):
    seen = fake_pipeline(monkeypatch, [topic()])
    from tow.paths import state_path

    before = state_path().read_bytes()
    assert check.run_check(apply=False, notify=True, scheduled_scope="timer", how="timer")["results"] == []
    assert seen == []
    assert state_path().read_bytes() == before
    assert check.run_check(apply=True, notify=False, ids=[])["results"] == []


def test_timer_does_not_mask_global_watchdog_or_move_manual_cadence(monkeypatch):
    fake_pipeline(monkeypatch, [topic()])
    layout.write_json(
        layout.schedule_path(), {"timer_batch": {"t1": {"revision": "a", "minutes": 10, "started_at": T0}}}
    )
    check.run_check(apply=True, notify=False, how="timer", scheduled_scope="timer")
    health = load_state()["health"]
    assert health["auto_at_ts"] == 100
    assert health["auto_ok_at_ts"] == 99
    before = layout.schedule_path().read_bytes()
    check.run_check(apply=True, notify=False, how="manual")
    assert layout.schedule_path().read_bytes() == before


def test_owner_edit_is_preserved_during_result_merge():
    previous = topic()
    stamp = owner_fields(previous)
    current = copy.deepcopy(previous)
    topic_timers.set_interval(current, 37)
    assert owner_fields(current) != stamp
    disk = {"topics": [current]}
    merge_check_results(disk, {"topics": [{**previous, "last_ok": True}]})
    assert disk["topics"][0]["check_interval_min"] == 37


def test_api_add_edit_clear_and_omission(monkeypatch):
    monkeypatch.setattr("tow.title.guess_topic_title", lambda *_a, **_k: "Synthetic series")
    monkeypatch.setattr("tow.web.services.run_check", lambda **_kw: {"qbit": "ok", "results": []})
    client = TestClient(app, headers={"Origin": "http://127.0.0.1"})
    data = {
        "url": "http://rutor.info/torrent/1234567/show-a",
        "title": "Show A",
        "save_path": "D:/TV",
        "check_interval_min": "37",
    }
    assert client.post("/topics/add", data=data, follow_redirects=False).status_code == 303
    item = load_state()["topics"][0]
    assert item["check_interval_min"] == 37
    tid = item["id"]
    edit = {"save_path": "D:/TV", "title": "Show B"}
    client.post(f"/topics/{tid}/edit", data=edit)
    assert load_state()["topics"][0]["check_timer_revision"] == item["check_timer_revision"]
    client.post(f"/topics/{tid}/edit", data={**edit, "check_interval_min": "7"})
    assert load_state()["topics"][0]["check_interval_min"] == 7
    client.post(f"/topics/{tid}/edit", data={**edit, "check_interval_min": ""})
    assert load_state()["topics"][0]["check_interval_min"] is None


@pytest.mark.parametrize("raw", ["0", "1.5", "10081", "9" * 5000])
def test_refused_add_retains_draft_but_saves_nothing(raw, monkeypatch):
    monkeypatch.setattr("tow.web.services.run_check", lambda **_kw: pytest.fail("no check on bad timer"))
    client = TestClient(app, headers={"Origin": "http://127.0.0.1"})
    page = client.post(
        "/topics/add", data={"url": "http://rutor.info/torrent/1234567/show-a", "check_interval_min": raw}
    ).text
    assert not load_state()["topics"]
    field = BeautifulSoup(page, "html.parser").select_one("#topic-check-interval")
    assert field["value"] == raw
    assert field["aria-invalid"] == "true"


def test_bad_edit_preserves_everything_and_undo_restores_policy():
    save_state({"topics": [topic()], "mirrors": {}})
    client = TestClient(app, headers={"Origin": "http://127.0.0.1"})
    before = load_state()
    client.post("/topics/t1/edit", data={"save_path": "D:/TV", "check_interval_min": "0"})
    assert load_state() == before
    client.post("/topics/t1/edit", data={"save_path": "D:/TV", "check_interval_min": "37"})
    assert load_state()["topics"][0]["check_interval_min"] == 37
    client.post("/undo")
    assert load_state()["topics"][0]["check_interval_min"] == 10
    assert load_state()["topics"][0]["check_timer_revision"] == "a"


def test_store_and_import_reject_bad_policy_before_apply():
    with pytest.raises(ValueError, match="invalid topic check interval"):
        validate_state_bytes(b'{"topics":[{"check_interval_min":true}]}')
    with pytest.raises(ExportImportError):
        _validate_state_topic(0, {"check_interval_min": 0})


@pytest.mark.parametrize(
    ("phase", "extra"), [("paused", {"paused": True}), ("done", {"once_done": True}), ("stopped", {})]
)
def test_status_never_invents_deadline_when_stopped(monkeypatch, phase, extra):
    monkeypatch.setattr(layout, "status", dict)
    status = layout.topic_timer_status({"topics": [topic(**extra)]})["t1"]
    assert status["state"] == phase
    assert status["next_at"] == 0


@pytest.mark.parametrize(("at", "phase"), [(T0 + 60, "scheduled"), (T0 - 60, "queued")])
def test_status_uses_authoritative_deadline_not_old_report_age(monkeypatch, at, phase):
    monkeypatch.setattr("time.time", lambda: T0)
    report = {
        "at": datetime.fromtimestamp(T0 - 3600, UTC).isoformat(),
        "topic_timers": {"t1": {"revision": "a", "at": datetime.fromtimestamp(at, UTC).isoformat()}},
    }
    monkeypatch.setattr(layout, "status", lambda: report)
    assert layout.topic_timer_status({"topics": [topic()]})["t1"]["state"] == phase


def test_ui_and_health_show_only_custom_timers():
    save_state({"topics": [topic(), topic("default", None), topic("pause", paused=True)], "mirrors": {}})
    client = TestClient(app)
    page = BeautifulSoup(client.get("/").text, "html.parser")
    assert len(page.select("[data-topic-timer]")) == 2
    assert not page.select("[data-clock-label]")
    assert page.select_one("#topic-check-interval")["type"] == "number"
    assert page.select_one("[data-topic-timer]").parent.select_one("[data-hl]")
    assert not page.select_one("[data-topic-timer]").find_parent(attrs={"data-hl": True})
    assert client.get("/topics/t1/edit").status_code == 200
    health = client.get("/health.json").json()
    assert set(health["topic_timers"]) == {"t1", "pause"}
    assert health["topic_timers"]["pause"]["state"] == "paused"


def test_personal_timer_wrapper_reserves_mobile_control_space():
    from pathlib import Path

    css = (Path(__file__).parents[1] / "src/tow/static/app.css").read_text(encoding="utf-8")
    assert "details.row-edit > summary > .topic-name,\n" in css


def test_invalid_scope_or_progress_scope_is_refused_before_io():
    for scope, progress in [("unknown", False), ("global", True), ("timer", True)]:
        with pytest.raises(ValueError, match="invalid scheduled check scope"):
            check.run_check(apply=False, notify=False, scheduled_scope=scope, progress_only=progress)


def test_running_and_new_policy_status(monkeypatch):
    monkeypatch.setattr("time.time", lambda: T0)
    layout.write_json(
        layout.schedule_path(), {"timer_batch": {"t1": {"revision": "a", "minutes": 10, "started_at": T0}}}
    )
    report = {
        "topic_timers": {"t1": {"revision": "a", "at": datetime.fromtimestamp(T0 + 600, UTC).isoformat()}},
        "job": {"name": "timer"},
    }
    monkeypatch.setattr(layout, "status", lambda: report)
    assert layout.topic_timer_status({"topics": [topic()]})["t1"]["state"] == "running"
    changed = topic(revision="b")
    result = layout.topic_timer_status({"topics": [changed]})["t1"]
    assert result["state"] == "waiting"
    assert result["next_at"] == 0


def test_many_timers_follow_independent_minute_intervals_without_changing_global():
    schedule = Schedule(started_at=T0, interval_sec=43200)
    items = {"topics": [topic(f"t{i}", minutes=1 + i % 73) for i in range(1000)]}
    schedule.topic_timers = topic_timers.active_policies(items)
    for step in range(1, 361):
        now = T0 + step * 60
        for tid, at in schedule.timer_due(now).items():
            minutes = schedule.topic_timers[tid]["minutes"]
            if at <= now:
                assert step % minutes == 0
                schedule.timer_attempts[tid] = {"revision": "a", "minutes": minutes, "started_at": now}
        assert all(at > now for at in schedule.timer_due(now).values())
        assert schedule.check_due_at(now, T0) == T0 + 43200


def test_reassigned_policy_discards_previous_clock_anchor():
    schedule = Schedule(started_at=T0)
    schedule.topic_timers = topic_timers.active_policies({"topics": [topic(at=T0 + 86400)]})
    assert schedule.timer_due(T0)["t1"] == T0 + 600
    schedule.topic_timers = topic_timers.active_policies({"topics": [topic(minutes=7, at=T0 + 10, revision="b")]})
    assert schedule.timer_due(T0 + 10)["t1"] == T0 + 430
    assert set(schedule._corrected).issubset({"timer:t1:b"})


@pytest.mark.parametrize(
    ("args", "scope", "how"),
    [(["--global-only"], "global", "auto"), (["--timer-only"], "timer", "timer"), (["--manual"], "", "manual")],
)
def test_cli_scheduled_modes(monkeypatch, args, scope, how):
    observed = []
    monkeypatch.setattr(check, "run_check", lambda **kw: observed.append(kw) or {"results": [], "qbit": "ok"})
    assert cli.main(["check", "--json", *args]) == 0
    assert observed[0]["scheduled_scope"] == scope
    assert observed[0]["how"] == how
