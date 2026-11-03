"""Client server refusals cannot be bypassed by verification, space, or reconciliation."""

from __future__ import annotations

from copy import deepcopy
from datetime import UTC, datetime

import pytest
from test_client_verification import H, _run
from test_client_verification import world as verifying_world
from test_file_repair import advance
from test_file_repair import world as repair_world
from test_wait_for_space import GIB, NEW, _check, _space_pass
from test_wait_for_space import world as space_world

from tow import file_repair
from tow.check import client_continuation_status, client_waits, reconcile, space, verification
from tow.check import run as check_run
from tow.errors import TowError
from tow.issues import Snapshot
from tow.issues.kinds import topic_error
from tow.store import load_state, save_state


def pin(monkeypatch):
    clock = {"now": datetime.now(UTC).timestamp()}
    for module in (verification, space, reconcile, check_run, client_waits):
        monkeypatch.setattr(module, "machine_now", lambda: datetime.fromtimestamp(clock["now"], UTC))
    return clock


def prepare(kind, monkeypatch, tmp_path):
    if kind == "verification":
        client = verifying_world.__wrapped__(monkeypatch, tmp_path)[0]
        _run()
        client.end_check(found=0.3, dropped_start=False)
        return client, H, _run, "verification_pending"
    waiting_space = space_world.__wrapped__(monkeypatch, tmp_path)
    _check()
    waiting_space.disk["free"] = 60 * GIB
    return waiting_space.client, NEW, _space_pass, "waiting_space"


@pytest.mark.parametrize("kind", ["verification", "space"])
@pytest.mark.parametrize("status", [425, 429, 503])
def test_native_wait_respects_server_hour_floor_including_reconciliation(monkeypatch, tmp_path, kind, status):
    client, infohash, run, key = prepare(kind, monkeypatch, tmp_path)
    clock = pin(monkeypatch)
    inspect = client.inspect_torrent
    observed = []
    for method in ("ping", "has_any_torrent"):
        function = getattr(client, method, None)
        if function is not None:

            def counted(*args, _function=function, _method=method, **kwargs):
                observed.append(_method)
                return _function(*args, **kwargs)

            monkeypatch.setattr(client, method, counted)

    def refused(h):
        observed.append(h)
        raise TowError("client.managed.http_error", code=status, retry_after=3600)

    monkeypatch.setattr(client, "inspect_torrent", refused)
    run(space_only=True) if kind == "verification" else run()
    receipt = load_state()["topics"][0][key]
    assert receipt["not_before"] == clock["now"] + 3600
    assert observed[-1] == infohash
    first_reads = list(observed)
    state = load_state()
    cfg = check_run.load_config()
    plan = client_continuation_status(
        state,
        cfg,
        report={"next": {"space": datetime.fromtimestamp(clock["now"] + 300, UTC).isoformat()}},
        now=clock["now"],
    )
    issues = topic_error.collect(Snapshot(state, cfg, clock["now"], client_continuations=plan))
    assert issues
    assert not issues[0].requires_attention
    peer = deepcopy(state["topics"][0])
    peer.update(id="another-topic", hash="b" * 40)
    peer.pop("verification_pending", None)
    peer.pop("waiting_space", None)
    state["topics"].append(peer)
    previous_health = {key: state["health"].get(key) for key in ("qbit", "qbit_ok", "clients_ok")}
    save_state(state)
    for _ in range(2):
        clock["now"] += 300
        check_run.run_check(apply=True, notify=False, progress_only=True)
    assert observed == first_reads  # no ping, listing, inspect or command before Retry-After
    assert {key: load_state()["health"].get(key) for key in previous_health} == previous_health
    assert load_state()["topics"][0][key]["since"] == receipt["since"]
    monkeypatch.setattr(client, "inspect_torrent", inspect)
    clock["now"] = receipt["not_before"]
    run(space_only=True) if kind == "verification" else run()
    after = load_state()["topics"][0]
    assert not after.get(key)
    if kind == "space":
        assert client.starts == [infohash]
    else:
        assert [call[0] for call in client.calls].count("start") == 1  # the original add only


@pytest.mark.parametrize("kind", ["verification", "space"])
def test_forbidden_native_client_requires_owner_and_is_not_polled_automatically(monkeypatch, tmp_path, kind):
    client, infohash, run, key = prepare(kind, monkeypatch, tmp_path)
    observed = []

    def refused(h):
        observed.append(h)
        raise TowError("client.managed.http_error", code=403)

    monkeypatch.setattr(client, "inspect_torrent", refused)
    run(space_only=True) if kind == "verification" else run()
    assert load_state()["topics"][0][key]["blocked_params"] == {"code": 403}
    for _ in range(2):
        check_run.run_check(apply=True, notify=False, progress_only=True)
    assert observed == [infohash]


def test_nested_native_refusal_keeps_typed_status_and_retry_after_without_parsing_words():
    error = TowError(
        "check.client_unreachable", reason=TowError("client.managed.http_error", code=429, retry_after=3600)
    )
    assert client_waits.http_status(error) == 429
    assert client_waits.retry_after(error, now=1000) == 3600
    assert client_waits.permanent_http(error) is None
    assert client_waits.permanent_http(TowError("check.client_unreachable", reason="HTTP 403")) is None


@pytest.mark.parametrize("blocked", [True, False])
def test_old_client_refusal_cannot_defer_a_new_resolved_default_connection(blocked):
    topic = {
        "id": "a",
        "hash": "a" * 40,
        "waiting_space": {
            "client": "old",
            "hash": "a" * 40,
            "not_before": 5000,
            "retry_status": 429,
        },
    }
    if blocked:
        topic["waiting_space"].update(blocked="client.managed.http_error", blocked_params={"code": 403})
    cfg = {"clients": {"new": {"kind": "qbittorrent", "default": True}}}
    assert not client_waits.deferred(topic, now=1000, cfg=cfg)
    assert not client_waits.deferred_clients({"topics": [topic]}, cfg, now=1000)


def test_existing_repair_http_floor_also_excludes_ping_and_progress_reads(tmp_path, monkeypatch):
    client, clock = repair_world(tmp_path, monkeypatch)
    file_repair.request("synthetic")
    inspect = client.inspect_torrent

    def refused(h):
        raise TowError("client.managed.http_error", code=429, retry_after=3600)

    monkeypatch.setattr(client, "inspect_torrent", refused)
    advance(client)
    observed = []
    monkeypatch.setattr(client, "ping", lambda: observed.append("ping") or "ok", raising=False)
    monkeypatch.setattr(client, "inspect_torrent", lambda h: observed.append("inspect") or inspect(h))
    monkeypatch.setattr(check_run, "load_config", file_repair.load_config)
    for module in (check_run, reconcile):
        monkeypatch.setattr(module, "machine_now", lambda: datetime.fromtimestamp(clock["now"], UTC))
    clock["now"] += 300
    result = check_run.run_check(apply=True, notify=False, progress_only=True)
    assert result["results"][0]["status"] == "repair_pending"
    assert observed == []
    assert client.calls == []
    assert "qbit_ok" not in load_state().get("health", {})  # no observation is not success
