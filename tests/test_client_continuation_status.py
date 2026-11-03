"""Owner attention follows a live bounded client plan, never a saved pending flag alone."""

from __future__ import annotations

from copy import deepcopy
from datetime import UTC, datetime

import pytest
from test_file_repair import advance, world

from tow import file_repair
from tow.check import client_continuation_matches, client_continuation_status
from tow.check import run as check_run
from tow.check.rows import client_marks
from tow.errors import TowError
from tow.issues import Snapshot
from tow.issues.kinds import client_down, files_missing, topic_error
from tow.store import load_state
from tow.supervisor import layout

NOW = 1000.0
CFG = {"clients": {"main": {"kind": "qbittorrent", "default": True}}}


def iso(value):
    return datetime.fromtimestamp(value, UTC).isoformat()


def planned(**extra):
    return {"at": iso(NOW - 10), "next": {"space": iso(NOW + 60)}, **extra}


def project(state, *, cfg=None, report=None, now=NOW):
    return client_continuation_status(
        state, CFG if cfg is None else cfg, report=planned() if report is None else report, now=now
    )


def sample(topic, plans=None, *, config=None, health=None, now=NOW):
    return Snapshot(
        state={"topics": [topic], "health": health or {}},
        config=config or CFG,
        now=now,
        client_continuations=plans or {},
    )


def requested(tmp_path, monkeypatch):
    client, clock = world(tmp_path, monkeypatch)
    file_repair.request("synthetic")
    return client, clock, load_state()


def test_actual_client_plan_is_bound_read_only_and_keeps_missing_evidence(tmp_path, monkeypatch):
    _client, _clock, state = requested(tmp_path, monkeypatch)
    before = deepcopy(state)
    entry = project(state)["synthetic"]
    assert entry["phase"] == "scheduled"
    assert entry["next_at"] == NOW + 60
    assert entry["deadline"] == NOW + 120
    assert entry["kind"] == "repair"
    assert client_continuation_matches(state["topics"][0], CFG, entry, now=NOW)
    [issue] = files_missing.collect(sample(state["topics"][0], {"synthetic": entry}))
    assert not issue.requires_attention
    assert state == before


def test_saved_active_flag_without_supervisor_proof_retains_attention_and_a_working_check(tmp_path, monkeypatch):
    _client, _clock, state = requested(tmp_path, monkeypatch)
    assert project(state, report={}) == {}
    [issue] = files_missing.collect(sample(state["topics"][0]))
    assert issue.requires_attention
    assert issue.fix.action == "check_topic"


@pytest.mark.parametrize("change", ["stop", "expired_plan", "far_future", "bad_time", "bad_job", "blocked_owner"])
def test_stale_or_unbounded_facts_cannot_hide_missing_files(tmp_path, monkeypatch, change):
    _client, _clock, state = requested(tmp_path, monkeypatch)
    report = planned()
    if change == "stop":
        report["stopping"] = True
    elif change == "expired_plan":
        report["next"]["space"] = iso(NOW - 60)
    elif change == "far_future":
        report["next"]["space"] = iso(NOW + 86400)
    elif change == "bad_time":
        report["next"]["space"] = "bad"
    elif change == "bad_job":
        report = {"job": {"name": "space", "pid": True, "since": iso(NOW), "deadline": iso(NOW + 600)}}
    else:
        state["topics"][0]["repair_pending"].update(phase="blocked", blocked="repair.client_stopped")
    assert project(state, report=report) == {}


def test_finite_active_job_and_blocker_extend_only_their_real_budget(tmp_path, monkeypatch):
    _client, _clock, state = requested(tmp_path, monkeypatch)
    report = {"job": {"name": "space", "pid": 42, "since": iso(NOW), "deadline": iso(NOW + 600)}}
    entry = project(state, report=report)["synthetic"]
    assert entry["phase"] == "running"
    assert entry["deadline"] == NOW + 600
    assert project(state, report=report, now=NOW + 600) == {}
    report = planned(job={"name": "backup", "pid": 42, "since": iso(NOW), "deadline": iso(NOW + 1800)})
    assert project(state, report=report, now=NOW + 121)["synthetic"]["deadline"] == NOW + 1860
    assert project(state, report=report, now=NOW + 1800) == {}


@pytest.mark.parametrize("change", ["paused", "selection", "generation", "request", "default", "disabled"])
def test_snapshot_cannot_quiet_a_changed_owner_or_connection(tmp_path, monkeypatch, change):
    _client, _clock, state = requested(tmp_path, monkeypatch)
    topic = state["topics"][0]
    entry = project(state)["synthetic"]
    cfg = deepcopy(CFG)
    if change == "paused":
        topic["paused"] = True
    elif change == "selection":
        topic["selection"] = {"mode": "files", "value": "different.mkv"}
    elif change == "generation":
        topic["file_observation_revision"] = "different"
    elif change == "request":
        topic["repair_request_revision"] = "different"
    elif change == "default":
        topic.pop("client_id", None)
        cfg["clients"] = {"other": {"kind": "qbittorrent", "default": True}}
    else:
        cfg["clients"]["main"]["enabled"] = False
    assert not client_continuation_matches(topic, cfg, entry, now=NOW)


def test_legacy_false_start_failure_is_pending_only_with_actual_read_only_followup(tmp_path, monkeypatch):
    client, _clock, _state = requested(tmp_path, monkeypatch)
    advance(client)
    client.finish_check()
    advance(client)
    state = load_state()
    receipt = state["topics"][0]["repair_pending"]
    receipt.update(phase="blocked", blocked="repair.resume_unconfirmed", running_seen=False)
    assert project(state)["synthetic"]["kind"] == "repair"
    assert project(state, report={}) == {}
    receipt["blocked"] = "repair.client_stopped"
    assert project(state) == {}


@pytest.mark.parametrize("kind", ["verification", "space"])
def test_existing_client_waits_have_context_and_original_twelve_hour_bounds(kind):
    topic = {
        "id": "a",
        "hash": "a" * 40,
        "client_id": "main",
        "url": "magnet:synthetic",
        "save_path": "/synthetic",
        "selection": {"mode": "all"},
    }
    waiting = {"hash": topic["hash"], "client": "main", "since": iso(NOW), "client_generation": None}
    if kind == "verification":
        waiting.update(path=topic["save_path"], owner=list(client_marks(topic)), receipt={"wanted": [0]})
        topic["verification_pending"] = waiting
    else:
        waiting["selection"] = "native-file-selection"
        topic["waiting_space"] = waiting
    state = {"topics": [topic]}
    assert project(state)["a"]["kind"] == kind
    assert project(state, now=NOW + 43200, report={"next": {"space": iso(NOW + 43201)}}) == {}
    waiting["client_generation"] = "other-endpoint"
    assert project(state) == {}


def test_one_malformed_topic_or_receipt_does_not_hide_other_pending_work(tmp_path, monkeypatch):
    _client, _clock, state = requested(tmp_path, monkeypatch)
    broken = deepcopy(state["topics"][0])
    broken["id"] = "bad"
    broken["repair_pending"]["retry_error"] = ["bad"]
    state["topics"].insert(0, broken)
    assert set(project(state)) == {"synthetic"}


def test_removed_client_and_malformed_snapshot_are_not_pending_authority(tmp_path, monkeypatch):
    _client, _clock, state = requested(tmp_path, monkeypatch)
    entry = project(state)["synthetic"]
    topic = state["topics"][0]
    removed = {"clients": {"different": {"kind": "qbittorrent", "default": True}}}
    assert not file_repair.current(topic, file_repair.receipt_of(topic), removed)
    assert project(state, cfg=removed) == {}
    assert not client_continuation_matches(topic, removed, entry, now=NOW)
    entry["phase"] = ["malformed"]
    assert not client_continuation_matches(topic, CFG, entry, now=NOW)


def test_server_retry_after_can_wait_for_a_real_maintenance_pass_without_early_rpc(tmp_path, monkeypatch):
    from types import SimpleNamespace

    client, clock, _state = requested(tmp_path, monkeypatch)

    def temporarily_unavailable(h):
        error = OSError("synthetic HTTP refusal")
        error.response = SimpleNamespace(status_code=503, headers={"retry-after": "3600"})
        raise error

    inspect = client.inspect_torrent
    monkeypatch.setattr(client, "inspect_torrent", temporarily_unavailable)
    advance(client)
    state = load_state()
    receipt = state["topics"][0]["repair_pending"]
    assert receipt["next_at"] == NOW + 3600
    entry = project(state, report={"next": {"space": iso(NOW + 300)}})["synthetic"]
    assert entry["next_at"] == NOW + 300  # actual observer, not a promised native RPC
    assert entry["reason"] == "repair.waiting_client"
    [issue] = files_missing.collect(sample(state["topics"][0], {"synthetic": entry}))
    assert not issue.requires_attention
    monkeypatch.setattr(client, "inspect_torrent", inspect)
    monkeypatch.setattr(check_run, "load_config", lambda: CFG)
    clock["now"] = NOW + 300
    for _ in range(2):
        result = check_run.run_check(apply=True, notify=False, progress_only=True)
        assert result["results"][0]["status"] == "repair_pending"
    assert client.calls == []
    after = load_state()["topics"][0]["repair_pending"]
    assert after["next_at"] == receipt["next_at"]
    assert after["deadline"] == receipt["deadline"]


@pytest.mark.parametrize("native_error", [True, False])
def test_client_plan_quiets_only_native_transport_errors_and_never_site_failure(tmp_path, monkeypatch, native_error):
    _client, _clock, state = requested(tmp_path, monkeypatch)
    topic = state["topics"][0]
    error = (
        TowError("check.client_unreachable", reason=TowError("doctor.reason.timeout"))
        if native_error
        else TowError("mirrors.cloudflare")
    )
    topic.update(
        last_error=str(error),
        last_error_code=error.code,
        last_error_params=error.record()["params"],
        last_error_class=error.error_class,
    )
    snap = sample(topic, project(state), health={"qbit_ok": False})
    result = client_down.collect(snap) if native_error else topic_error.collect(snap)
    assert result
    assert result[0].requires_attention is not native_error


def test_layout_uses_real_live_status_and_never_creates_its_own_job(tmp_path, monkeypatch):
    _client, _clock, state = requested(tmp_path, monkeypatch)
    monkeypatch.setattr(layout, "status", planned)
    assert layout.client_continuation_status(state, CFG)["synthetic"]["phase"] == "scheduled"
    monkeypatch.setattr(layout, "status", dict)
    assert layout.client_continuation_status(state, CFG) == {}
