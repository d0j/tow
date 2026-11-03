"""A confirmed sign-in waits quietly only for an actual ordinary check, never an extra download."""

from __future__ import annotations

from copy import deepcopy
from datetime import UTC, datetime

import pytest

from tow import auth_observations, issues, site_retries
from tow.issues.kinds import site_needs_login
from tow.issues.model import Snapshot

NOW = 1_800_000_000.0


def iso(stamp):
    return datetime.fromtimestamp(stamp, UTC).isoformat()


def world():
    cfg = {
        "interval_sec": 43200,
        "trackers": {
            "demo": {
                "title": "Demo",
                "url_regex": r"^https://tracker\.example/topic/(\d+)$",
                "fetch_hosts": ["https://tracker.example"],
                "login_path": "/login",
            }
        },
    }
    topic = {
        "id": "a",
        "url": "https://tracker.example/topic/1",
        "last_error": "synthetic old login refusal",
        "last_error_class": "tracker_auth",
        "last_check": iso(NOW),
        "auth_error_revision": "a" * 32,
    }
    state = {"topics": [topic], "mirrors": {"demo": {"signed_in_at": NOW + 1}}}
    state["mirrors"]["demo"]["signed_in_refusals"] = auth_observations.accept(
        state, cfg, "demo", auth_observations.capture(state, cfg)
    )
    report = {"next": {"check": iso(NOW + 600)}}
    return state, cfg, report


def collect(state, cfg, report, schedule=None, *, now=NOW + 2):
    facts = site_retries.recovery_status(state, cfg, report=report, schedule=schedule or {}, now=now)
    snapshot = Snapshot(state, cfg, now, site_retries=facts)
    return facts, site_needs_login.collect(snapshot)


def test_verified_signin_with_a_real_future_global_check_is_a_note_without_any_write_or_new_request():
    state, cfg, report = world()
    before = deepcopy((state, cfg, report))
    facts, [issue] = collect(state, cfg, report)
    assert facts["a"]["reason"] == "auth_confirmation"
    assert facts["a"]["next_at"] == NOW + 600
    assert issue.message.key == "issues.site_signed_in"
    assert issue.requires_attention is False
    assert issue.fix is None
    assert issues.attention([issue]) == []
    assert (state, cfg, report) == before


@pytest.mark.parametrize(
    "change", ["no_supervisor", "stopping", "no_plan", "expired", "far_future", "failed_after_login"]
)
def test_missing_stale_or_inapplicable_ordinary_plan_never_suppresses_auth_attention(change):
    state, cfg, report = world()
    if change == "no_supervisor":
        report = {}
    elif change == "stopping":
        report["stopping"] = True
    elif change == "no_plan":
        report = {"next": {"progress": iso(NOW + 10)}}
    elif change == "expired":
        report["next"]["check"] = iso(NOW + 1)
    elif change == "far_future":
        report["next"]["check"] = iso(NOW + 86400 * 10)
    else:
        state["topics"][0]["last_check"] = iso(NOW + 2)
        state["topics"][0]["auth_error_revision"] = "b" * 32
    facts, [issue] = collect(state, cfg, report, now=NOW + 100)
    assert facts == {}
    assert issue.requires_attention is True


def test_a_personal_timer_is_not_mistaken_for_the_global_check_that_excludes_it():
    state, cfg, report = world()
    topic = state["topics"][0]
    topic.update(check_interval_min=5, check_timer_revision="personal", check_timer_set_at_ts=NOW)
    assert collect(state, cfg, report)[0] == {}
    report["topic_timers"] = {"a": {"at": iso(NOW + 300), "revision": "personal"}}
    facts, [issue] = collect(state, cfg, report)
    assert facts["a"]["next_at"] == NOW + 300
    assert issue.requires_attention is False
    report["topic_timers"]["a"]["revision"] = "older timer"
    assert collect(state, cfg, report)[0] == {}


def test_current_ordinary_job_is_pending_only_with_a_real_deadline_and_postsignin_start():
    state, cfg, report = world()
    report = {"job": {"name": "check", "pid": 42, "since": iso(NOW + 10), "deadline": iso(NOW + 3610)}}
    facts, [issue] = collect(state, cfg, report, now=NOW + 12)
    assert facts["a"]["phase"] == "running"
    assert issue.requires_attention is False
    assert collect(state, cfg, report, now=NOW + 3610)[0] == {}
    report["job"]["since"] = iso(NOW)
    report["job"]["deadline"] = iso(NOW + 3600)
    assert collect(state, cfg, report, now=NOW + 12)[0] == {}


def test_a_changed_context_cannot_reuse_a_precomputed_confirmation_note():
    state, cfg, report = world()
    facts, _found = collect(state, cfg, report)
    state["topics"][0]["client_id"] = "another"
    snapshot = Snapshot(state, cfg, NOW + 2, site_retries=facts)
    [issue] = site_needs_login.collect(snapshot)
    assert issue.requires_attention is True


def test_a_new_auth_failure_in_the_signin_second_never_inherits_old_confirmation():
    state, cfg, report = world()
    state["mirrors"]["demo"]["signed_in_at"] = NOW + 0.25
    # A refusal at .75 is serialized as the same ISO second as the earlier .25 sign-in.
    state["topics"][0]["last_check"] = iso(NOW)
    state["topics"][0]["auth_error_revision"] = "b" * 32
    facts, [issue] = collect(state, cfg, report, now=NOW + 1)
    assert facts == {}
    assert issue.requires_attention is True
    assert issue.message.key == "issues.site_needs_login"
    assert issue.fix.action == "sign_in"


def test_a_refusal_100ms_before_confirmation_in_the_same_second_waits_for_its_real_plan():
    state, cfg, report = world()
    state["mirrors"]["demo"]["signed_in_at"] = NOW + 0.1
    facts, [issue] = collect(state, cfg, report, now=NOW + 0.2)
    assert facts["a"]["reason"] == "auth_confirmation"
    assert issue.requires_attention is False


def test_confirmed_site_configuration_change_does_not_suppress_a_refusal():
    state, cfg, report = world()
    cfg["trackers"]["demo"]["login_path"] = "/another-login"
    assert collect(state, cfg, report)[0] == {}


@pytest.mark.parametrize("legacy", [False, True])
def test_a_late_callback_only_accepts_refusals_captured_before_the_check_lock_was_released(legacy):
    from tow.check.rows import stamp_result

    state, cfg, report = world()
    topic = state["topics"][0]
    if legacy:
        topic.pop("auth_error_revision")
    captured = auth_observations.capture(state, cfg)
    # Another applying check obtained the lock after the confirming run and before the
    # web follow-up. Even if its time string is identical, it is a different observation.
    stamp_result(topic, {"ok": False, "error": "new refusal", "error_class": "tracker_auth"})
    topic["last_check"] = iso(NOW)
    proof = auth_observations.accept(state, cfg, "demo", captured)
    assert proof["topics"] == {}
    state["mirrors"]["demo"]["signed_in_refusals"] = proof
    facts, [issue] = collect(state, cfg, report)
    assert facts == {}
    assert issue.requires_attention is True


def test_legacy_capture_is_assigned_a_nonce_only_while_its_exact_snapshot_is_current():
    state, cfg, _report = world()
    topic = state["topics"][0]
    topic.pop("auth_error_revision")
    captured = auth_observations.capture(state, cfg)
    proof = auth_observations.accept(state, cfg, "demo", captured)
    assert auth_observations.revision(topic["auth_error_revision"])
    assert proof["topics"]["a"] == topic["auth_error_revision"]
    topic.pop("auth_error_revision")
    topic["last_error"] = "different legacy observation"
    assert auth_observations.accept(state, cfg, "demo", captured)["topics"] == {}
    assert "auth_error_revision" not in topic


def test_run_captures_refusal_generations_before_releasing_its_applying_lock(monkeypatch):
    from contextlib import contextmanager

    from tow.check import run

    state, cfg, _report = world()
    locked = []

    @contextmanager
    def check_lock(**_kwargs):
        locked.append(True)
        try:
            yield
        finally:
            locked.pop()

    original = auth_observations.capture

    def capture(*args):
        assert locked
        return original(*args)

    monkeypatch.setattr(run, "check_run_lock", check_lock)
    monkeypatch.setattr(
        run,
        "_run_check",
        lambda **_kw: {"results": [{"id": "confirmed", "ok": True, "tracker": "demo", "site_answered": True}]},
    )
    monkeypatch.setattr(run, "load_state", lambda **_kw: state)
    monkeypatch.setattr(run, "load_config", lambda: cfg)
    monkeypatch.setattr(auth_observations, "capture", capture)
    out = run.run_check(apply=True, notify=False, ids=["confirmed"], how="manual")
    assert out["auth_refusals"]["demo"]["topics"]["a"]["revision"] == "a" * 32
    assert not locked


@pytest.mark.parametrize("legacy", [False, True])
def test_signin_note_persists_only_current_captured_generations_under_the_data_lock(monkeypatch, legacy):
    from tow import config, mirrors
    from tow.store import load_state, save_state

    state, cfg, report = world()
    state["mirrors"] = {}
    topic = state["topics"][0]
    if legacy:
        topic.pop("auth_error_revision")
    captured = auth_observations.capture(state, cfg)
    save_state(state)
    monkeypatch.setattr(config, "load_config", lambda: cfg)
    monkeypatch.setattr(mirrors, "_now", lambda: NOW + 0.1)
    mirrors.note_signed_in("demo", captured=captured)
    saved = load_state()
    assert auth_observations.revision(saved["topics"][0]["auth_error_revision"])
    assert collect(saved, cfg, report, now=NOW + 0.2)[1][0].requires_attention is False
    # A check completed after capture but before the UI callback receives a new UUID.
    saved["topics"][0]["auth_error_revision"] = "f" * 32
    save_state(saved)
    mirrors.note_signed_in("demo", captured=captured)
    saved = load_state()
    assert saved["mirrors"]["demo"]["signed_in_refusals"]["topics"] == {}
    assert collect(saved, cfg, report, now=NOW + 0.2)[1][0].requires_attention is True


def test_a_skipped_or_cached_login_result_without_a_site_capture_does_not_confirm_auth(monkeypatch):
    from tow.web import routes_topic_login, services

    monkeypatch.setattr(services, "note_signed_in", lambda *a, **kw: pytest.fail("a site was not checked"))
    routes_topic_login._signed_in("demo", {"results": [{"ok": True, "status": "skipped"}]})
