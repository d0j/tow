"""Only a current owner action belongs in attention; automatic work keeps its evidence."""

from __future__ import annotations

from copy import deepcopy
from dataclasses import replace
from datetime import UTC, datetime

import pytest
from bs4 import BeautifulSoup
from fastapi.testclient import TestClient

from tow import site_retries
from tow.errors import TowError
from tow.issues import Issue, Kind, Message, Snapshot, Target, attention, collect, fingerprint
from tow.issues.recovery import site_retrying
from tow.store import load_state, save_state

NOW = 1_800_000_000.0
CFG = {
    "interval_sec": 3600,
    "clients": {"main": {"kind": "qbittorrent", "default": True}},
    "trackers": {
        "demo": {
            "title": "Demo",
            "url_regex": r"^https://demo\.example/topic/(.+)",
            "fetch_hosts": ["https://demo.example"],
        }
    },
}


def failed(identity="a"):
    record = TowError("mirrors.all_failed", tracker="demo", error=TowError("mirrors.cloudflare")).record()
    return {
        "id": identity,
        "title": "Synthetic Show " + identity,
        "url": f"https://demo.example/topic/{identity}",
        "hash": "a" * 40,
        "client_id": "main",
        "last_error": "saved failure",
        "last_error_code": record["code"],
        "last_error_params": record["params"],
        "last_error_class": "tracker",
        "last_check": datetime.fromtimestamp(NOW, UTC).isoformat(),
    }


def snapshot(*topics, health=None):
    return Snapshot(state={"topics": list(topics), "health": health or {}}, config=CFG, now=NOW)


def continuation(sample, identity="a", **changes):
    policy = site_retries.policies(sample.state, sample.config, now=sample.now)[identity]
    return {
        "phase": "scheduled",
        "revision": policy["revision"],
        "observed_at": policy["observed_at"],
        "next_at": NOW + 1800,
        "deadline": NOW + 2400,
        "attempt": 1,
        "reason": "site_retry",
        **changes,
    }


def topic_issues(sample):
    return [issue for issue in collect(sample) if issue.kind == "topic_error"]


@pytest.mark.parametrize("phase", ["scheduled", "queued", "running"])
def test_actual_continuation_defers_attention_without_erasing_the_failure(phase):
    sample = snapshot(failed())
    before = deepcopy(sample.state)
    sample = replace(sample, site_retries={"a": continuation(sample, phase=phase)})
    [issue] = topic_issues(sample)
    assert not issue.requires_attention
    assert not attention(collect(sample))
    assert sample.state == before
    assert sample.state["topics"][0]["last_error"] == "saved failure"


@pytest.mark.parametrize(
    "changes",
    [
        {"phase": "exhausted"},
        {"phase": "stopped"},
        {"phase": "unknown"},
        {"revision": "b" * 64},
        {"observed_at": NOW - 1},
        {"deadline": NOW},
        {"deadline": float("nan")},
        {"deadline": True},
        {"next_at": NOW + 3000},
        {"next_at": float("inf")},
    ],
)
def test_stale_expired_or_damaged_plan_never_hides_a_problem(changes):
    sample = snapshot(failed())
    sample = replace(sample, site_retries={"a": continuation(sample, **changes)})
    [issue] = topic_issues(sample)
    assert issue.requires_attention
    assert attention(collect(sample))


@pytest.mark.parametrize("change", [{"paused": True}, {"once_done": True}, {"selection_dirty": True}])
def test_owner_context_change_does_not_transfer_a_retry_plan(change):
    sample = snapshot(failed())
    plan = continuation(sample)
    updated = deepcopy(sample.state)
    updated["topics"][0].update(change)
    sample = replace(sample, state=updated, site_retries={"a": plan})
    assert not site_retrying(sample)


@pytest.mark.parametrize("bad", [None, [], {}, {"phase": "running"}])
def test_missing_plan_is_not_guessed(bad):
    sample = replace(snapshot(failed()), site_retries={"a": bad})
    assert topic_issues(sample)[0].requires_attention


def test_fresh_failure_needs_its_own_observation_bound_plan():
    sample = snapshot(failed())
    old = continuation(sample)
    updated = deepcopy(sample.state)
    updated["topics"][0]["last_check"] = datetime.fromtimestamp(NOW + 1, UTC).isoformat()
    current = replace(sample, state=updated, site_retries={"a": old})
    assert topic_issues(current)[0].requires_attention


def test_one_site_with_a_pending_and_an_owner_action_targets_the_owner_action():
    sample = snapshot(failed("a"), failed("b"))
    sample = replace(sample, site_retries={"a": continuation(sample)})
    [issue] = topic_issues(sample)
    assert issue.requires_attention
    assert issue.count == 2
    assert issue.subjects == ("a", "b")
    assert issue.target.open == "b"
    assert issue.fix.subject == "b"


def test_merge_cannot_hide_the_owner_action_regardless_of_order():
    base = Issue("synthetic", "shared", "warn", Message("common.busy.checking"), Target("home", open="pending"))
    waiting = replace(base, requires_attention=False)
    actionable = replace(base, target=Target("home", open="owner"))
    for rows in ([waiting, actionable], [actionable, waiting]):
        [merged] = collect(snapshot(), only=(Kind("synthetic", 1, lambda _snapshot, rows=rows: rows),))
        assert merged.requires_attention
        assert merged.target.open == "owner"
        assert merged.count == 2
    assert fingerprint([waiting]) != fingerprint([actionable])


def test_an_invalid_actionability_flag_cannot_be_treated_as_false():
    with pytest.raises(ValueError, match="requires_attention"):
        Issue("synthetic", "a", "warn", Message("common.busy.checking"), Target("home"), requires_attention="no")


def test_pending_transport_defers_the_folded_client_problem_but_not_a_manual_failure(monkeypatch):
    topic = failed()
    error = TowError("check.client_unreachable", reason=TowError("check.client_empty"))
    topic.update(last_error_code=error.code, last_error_params=error.params, last_error_class="qbit")
    sample = snapshot(topic, health={"qbit_ok": False})
    policy = {"a": {"revision": "b" * 64, "observed_at": NOW}}
    monkeypatch.setattr(site_retries, "policies", lambda *_args, **_kwargs: policy)
    sample = replace(sample, site_retries={"a": continuation(sample)})
    [issue] = collect(sample)
    assert issue.kind == "client_down"
    assert not issue.requires_attention
    manual = replace(sample, state={**sample.state, "health": {"qbit_ok": False, "ping_failed": {"main": NOW}}})
    assert collect(manual)[0].requires_attention


def test_two_independent_sessions_follow_pending_action_required_and_recovery(monkeypatch):
    from tow.config import load_config, save_config
    from tow.web import app, services

    monkeypatch.setattr("tow.web.views.time.time", lambda: NOW)
    cfg = load_config()
    cfg.update(deepcopy(CFG), language="ru")
    save_config(cfg)
    sample = snapshot(failed(), health={"qbit_ok": True, "auto_at_ts": int(NOW)})
    save_state(dict(sample.state))
    current = {"a": continuation(sample)}
    monkeypatch.setattr(services, "site_recovery_status", lambda *_args: current)
    a, b = TestClient(app), TestClient(app)

    def visible(client):
        page = BeautifulSoup(client.get("/").text, "html.parser")
        return "hidden" not in page.select_one("#current-problems").attrs

    assert [visible(client) for client in (a, b)] == [False, False]
    assert load_state()["topics"][0]["last_error"] == "saved failure"
    panel = BeautifulSoup(a.get("/topics/a/edit").text, "html.parser")
    assert "Повтор запланирован" in panel.get_text()
    assert "Предыдущая проверка:" in panel.get_text()
    assert not panel.select('form[action="/topics/a/check"]')
    current.clear()
    assert [visible(client) for client in (a, b)] == [True, True]
    assert BeautifulSoup(a.get("/topics/a/edit").text, "html.parser").select('form[action="/topics/a/check"]')
    state = load_state()
    for key in ("last_error", "last_error_code", "last_error_params", "last_error_class"):
        state["topics"][0].pop(key, None)
    state["topics"][0]["last_ok"] = True
    save_state(state)
    assert [visible(client) for client in (a, b)] == [False, False]


@pytest.mark.parametrize("waiting", ["space", "confirm"])
def test_a_pending_wait_is_not_a_popup_or_an_actionable_error(tmp_path, monkeypatch, waiting):
    from test_file_repair import CONFIG, world

    from tow import check, file_repair
    from tow.config import load_config, save_config
    from tow.web import app, services

    world(tmp_path, monkeypatch)
    cfg = load_config()
    cfg.update(deepcopy(CONFIG), language="ru")
    save_config(cfg)
    monkeypatch.setattr(
        services,
        "client_continuation_status",
        lambda state, cfg: check.client_continuation_status(
            state,
            cfg,
            report={"next": {"space": datetime.fromtimestamp(1010, UTC).isoformat()}},
            now=1000,
        ),
    )
    file_repair.request("synthetic")
    state = load_state()
    state["topics"][0]["repair_pending"]["retry_error"] = {
        "code": "repair.waiting_space",
        "params": {"bytes": 1_000_000_000},
    }
    if waiting == "confirm":
        state["topics"][0]["repair_pending"].update(
            phase="blocked", blocked="repair.dispatch_unconfirmed", recheck_dispatched=True, check_at=1000
        )
    save_state(state)
    page = BeautifulSoup(TestClient(app).get("/").text, "html.parser")
    assert "hidden" in page.select_one("#current-problems").attrs
    row = page.select_one("#row-synthetic")
    assert ("Ожидается место на диске" if waiting == "space" else "Ожидается подтверждение") in row.get_text()
    assert "1000000000" not in row.get_text()


def test_native_owner_failure_is_explained_in_the_recovery_panel(tmp_path, monkeypatch):
    from test_file_repair import CONFIG, world

    from tow import file_repair
    from tow.config import load_config, save_config
    from tow.web import app

    world(tmp_path, monkeypatch)
    cfg = load_config()
    cfg.update(deepcopy(CONFIG), language="ru")
    save_config(cfg)
    file_repair.request("synthetic")
    state = load_state()
    state["topics"][0]["repair_pending"].update(phase="blocked", blocked="client.managed.not_owned")
    save_state(state)
    result = TestClient(app).get("/topics/synthetic/edit")
    assert result.status_code == 200
    assert "торрент добавлен не через TOW" in result.text


def test_a_native_recovery_can_observe_a_disconnected_client_without_a_topic_error(tmp_path, monkeypatch):
    from test_file_repair import CONFIG, advance, world

    from tow import check, file_repair

    client, clock = world(tmp_path, monkeypatch)
    file_repair.request("synthetic")

    def offline(_hash):
        raise OSError("synthetic temporary outage")

    monkeypatch.setattr(client, "inspect_torrent", offline)
    advance(client)
    state = load_state()
    assert not state["topics"][0].get("last_error")
    assert state["topics"][0]["repair_pending"]["retry_error"]["code"] == "repair.waiting_client"
    state["health"] = {"qbit_ok": False}
    plans = check.client_continuation_status(
        state,
        CONFIG,
        report={"next": {"space": datetime.fromtimestamp(clock["now"] + 300, UTC).isoformat()}},
        now=clock["now"],
    )
    sample = Snapshot(state, CONFIG, clock["now"], client_continuations=plans)

    def client_problem(observation):
        return next(issue for issue in collect(observation) if issue.kind == "client_down")

    assert not client_problem(sample).requires_attention
    assert client_problem(replace(sample, client_continuations={})).requires_attention
    state["health"]["ping_failed"] = {"main": clock["now"]}
    assert client_problem(sample).requires_attention
    state["health"].pop("ping_failed")
    state["topics"].append(
        {
            "id": "other",
            "client_id": "main",
            "last_error": "synthetic forbidden",
            "last_error_code": "client.managed.http_error",
            "last_error_params": {"code": 403},
            "last_error_class": "qbit",
        }
    )
    assert client_problem(sample).requires_attention


def test_restored_permission_is_actionable_even_without_a_saved_error(tmp_path):
    from tow.config import load_config, save_config
    from tow.web import app

    cfg = load_config()
    cfg.update(deepcopy(CFG), language="ru")
    save_config(cfg)
    topic = {
        "id": "a",
        "title": "Synthetic Show",
        "url": "https://demo.example/topic/a",
        "client_id": "main",
        "hash": "a" * 40,
        "save_path": str(tmp_path),
        "verification_pending": {"blocked": "check.restored_confirmation", "repair_only": True},
    }
    state = {"topics": [topic], "health": {"qbit_ok": True}}
    save_state(state)
    [issue] = collect(Snapshot(state=state, config=cfg, now=NOW))
    assert issue.requires_attention
    assert issue.fix.action == "check_topic"
    assert issue.target.open == "a"
    client = TestClient(app)
    page = BeautifulSoup(client.get("/").text, "html.parser")
    assert "hidden" not in page.select_one("#current-problems").attrs
    assert "Нужно подтвердить продолжение" in page.select_one("#row-a").get_text()
    panel = BeautifulSoup(client.get("/topics/a/edit").text, "html.parser")
    button = panel.select_one('form[action="/topics/a/check"] button')
    assert button.get_text(strip=True) == "Продолжить загрузку"
    assert panel.select_one('form[action="/topics/a/check"][data-confirm]')
