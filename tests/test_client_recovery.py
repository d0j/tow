"""Client transport recovery keeps its authority separate from auth and native mutations."""

from __future__ import annotations

import socket
import ssl
from copy import deepcopy
from datetime import UTC, datetime
from types import SimpleNamespace

import httpx
import pytest
import requests
from qbittorrentapi import APIConnectionError
from test_check_pipeline import OLD, Client, Tracker, _topic, _wire

from tow import check, site_retries
from tow.check import client_ops
from tow.check import reconcile as check_reconcile
from tow.errors import Msg, TowError
from tow.store import load_state, save_download_history, save_state
from tow.supervisor.schedule import Schedule

NOW = datetime(2026, 10, 14, 9, tzinfo=UTC).timestamp()


@pytest.mark.parametrize(
    ("cause", "code"),
    [
        (ConnectionRefusedError(), "doctor.reason.refused"),
        (ConnectionResetError(), "doctor.reason.reset"),
        (TimeoutError(), "doctor.reason.timeout"),
        (socket.gaierror(), "doctor.reason.no_address"),
        (httpx.ConnectError("opaque"), "doctor.reason.network"),
        (requests.exceptions.ReadTimeout("opaque"), "doctor.reason.timeout"),
    ],
)
def test_typed_transport_causes_survive_wrapping_and_authorize_only_bounded_recovery(cause, code):
    wrapped = APIConnectionError("opaque wrapper")
    wrapped.__context__ = cause
    run = SimpleNamespace(client_errors={"main": wrapped})
    error = client_ops.client_unreachable(run, "main").record()
    assert error["params"]["reason"]["$msg"]["code"] == code
    topic, cfg = failed(error)
    item = site_retries.policies({"topics": [topic]}, cfg, now=NOW)["t1"]
    assert item["reason"] == "client_recovery"
    assert item["delay"] == 300
    schedule = Schedule(NOW, interval_sec=43200, site_retries={"t1": item})
    assert NOW + 300 <= schedule.site_retry_due(NOW)["t1"] <= NOW + 330
    for count in (1, 2, 3):
        at = schedule.site_retry_due(NOW)["t1"]
        schedule.site_retry_attempts["t1"] = {"revision": item["revision"], "count": count, "started_at": at}
    assert schedule.site_retry_due(NOW) == {}


@pytest.mark.parametrize(
    "cause",
    [
        RuntimeError("timeout was typed in a password; not a transport proof"),
        PermissionError("not a network failure"),
        requests.exceptions.SSLError("certificate refusal"),
        requests.exceptions.ProxyError("configured proxy refusal"),
        httpx.ProxyError("proxy refusal"),
        ssl.SSLCertVerificationError("certificate refusal"),
        requests.exceptions.HTTPError("HTTP 401"),
        TowError("client.transmission.bad_login"),
        TowError("client.deluge.not_web"),
    ],
)
def test_auth_configuration_certificate_and_untyped_text_never_become_retry_permission(cause):
    wrapped = APIConnectionError("opaque wrapper")
    wrapped.__context__ = cause
    assert client_ops._transport_reason(wrapped) is None
    error = client_ops.client_unreachable(SimpleNamespace(client_errors={"main": wrapped}), "main").record()
    topic, cfg = failed(error)
    assert site_retries.policies({"topics": [topic]}, cfg, now=NOW) == {}


@pytest.mark.parametrize("status", [408, 425, 429, 500, 502, 503, 504])
def test_temporary_numeric_client_http_failure_keeps_its_server_backoff_and_finite_budget(status):
    error = client_ops.client_unreachable(
        SimpleNamespace(client_errors={"main": TowError("client.managed.http_error", code=status, retry_after=7200)}),
        "main",
    ).record()
    topic, cfg = failed(error)
    item = site_retries.policies({"topics": [topic]}, cfg, now=NOW)["t1"]
    assert item["reason"] == "client_recovery"
    schedule = Schedule(NOW, interval_sec=43200, site_retries={"t1": item})
    assert schedule.site_retry_due(NOW)["t1"] == NOW + 7200


@pytest.mark.parametrize("status", [True, "503", 401, 403, 404, 421, 501, 505, 508])
def test_client_auth_protocol_and_malformed_statuses_never_grant_a_retry(status):
    topic, cfg = failed(
        TowError("check.client_unreachable", reason=TowError("client.managed.http_error", code=status)).record()
    )
    assert site_retries.policies({"topics": [topic]}, cfg, now=NOW) == {}


def failed(record=None):
    record = record or TowError("check.client_unreachable", reason=Msg("check.client_empty")).record()
    topic = _topic(
        hash=OLD,
        client_id="main",
        last_error="synthetic client failure",
        last_error_code=record["code"],
        last_error_params=record["params"],
        last_error_class="qbit",
        last_check=datetime.fromtimestamp(NOW, UTC).isoformat(),
    )
    cfg = {
        "clients": {"main": {"kind": "qbittorrent", "default": True}},
        "trackers": {
            "fake": {
                "url_regex": r"^https://tracker\.example/(\d+)$",
                "fetch_hosts": ["https://tracker.example"],
            }
        },
    }
    return topic, cfg


@pytest.mark.parametrize("change", ["paused", "dirty", "site_paused", "client_off", "verification", "space"])
def test_client_retry_cannot_cross_owner_or_existing_fsm_boundaries(change):
    topic, cfg = failed()
    state = {"topics": [topic], "mirrors": {}}
    if change == "paused":
        topic["paused"] = True
    elif change == "dirty":
        topic["selection_dirty"] = True
    elif change == "site_paused":
        state["mirrors"] = {"fake": {"frozen": True}}
    elif change == "client_off":
        cfg["clients"]["main"]["enabled"] = False
    else:
        topic["verification_pending" if change == "verification" else "waiting_space"] = {"hash": OLD}
    assert site_retries.policies(state, cfg, now=NOW) == {}


def connected_world(monkeypatch):
    topic, _cfg = failed()
    topic.update(
        selection={"mode": "all"},
        selection_hash=OLD,
        selection_verified=True,
        last_ok=False,
        site_ok_at="2026-10-14T08:59:00+00:00",
    )
    client = Client()
    client.put(OLD, topic["save_path"], tags=["tow"])
    _wire(monkeypatch, {"main": client}, Tracker())
    save_state({"topics": [topic]})
    save_download_history({"schema_version": 1, "topics": {}})
    return client, deepcopy(topic)


def test_fresh_owned_existing_task_clears_only_the_stale_transport_error_without_inventing_a_site_check(
    monkeypatch,
):
    client, before = connected_world(monkeypatch)
    result = check.run_check(apply=True, notify=False, how="progress", progress_only=True)
    after = load_state()["topics"][0]
    assert not after.get("last_error")
    assert not after.get("last_error_code")
    for key in ("last_ok", "last_check", "site_ok_at", "hash", "selection_hash", "save_path"):
        assert after[key] == before[key]
    assert result["results"][0]["status"] == "skipped"
    assert client.adds == []


def test_a_recovered_owned_native_task_clears_a_typed_503_without_fetching_or_adding(monkeypatch):
    client, before = connected_world(monkeypatch)
    state = load_state()
    error = TowError("check.client_unreachable", reason=TowError("client.managed.http_error", code=503)).record()
    state["topics"][0].update(last_error_code=error["code"], last_error_params=error["params"])
    save_state(state)
    check.run_check(apply=True, notify=False, how="progress", progress_only=True)
    after = load_state()["topics"][0]
    assert not after.get("last_error")
    assert after["last_check"] == before["last_check"]
    assert client.adds == []


@pytest.mark.parametrize(
    "change", ["owner", "path", "native_error", "native_unknown", "wanted", "unverified", "pending", "auth"]
)
def test_a_ping_or_partial_or_changed_native_proof_cannot_clear_a_real_or_unresolved_error(monkeypatch, change):
    client, _before = connected_world(monkeypatch)
    state = load_state()
    if change == "owner":
        client.torrents[OLD]["tags"] = []
    elif change == "path":
        client.torrents[OLD]["save_path"] = "/other"
    elif change == "native_error":
        client.torrents[OLD]["state"] = "error"
    elif change == "native_unknown":
        client.torrents[OLD]["state"] = "unknown"
    elif change == "wanted":
        client.torrents[OLD]["files"][0]["priority"] = 0
    elif change == "unverified":
        state["topics"][0]["selection_verified"] = False
    elif change == "pending":
        client.torrents[OLD]["tags"] = ["tow", "tow-pending"]
    else:
        state["topics"][0].update(last_error_code="client.transmission.bad_login", last_error_params={})
    save_state(state)
    check.run_check(apply=True, notify=False, how="progress", progress_only=True)
    after = load_state()["topics"][0]
    assert after["last_error"] == "synthetic client failure"
    assert client.adds == []


def test_a_failed_fresh_readback_does_not_replace_an_outage_with_false_success(monkeypatch):
    client, _before = connected_world(monkeypatch)

    def unavailable(_hash):
        raise ConnectionRefusedError("synthetic connection went away again")

    monkeypatch.setattr(client, "inspect_torrent", unavailable)
    # Reconcile's primary observation is independent; the secondary confirmation fails.
    assert check_reconcile._client_error_resolved(load_state()["topics"][0], client) is False
    assert load_state()["topics"][0]["last_error"]
