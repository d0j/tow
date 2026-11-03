"""Authorized replacement recovery shares the finite, durable check retry budget."""

from __future__ import annotations

from datetime import UTC, datetime

import pytest
import test_site_retry_runs as retry_runs

from tow import site_retries
from tow.check.rows import set_error, stamp_result
from tow.errors import Msg, TowError
from tow.supervisor.schedule import Schedule

T0 = retry_runs.T0


def config():
    return {
        "replace_previous_revision": True,
        "trackers": {
            "demo": {
                "fetch_hosts": ["https://demo.example"],
                "url_regex": r"^https://demo\.example/topic/([a-z]+)$",
                "download_path": "/download/{id}",
            }
        },
    }


def waiting():
    error = TowError("check.previous_revision_active", file="Show/e01.mkv", hash="B" * 40)
    record = error.record()
    return {
        "id": "a",
        "url": "https://demo.example/topic/a",
        "hash": "A" * 40,
        "save_path": "D:/TV",
        "client_id": "main",
        "last_check": datetime.fromtimestamp(T0, UTC).isoformat(),
        "last_error": str(error),
        "last_error_code": record["code"],
        "last_error_params": record["params"],
        "last_error_class": "qbit",
    }


@pytest.mark.parametrize(
    ("global_choice", "topic_choice", "planned"),
    [(True, "", True), (False, "on", True), (True, "off", False), (False, "", False)],
)
def test_only_the_effective_owner_permission_authorizes_replacement_recovery(global_choice, topic_choice, planned):
    cfg, topic = config(), waiting()
    cfg["replace_previous_revision"] = global_choice
    topic["replace_previous"] = topic_choice
    policies = site_retries.policies({"topics": [topic]}, cfg, now=T0)
    assert bool(policies) is planned
    if planned:
        due = Schedule(T0, interval_sec=43200, site_retries=policies).site_retry_due(T0)["a"]
        assert T0 + 300 <= due <= T0 + 330


@pytest.mark.parametrize(
    "change",
    [
        "off",
        "global_off",
        "pause",
        "delete",
        "success",
        "revision",
        "current_hash",
        "url",
        "folder",
        "client",
        "selection",
    ],
)
def test_reserved_replacement_batch_cannot_act_on_changed_owner_intent_or_evidence(change):
    cfg, topic = config(), waiting()
    state = {"topics": [topic]}
    item = site_retries.policies(state, cfg, now=T0)["a"]
    batch = {
        "site_retry_batch": {
            "a": {"revision": item["revision"], "observed_at": item["observed_at"], "count": 1, "started_at": T0 + 400}
        }
    }
    assert site_retries.batch_ids(state, cfg, batch) == ["a"]
    if change == "off":
        topic["replace_previous"] = "off"
    elif change == "global_off":
        cfg["replace_previous_revision"] = False
    elif change == "pause":
        topic["paused"] = True
    elif change == "delete":
        state["topics"] = []
    elif change == "success":
        topic["last_error"] = None
    elif change == "revision":
        topic["last_error_params"]["hash"] = "C" * 40
    else:
        field, value = {
            "current_hash": ("hash", "D" * 40),
            "url": ("url", "https://demo.example/topic/b"),
            "folder": ("save_path", "D:/other"),
            "client": ("client_id", "other"),
            "selection": ("selection", {"mode": "files", "value": "*.mkv"}),
        }[change]
        topic[field] = value
    assert site_retries.batch_ids(state, cfg, batch) == []


def test_repeated_progress_and_changed_stop_causes_do_not_reset_the_durable_budget(monkeypatch):
    cfg, topic = config(), waiting()
    monkeypatch.setattr(retry_runs, "config", lambda: cfg)
    service = retry_runs.Service(topic)
    revision = site_retries.policies({"topics": [topic]}, cfg, now=T0)["a"]["revision"]
    for count in (1, 2, 3):
        assert service.dispatch()
        assert retry_runs.saved_attempt()["count"] == count
        topic["last_check"] = datetime.fromtimestamp(service.now + 60, UTC).isoformat()
        topic["last_error_params"]["reason"] = {
            "$msg": {"code": "client.managed.error_state", "params": {"state": f"synthetic-{count}"}}
        }
        assert site_retries.policies({"topics": [topic]}, cfg, now=service.now)["a"]["revision"] == revision
        service.start()  # a new supervisor reads the same durable attempts
    assert not service.dispatch()
    topic["replace_previous"] = "off"
    assert service.due() is None
    topic.pop("replace_previous")
    assert service.due() is None  # disabling and re-enabling does not mint more retries
    topic["replace_previous"] = "on"
    assert service.due() is None  # the same effective permission is not a new policy


def test_alternating_site_and_stop_failures_keep_one_budget_and_recovery_clears_the_hint(monkeypatch):
    cfg, topic = config(), waiting()
    monkeypatch.setattr(retry_runs, "config", lambda: cfg)
    service = retry_runs.Service(topic)
    revision = site_retries.policies({"topics": [topic]}, cfg, now=T0)["a"]["revision"]
    failures = [
        TowError("mirrors.all_failed", tracker="demo", error=TowError("mirrors.transport", error="synthetic")),
        TowError("check.previous_revision_active", file="Show/e01.mkv", hash="B" * 40),
        TowError("check.client_unreachable", reason=Msg("doctor.reason.refused")),
    ]
    for count, error in enumerate(failures, 1):
        assert service.dispatch()
        row = {"ok": False}
        set_error(row, error)
        stamp_result(topic, row)
        assert topic["replacement_pending_hash"] == "B" * 40
        assert site_retries.policies({"topics": [topic]}, cfg, now=service.now)["a"]["revision"] == revision
        assert retry_runs.saved_attempt()["count"] == count
        service.start()
    assert not service.dispatch()
    stamp_result(topic, {"ok": True})
    assert "replacement_pending_hash" not in topic
    assert service.due() is None


@pytest.mark.parametrize("restriction", ["frozen", "server_pause"])
def test_replacement_recovery_obeys_site_pause_and_cooldown(restriction):
    cfg, topic = config(), waiting()
    bucket = {"frozen": True} if restriction == "frozen" else {"rate_cool": {"https://demo.example": T0 + 86400}}
    policies = site_retries.policies({"topics": [topic], "mirrors": {"demo": bucket}}, cfg, now=T0)
    if restriction == "frozen":
        assert not policies
    else:
        assert Schedule(T0, site_retries=policies).site_retry_due(T0)["a"] == T0 + 86400
