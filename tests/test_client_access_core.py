"""Finite observations outlive task cancellation without reviving task permissions."""

from __future__ import annotations

from copy import deepcopy
from datetime import UTC, datetime

import pytest
from test_client_verification import _run
from test_client_verification import world as verifying_world
from test_client_wait_floors import pin
from test_file_repair import world as repair_world

from tow import client_access, file_repair
from tow.check import client_waits, reconcile
from tow.check import run as check_run
from tow.errors import TowError
from tow.store import load_state, save_state

CFG = {"clients": {"main": {"kind": "qbittorrent", "default": True}}}


def secrets(password="old", *, host="http://EXAMPLE.test", port=None):
    block = {"host": host, "password": password}
    if port is not None:
        block["port"] = port
    return {"clients": {"main": block}}


def test_connection_identity_normalizes_defaults_and_keeps_passwords_out_of_metadata():
    old = client_access.context(CFG, secrets())
    cosmetic = client_access.context(CFG, secrets(host="http://example.test", port=8080))
    new_password = client_access.context(CFG, secrets("new"))
    assert old == cosmetic
    assert old["endpoint"] == new_password["endpoint"]
    assert old["auth"] != new_password["auth"]
    assert "password" not in repr(old)


def test_acknowledging_no_matching_auth_observation_is_an_exact_noop():
    ctx = client_access.context(CFG, secrets())
    changed = client_access.context(CFG, secrets("corrected"))
    state = {"topics": []}
    before = deepcopy(state)
    assert client_access.acknowledge(state, ctx, "")
    client_access.credentials_changed(state, ctx, changed)
    assert state == before
    client_access.observe(state, changed, TowError("client.managed.http_error", code=403), now=1000)
    before = deepcopy(state)
    assert client_access.acknowledge(state, ctx, "")
    assert state == before  # a different credential context is not this acknowledgement's to clear


def test_suppressed_or_shorter_responses_never_renew_a_real_floor():
    ctx = client_access.context(CFG, secrets())
    state = {}
    identity = client_access.observe(
        state, ctx, TowError("client.managed.http_error", code=429, retry_after=3600), now=1000
    )
    before = deepcopy(state)
    assert (
        client_access.observe(state, ctx, client_access.DeferredClientError(429, observation_id=identity), now=1300)
        == identity
    )
    assert (
        client_access.observe(state, ctx, TowError("client.managed.http_error", code=429, retry_after=30), now=1300)
        == identity
    )
    assert state == before
    assert not client_access.server_wait(
        state, client_access.context(CFG, secrets(host="http://different.test")), now=1300
    )


def test_expired_or_current_auth_refusal_never_holds_the_whole_connection():
    ctx = client_access.context(CFG, secrets())
    state = {}
    client_access.observe(state, ctx, TowError("client.managed.http_error", code=403), now=1000)
    assert not client_waits.deferred_clients(state, CFG, secrets=secrets(), now=1300)
    assert not client_waits.deferred_clients(state, CFG, secrets=secrets(), now=1000 + 43200)


def test_ordinary_no_receipt_response_survives_topic_deletion_and_has_no_early_ping(tmp_path, monkeypatch):
    client, _tracker, _sent = verifying_world.__wrapped__(monkeypatch, tmp_path)
    _run()
    client.end_check(found=0.3, dropped_start=False)
    _run(space_only=True)
    clock = pin(monkeypatch)
    observed = []

    def refused():
        observed.append("ping")
        raise TowError("client.managed.http_error", code=429, retry_after=3600)

    monkeypatch.setattr(client, "ping", refused)
    _run(progress_only=True)
    state = load_state()
    assert not state["topics"][0].get("verification_pending")
    cached = deepcopy(state["client_access"])
    topic = state["topics"].pop()
    save_state(state)
    topic["id"] = "replacement-watch"
    state["topics"] = [topic]
    save_state(state)
    clock["now"] += 300
    _run(progress_only=True)
    assert observed == ["ping"]
    assert load_state()["client_access"] == cached


def test_repair_cancellation_preserves_only_server_pause_not_command_authority(tmp_path, monkeypatch):
    client, clock = repair_world(tmp_path, monkeypatch)
    monkeypatch.setattr(client, "ping", lambda: "ok", raising=False)
    monkeypatch.setattr(check_run, "load_config", file_repair.load_config)
    for module in (check_run, reconcile, client_waits):
        monkeypatch.setattr(module, "machine_now", lambda: datetime.fromtimestamp(clock["now"], UTC))
    file_repair.request("synthetic")
    observed = []

    def refused(h):
        observed.append(h)
        raise TowError("client.managed.http_error", code=429, retry_after=3600)

    monkeypatch.setattr(client, "inspect_torrent", refused)
    check_run.run_check(apply=True, notify=False, progress_only=True)
    cached = deepcopy(load_state()["client_access"])
    file_repair.cancel("synthetic")
    clock["now"] += 300
    check_run.run_check(apply=True, notify=False, progress_only=True)
    assert observed == ["a" * 40]
    assert load_state()["client_access"] == cached
    assert not load_state()["topics"][0].get("repair_pending")
    assert client.calls == []


@pytest.mark.parametrize("goal", ["running", "complete", "stopped", "unapplied"])
def test_credential_repair_observes_goal_without_any_native_mutation(tmp_path, monkeypatch, goal):
    client, _tracker, _sent = verifying_world.__wrapped__(monkeypatch, tmp_path)
    if goal == "unapplied":
        client.initial_check = True
    _run()
    if goal != "unapplied":
        client.end_check(found=1.0 if goal == "complete" else 0.3, dropped_start=goal in {"complete", "stopped"})
    current_secrets = {"qbittorrent": {"password": "old"}}
    monkeypatch.setattr(check_run, "read_secrets", lambda: (deepcopy(current_secrets), None))
    inspect = client.inspect_torrent

    def refused(h):
        raise TowError("client.managed.http_error", code=403)

    monkeypatch.setattr(client, "inspect_torrent", refused)
    _run(space_only=True)
    before = deepcopy(load_state()["topics"][0]["verification_pending"])
    current_secrets["qbittorrent"]["password"] = "corrected"
    monkeypatch.setattr(client, "inspect_torrent", inspect)
    client.calls.clear()
    row = _run(space_only=True)
    topic = load_state()["topics"][0]
    assert client.calls == []  # no start, selection, stop, relocation, or label update
    if goal in {"running", "complete"}:
        assert row["ok"]
        assert not topic.get("verification_pending")
        again = _run(progress_only=True)
        assert again["ok"]
        assert client.calls == []
    else:
        waiting = topic["verification_pending"]
        assert waiting["repair_only"] is True
        assert waiting["since"] == before["since"]
        assert waiting.get("resume_attempted") == before.get("resume_attempted")
        assert waiting["blocked"] != "client.managed.http_error"


def test_old_auth_failure_cannot_replace_new_auth_but_same_endpoint_server_pause_still_applies(tmp_path, monkeypatch):
    current = secrets("new")
    monkeypatch.setattr(check_run, "load_config", lambda: CFG)
    monkeypatch.setattr(check_run, "read_secrets", lambda: (current, None))
    state = {}
    client_access.observe(
        state, client_access.context(CFG, current), TowError("client.managed.http_error", code=403), now=1000
    )
    save_state(state)
    auth = deepcopy(state["client_access"]["main"]["auth"])
    assert not client_waits.record_context(
        state, CFG, secrets("old"), "main", TowError("client.managed.http_error", code=403), now=1300, apply=True
    )
    assert load_state()["client_access"]["main"]["auth"] == auth
    assert client_waits.record_context(
        state,
        CFG,
        secrets("old"),
        "main",
        TowError("client.managed.http_error", code=429, retry_after=3600),
        now=1300,
        apply=True,
    )
    assert load_state()["client_access"]["main"]["auth"] == auth
    assert client_access.server_wait(load_state(), client_access.context(CFG, current), now=1400)


def test_actual_password_only_settings_save_allows_automatic_readonly_goal_confirmation(tmp_path, monkeypatch):
    from fastapi.testclient import TestClient

    from tow.config import save_config
    from tow.store import save_secrets
    from tow.web import app

    client, _tracker, _sent = verifying_world.__wrapped__(monkeypatch, tmp_path)
    cfg = check_run.load_config()
    cfg["client"]["kind"] = "qbittorrent"
    save_config(cfg)
    save_secrets({"qbittorrent": {"host": "127.0.0.1", "port": 8080, "username": "owner", "password": "old"}})
    _run()
    client.end_check(found=0.3, dropped_start=False)
    inspect = client.inspect_torrent
    monkeypatch.setattr(
        client, "inspect_torrent", lambda h: (_ for _ in ()).throw(TowError("client.managed.http_error", code=403))
    )
    _run(space_only=True)
    before = deepcopy(load_state()["topics"][0]["verification_pending"])
    response = TestClient(app, headers={"Origin": "http://127.0.0.1"}).post(
        "/settings/client",
        data={
            "client_id": "main",
            "kind": "qbittorrent",
            "host": "127.0.0.1",
            "port": "8080",
            "username": "owner",
            "password": "corrected",
        },
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert load_state()["topics"][0]["verification_pending"] == before
    monkeypatch.setattr(client, "inspect_torrent", inspect)
    client.calls.clear()
    assert _run(space_only=True)["ok"]
    assert client.calls == []
    assert not load_state()["topics"][0].get("verification_pending")


def test_ordinary_client_init_auth_refusal_clears_only_after_exact_owned_readback(tmp_path, monkeypatch):
    client, _tracker, _sent = verifying_world.__wrapped__(monkeypatch, tmp_path)
    _run()
    client.end_check(found=0.3, dropped_start=False)
    _run(space_only=True)
    state = load_state()
    state["topics"][0].update(tracking_mode="watch", once_done=False)
    save_state(state)
    ping = client.ping
    monkeypatch.setattr(client, "ping", lambda: (_ for _ in ()).throw(TowError("client.managed.http_error", code=403)))
    _run()
    assert load_state()["topics"][0]["last_error_code"] == "check.client_unreachable"
    monkeypatch.setattr(client, "ping", ping)
    client.calls.clear()
    _run(progress_only=True)
    assert not load_state()["topics"][0].get("last_error")
    assert client.calls == []


@pytest.mark.parametrize("goal", ["active", "complete", "stopped"])
def test_repair_auth_recovery_observes_only_and_never_rearms_the_command(tmp_path, monkeypatch, goal):
    client, clock = repair_world(tmp_path, monkeypatch)
    current = {"clients": {"main": {"password": "old"}}}
    monkeypatch.setattr(check_run, "load_config", file_repair.load_config)
    monkeypatch.setattr(check_run, "read_secrets", lambda: (deepcopy(current), None))
    monkeypatch.setattr(client, "ping", lambda: "ok", raising=False)
    for module in (check_run, reconcile, client_waits):
        monkeypatch.setattr(module, "machine_now", lambda: datetime.fromtimestamp(clock["now"], UTC))
    file_repair.request("synthetic")
    inspect = client.inspect_torrent
    observed = []

    def refused(h):
        observed.append(h)
        raise TowError("client.managed.http_error", code=403)

    monkeypatch.setattr(client, "inspect_torrent", refused)
    check_run.run_check(apply=True, notify=False, progress_only=True)
    original = deepcopy(load_state()["topics"][0]["repair_pending"])
    clock["now"] += 300
    check_run.run_check(apply=True, notify=False, progress_only=True)
    assert observed == ["a" * 40]
    current["clients"]["main"]["password"] = "corrected"
    monkeypatch.setattr(client, "inspect_torrent", inspect)
    client.info["state"] = "downloading" if goal == "active" else "stoppedUP"
    client.info["progress"] = 0.3 if goal != "complete" else 1.0
    client.info["files"][0]["progress"] = 0.3 if goal != "complete" else 1.0
    if goal == "complete":
        (tmp_path / "S01E01.mkv").write_bytes(b"0123456789")
    check_run.run_check(apply=True, notify=False, progress_only=True)
    topic = load_state()["topics"][0]
    assert client.calls == []
    if goal == "complete":
        assert not topic.get("repair_pending")
        assert not topic.get("files_missing")
    else:
        pending = topic["repair_pending"]
        assert pending["deadline"] == original["deadline"]
        assert not pending.get("resume_dispatched")
        assert pending["phase"] == ("downloading" if goal == "active" else "blocked")
        if goal == "stopped":
            assert pending["blocked"] == "repair.client_stopped"


def test_empty_or_damaged_server_cache_does_not_require_a_master_key(monkeypatch):
    monkeypatch.setattr(
        client_access, "derive_local_secret", lambda purpose: pytest.fail("server floor needs no auth key")
    )
    assert not client_waits.deferred_clients({}, CFG, secrets=secrets(), now=1000)
    ctx = client_access.endpoint(CFG, secrets())
    state = {
        "client_access": {
            "main": {"floor": {"endpoint": ctx["endpoint"], "code": 429, "observed_at": 1000, "not_before": 4600}}
        }
    }
    assert not client_waits.deferred_clients(state, CFG, secrets=secrets(), now=1300)
    state["client_access"]["main"]["floor"]["id"] = "1" * 32
    assert client_waits.deferred_clients(state, CFG, secrets=secrets(), now=1300)


@pytest.mark.parametrize("enabled", [False, True])
@pytest.mark.parametrize("bad", [{"port": "broken"}, {"host": "http://[broken"}])
def test_invalid_unused_client_cannot_prevent_a_healthy_default_ping(tmp_path, monkeypatch, enabled, bad):
    client, _tracker, _sent = verifying_world.__wrapped__(monkeypatch, tmp_path)
    cfg = check_run.load_config()
    cfg["clients"] = {
        "main": {"kind": "qbittorrent", "default": True},
        "broken": {"kind": "qbittorrent", "enabled": enabled},
    }
    cfg.pop("client", None)
    current = {"clients": {"main": {"host": "127.0.0.1"}, "broken": {"host": "127.0.0.1", **bad}}}
    monkeypatch.setattr(check_run, "read_secrets", lambda: (current, None))
    state = load_state()
    state["client_access"] = {"unused": {"floor": {"endpoint": "different"}}}
    save_state(state)
    seen = []
    monkeypatch.setattr(client, "ping", lambda: seen.append("ping") or "ok")
    _run(progress_only=True)
    assert seen == ["ping"]


def test_one_real_auth_failure_is_not_renewed_for_two_receipts_or_a_new_same_credential_refusal(tmp_path, monkeypatch):
    client, _tracker, _sent = verifying_world.__wrapped__(monkeypatch, tmp_path)
    _run()
    client.end_check(found=0.3, dropped_start=False)
    clock = pin(monkeypatch)
    state = load_state()
    peer = deepcopy(state["topics"][0])
    peer["id"] = "peer"
    state["topics"].append(peer)
    save_state(state)
    pings = []

    def refused():
        pings.append("ping")
        raise TowError("client.managed.http_error", code=403)

    monkeypatch.setattr(client, "ping", refused)
    observe = client_access.observe
    observations = []

    def counted(*args, **kwargs):
        identity = observe(*args, **kwargs)
        observations.append(identity)
        return identity

    monkeypatch.setattr(client_access, "observe", counted)
    check_run.run_check(apply=True, notify=False, space_only=True)
    current = load_state()
    identity = current["client_access"]["main"]["auth"]["id"]
    assert observations == [identity]
    assert pings == ["ping"]
    assert {topic["verification_pending"]["http_refusal_id"] for topic in current["topics"]} == {identity}
    cfg = check_run.load_config()
    ctx = client_access.context(cfg, check_run.read_secrets()[0], "main")
    clock["now"] += 1
    replacement = observe(current, ctx, TowError("client.managed.http_error", code=403), now=clock["now"])
    assert replacement != identity
    save_state(current)
    check_run.run_check(apply=True, notify=False, space_only=True)
    assert observations == [identity]
    assert pings == ["ping"]
    assert load_state()["client_access"]["main"]["auth"]["id"] == replacement
    assert not client_access.acknowledge(current, ctx, identity)
