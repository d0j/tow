"""Site-outage timing, durable dispatch and revalidation without real network or processes."""

from __future__ import annotations

import copy
from datetime import UTC, datetime
from types import SimpleNamespace

import pytest

from tow import errors, i18n, site_retries
from tow.check.notices import RunNotifications
from tow.config import load_config
from tow.delivery import compose
from tow.notify import NotificationBatch, PendingNotification
from tow.protection import chain
from tow.protection.spec import AccessFailure, ProviderUnavailableError
from tow.supervisor import core, layout
from tow.supervisor.schedule import Schedule
from tow.topic_timers import retry_deadline

T0 = datetime(2026, 10, 14, 9, tzinfo=UTC).timestamp()


def failed(identity="a", reason="mirrors.cloudflare"):
    error = errors.TowError("mirrors.all_failed", tracker="demo", error=errors.TowError(reason))
    record = error.record()
    return {
        "id": identity,
        "url": f"https://demo.example/topic/{identity}",
        "hash": "A" * 40,
        "title": "Show A",
        "last_error": str(error),
        "last_error_code": record["code"],
        "last_error_params": record["params"],
        "last_error_class": "tracker",
        "last_check": datetime.fromtimestamp(T0, UTC).isoformat(),
    }


def config():
    return {"trackers": {"demo": {"fetch_hosts": ["https://demo.example"]}}, "protection": {"min_interval_sec": 1800}}


def plan(*topics):
    return site_retries.policies({"topics": list(topics), "mirrors": {}}, config())


@pytest.mark.parametrize("cls", ["auth", "tracker_auth", "quota", "qbit", "gone", "disk", "error", "frozen"])
def test_no_retry_for_owner_action_errors(cls):
    topic = failed()
    topic["last_error_class"] = cls
    assert plan(topic) == {}


@pytest.mark.parametrize("field", ["paused", "once_done"])
def test_pause_and_finished_once_remove_retries(field):
    topic = failed()
    topic[field] = True
    assert plan(topic) == {}


@pytest.mark.parametrize("reason", ["mirrors.blocked", "mirrors.tracker_daily_limit", "mirrors.http_auth"])
def test_explicit_ban_quota_and_auth_are_not_retried(reason):
    assert plan(failed(reason=reason)) == {}


def test_retry_does_not_wait_for_twelve_hour_owner_cadence():
    items = plan(failed("a"), failed("b", "mirrors.rate_limited"))
    schedule = Schedule(T0, interval_sec=43200, site_retries=items)
    due = schedule.site_retry_due(T0)
    assert T0 + 1800 <= due["a"] <= T0 + 1830
    assert due["a"] == due["b"]
    assert schedule.check_due_at(T0, T0) == T0 + 43200


def test_exponential_finite_budget_survives_failed_spawn_and_restart():
    items = plan(failed())
    schedule = Schedule(T0, site_retries=items)
    for count in range(1, 4):
        prior = schedule.site_retry_attempts.get("a", {}).get("started_at", T0)
        target = schedule.site_retry_due(prior)["a"]
        assert target - prior >= 1800 * 2 ** (count - 1)
        schedule.site_retry_attempts["a"] = {"revision": items["a"]["revision"], "count": count, "started_at": target}
        schedule = Schedule(T0, site_retries=items, site_retry_attempts=copy.deepcopy(schedule.site_retry_attempts))
    assert schedule.site_retry_due(T0) == {}


def test_server_pause_cannot_be_shortened():
    state = {"topics": [failed()], "mirrors": {"demo": {"rate_cool": {"https://demo.example": T0 + 86400}}}}
    items = site_retries.policies(state, config())
    assert Schedule(T0, site_retries=items).site_retry_due(T0)["a"] == T0 + 86400


def test_clock_correction_is_pinned_once():
    items = plan(failed())
    schedule = Schedule(T0, site_retries=items)
    first = schedule.site_retry_due(T0 - 10000)["a"]
    assert schedule.site_retry_due(T0 - 9999)["a"] == first


@pytest.mark.parametrize("value", [0, 4, True, "1", None])
def test_damaged_current_budget_fails_closed(value):
    item = plan(failed())["a"]
    assert retry_deadline(item, {"revision": item["revision"], "count": value, "started_at": T0}, anchor=T0) is None


@pytest.mark.parametrize("change", ["pause", "success", "delete", "url", "selection", "timer", "new_observation"])
def test_reserved_batch_is_revalidated_after_the_check_lock(change):
    topic = failed()
    state = {"topics": [topic]}
    item = plan(topic)["a"]
    batch = {
        "site_retry_batch": {
            "a": {"revision": item["revision"], "observed_at": item["observed_at"], "count": 1, "started_at": T0 + 1800}
        }
    }
    assert site_retries.batch_ids(state, config(), batch) == ["a"]
    if change == "pause":
        topic["paused"] = True
    elif change == "success":
        topic["last_error"] = None
    elif change == "delete":
        state["topics"] = []
    elif change == "url":
        topic["url"] = "https://other.example/topic/a"
    elif change == "selection":
        topic["selection"] = {"mode": "files", "files": ["other.mkv"]}
    elif change == "timer":
        topic["check_interval_min"] = 1
    else:
        topic["last_check"] = datetime.fromtimestamp(T0 + 60, UTC).isoformat()
    assert site_retries.batch_ids(state, config(), batch) == []


def supervisor(items):
    spawned = []
    deps = core.Deps(
        spawn=lambda argv, *a: spawned.append(argv) or SimpleNamespace(pid=123, poll=lambda: None),
        stop=lambda child: True,
        healthy=lambda port: True,
        port_open=lambda port: False,
        watchdog_pass=lambda now: None,
        send=lambda text: True,
        load_config=load_config,
        facts=lambda: {"site_retries": items},
        now=lambda: T0,
        monotonic=lambda: 1000,
    )
    return core.Supervisor(deps, python="python"), spawned


def test_dispatch_persists_before_spawn_and_never_moves_global_start():
    items = plan(failed())
    service, spawned = supervisor(items)
    service.schedule.site_retries = items
    at = service.schedule.site_retry_due(T0)["a"]
    service._persisted["check_started_at"] = T0
    service._start_job("site_retry", at, 1000)
    stored = layout.read_json(layout.schedule_path())
    assert stored["check_started_at"] == T0
    assert stored["site_retry_batch"]["a"]["count"] == 1
    assert "--site-retry-only" in spawned[0]


def test_no_launch_without_durable_reservation(monkeypatch):
    items = plan(failed())
    service, spawned = supervisor(items)
    service.schedule.site_retries = items

    def broken(*args):
        raise OSError("synthetic full disk")

    monkeypatch.setattr(layout, "write_json", broken)
    service._start_job("site_retry", T0 + 2000, 1000)
    assert spawned == []


def test_same_site_errors_have_one_short_message_and_keep_evidence():
    a, b = failed("a"), failed("b", "mirrors.rate_limited")
    for language in ("ru", "en"):
        assert errors.summary_of(
            errors.as_value({"code": a["last_error_code"], "params": a["last_error_params"]}), language
        ) == errors.summary_of(
            errors.as_value({"code": b["last_error_code"], "params": b["last_error_params"]}), language
        )
    items = [
        PendingNotification(
            "error",
            "op",
            topic,
            "demo",
            errors.as_value({"code": topic["last_error_code"], "params": topic["last_error_params"]}),
        )
        for topic in (a, b)
    ]
    messages = compose(items)
    assert len(messages) == 1
    assert messages[0][0].count("https://") == 1
    assert "last_error_params" in a


def test_switching_cloudflare_to_rate_limit_does_not_renotify():
    topic = failed()
    topic.update(error_notified=True, error_streak=3)
    batch = NotificationBatch()
    notices = RunNotifications(True, batch, {"a": "cloudflare"}, {}, "default")
    notices.queue(
        topic,
        kind="error",
        operation_id="op",
        error=errors.TowError("mirrors.rate_limited", cls="tracker"),
        error_cls="tracker",
    )
    assert list(batch) == []


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (TimeoutError("synthetic-secret"), ("timeout", "service")),
        (ValueError("synthetic-secret"), ("invalid_response", "validation")),
        (AccessFailure("missing_cookie", "validation", "synthetic-secret"), ("missing_cookie", "validation")),
        (ProviderUnavailableError("busy", phase="startup"), ("warming", "startup")),
    ],
)
def test_failure_details_never_expose_exception_text(error, expected):
    assert chain.failure_details(error) == expected


def test_event_log_keeps_safe_reason_phase_and_duration(monkeypatch):
    events = []
    monkeypatch.setattr(chain, "log_event", lambda kind, **fields: events.append(fields))
    chain.record_event(
        "protection_failed", "demo", "local", "failed", reason="missing_cookie", phase="validation", elapsed_ms=13300
    )
    assert events[0]["reason"] == "missing_cookie"
    assert events[0]["phase"] == "validation"
    assert events[0]["elapsed_ms"] == 13300
    chain.record_event(
        "protection_failed", "demo", "local", "failed", reason="synthetic-secret", phase="synthetic-secret"
    )
    assert "reason" not in events[1]
    assert "phase" not in events[1]


def test_log_explains_failure_in_both_languages():
    from tow.log import format_event

    event = {"kind": "protection_failed", "phase": "validation", "reason": "missing_cookie", "elapsed_ms": 13300}
    i18n.use("ru")
    assert "ключ доступа" in format_event(event)["detail"]
    i18n.use("en")
    assert "access cookie" in format_event(event)["detail"]


def test_one_click_site_incident_opens_the_real_check_control(monkeypatch):
    from fastapi.testclient import TestClient

    from tow.config import save_config
    from tow.store import load_state, save_secrets, save_state
    from tow.web import app, services, views

    cfg = load_config()
    cfg["trackers"] = {
        "demo": {
            "title": "Site A",
            "fetch_hosts": ["https://demo.example"],
            "url_regex": r"^https://demo\.example/topic/([a-z]+)$",
            "download_path": "/download/{id}",
        }
    }
    save_config(cfg)
    save_secrets({"init_done": True})
    topics = [failed("a"), failed("b", "mirrors.rate_limited")]
    save_state({"topics": topics, "mirrors": {}})
    monkeypatch.setattr(
        services, "site_retry_status", lambda *args: {"a": datetime.fromtimestamp(T0 + 1800, UTC).isoformat()}
    )
    client = TestClient(app)
    entry = views.attention_view(load_state(), cfg)["entries"][0]
    assert len(views.attention_view(load_state(), cfg)["entries"]) == 1
    assert len(entry["subjects"]) == 2
    page = client.get(entry["href"])
    assert page.status_code == 200
    assert 'data-fix="check_topic:a"' in page.text
    assert "autofocus" in page.text
    assert "/topics/a/check" in page.text
    assert "Автоматический повтор:" in page.text
    assert "Проверить доступ сейчас" in page.text
    assert "Cloudflare" in page.text


def test_wrapped_typed_errors_keep_their_class():
    from tow.log import error_class

    for cls in ("cloudflare", "tracker", "quota", "tracker_auth", "qbit"):
        value = errors.as_value(errors.TowError("mirrors.all_failed", cls=cls).record())
        assert error_class(value) == cls


def test_new_server_pause_invalidates_reserved_batch():
    topic = failed()
    state = {"topics": [topic]}
    item = plan(topic)["a"]
    batch = {
        "site_retry_batch": {
            "a": {"revision": item["revision"], "observed_at": item["observed_at"], "count": 1, "started_at": T0 + 1800}
        }
    }
    state["mirrors"] = {"demo": {"rate_cool": {"https://demo.example": T0 + 86400}}}
    assert site_retries.batch_ids(state, config(), batch) == []


def test_dead_clock_anchors_do_not_accumulate():
    schedule = Schedule(T0, site_retries=plan(failed()))
    schedule.site_retry_due(T0 - 3600)
    schedule.site_retries = {}
    schedule.site_retry_due(T0 - 3600)
    assert not any(key.startswith("site_retry:") for key in schedule._corrected)


def test_retry_floor_reads_clamped_deadlines_of_hosts_written_with_a_slash():
    # The configured "https://demo.example/" missed the saved "https://demo.example" pause, and a
    # deadline a clock jump left ten days ahead held the retry for ten days.
    cfg = {"trackers": {"demo": {"fetch_hosts": ["https://demo.example/"]}}, "protection": {"min_interval_sec": 1800}}
    state = {"topics": [failed()], "mirrors": {"demo": {"cool": {"https://demo.example": T0 + 600}}}}
    assert site_retries.policies(state, cfg, now=T0)["a"]["floor"] == T0 + 600
    state["mirrors"]["demo"] = {"rate_cool": {"https://demo.example": T0 + 10 * 86400}}
    assert site_retries.policies(state, cfg, now=T0)["a"]["floor"] == T0 + 86400
