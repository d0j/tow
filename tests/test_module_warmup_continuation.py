"""A slow owned cold start actually reaches one ordinary site check, with finite saved authority."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from unittest.mock import Mock

import httpx
import pytest
from test_check_pipeline import Client, Tracker, _wire
from test_protection import CF, install

from tow import check, site_retries
from tow.check import rows, run
from tow.config import load_config
from tow.mirrors import pick_and_get
from tow.platform import _local_service_worker as worker
from tow.protection import chain, local_service, providers, store, warmup
from tow.protection.spec import Clearance, validate_settings
from tow.store import load_state, save_download_history, save_state
from tow.supervisor import core, layout

BASE, NONCE = 1_800_000_000.0, "a" * 32


@pytest.fixture
def world(monkeypatch, tmp_path):
    clock, launches, solver_calls, readiness_calls = [0.0], [], [], []
    monkeypatch.setattr(warmup.time, "time", lambda: BASE + clock[0])
    monkeypatch.setattr(warmup.time, "monotonic", lambda: clock[0])
    monkeypatch.setattr(warmup.time, "sleep", lambda delay: clock.__setitem__(0, clock[0] + delay))
    monkeypatch.setattr(rows, "now", lambda: datetime.fromtimestamp(BASE + clock[0], UTC).isoformat())
    protection = {
        "enabled": True,
        "timeout_sec": 30,
        "max_attempts": 1,
        "providers": [{"id": "local", "kind": "flaresolverr", "url": "http://127.0.0.1:8191/v1", "enabled": True}],
    }

    def site(request):
        if "cf_clearance=synthetic" in request.headers.get("cookie", ""):
            return httpx.Response(
                200, content=b"torrent" if request.url.path.startswith("/download") else b"<title>Site</title>"
            )
        return httpx.Response(403, text=CF, headers={"cf-mitigated": "challenge"})

    site_calls, spec = install(monkeypatch, site, protection=protection)
    native = worker.NativeService(tmp_path, {"version": "synthetic", "idle_sec": 300})

    def launch(_deadline):
        launches.append(clock[0])
        native.child = SimpleNamespace(poll=lambda: None)
        native.port, native.start_nonce, native.start_deadline = 9999, NONCE, clock[0] + 120

    monkeypatch.setattr(native, "launch", launch)

    def response():
        if clock[0] < 80:
            raise OSError("synthetic service is not ready yet")
        return SimpleNamespace(status=200, read=lambda size: b'{"version":"synthetic"}')

    connection = Mock()
    connection.getresponse.side_effect = response
    monkeypatch.setattr(worker.http.client, "HTTPConnection", lambda *a, **kw: connection)

    def solve(provider, origin, timeout):
        solver_calls.append(clock[0])
        try:
            native.start(clock[0] + timeout)
        except worker.StartupPending:
            # The real adapter mapping of the broker's trusted no-site-request response.
            providers._service_status(
                httpx.Response(
                    503,
                    headers={
                        "Retry-After": "5",
                        "X-TOW-Module-State": "warming",
                        "X-TOW-Module-Nonce": native.start_nonce,
                        "X-TOW-Module-Remaining": str(int(native.start_deadline - clock[0])),
                    },
                ),
                local=True,
            )
        return Clearance({"cf_clearance": "synthetic"}, "Synthetic/1", BASE + clock[0] + 300, provider["id"])

    monkeypatch.setattr(chain, "public_addresses", lambda *a: ["93.184.216.34"])
    monkeypatch.setattr(chain, "adapters", lambda: {"flaresolverr": solve})
    monkeypatch.setattr(local_service, "ensure", lambda *a: pytest.fail("continuation cold-started a service"))

    def readiness(_provider, nonce, **_kwargs):
        readiness_calls.append((clock[0], len(site_calls), len(solver_calls)))
        return native.readiness(nonce)

    monkeypatch.setattr(local_service, "readiness", readiness)

    class SiteTracker(Tracker):
        name = "demo"

        def fetch_torrent(self, url, secrets, ua, *, ignore_cool=False, persist=True):
            answer, _host = pick_and_get(
                "demo",
                spec["fetch_hosts"],
                "/download/1",
                ok=lambda value: value.content == b"torrent",
                protection=protection,
                persist=persist,
                ignore_cool=ignore_cool,
            )
            return answer.content

    client = Client()
    _wire(monkeypatch, {"main": client}, SiteTracker())
    cfg = run.load_config()
    cfg.update(load_config())
    topic = {"id": "a", "url": "https://site.example/topic/1", "title": "Show", "save_path": str(tmp_path / "media")}
    save_state({"topics": [topic]})
    save_download_history({"schema_version": 1, "topics": {}})
    return SimpleNamespace(
        clock=clock,
        cfg=cfg,
        native=native,
        client=client,
        site_calls=site_calls,
        solver_calls=solver_calls,
        readiness_calls=readiness_calls,
        launches=launches,
    )


def reserve(world):
    def facts():
        return {"site_retries": site_retries.policies(load_state(), world.cfg), "topic_ids": {"a"}}

    deps = core.Deps(
        spawn=lambda *a: SimpleNamespace(pid=123, poll=lambda: 0),
        stop=lambda child: True,
        healthy=lambda port: True,
        port_open=lambda port: False,
        watchdog_pass=lambda now: None,
        send=lambda text: True,
        load_config=load_config,
        facts=facts,
        now=lambda: BASE + world.clock[0],
        monotonic=lambda: world.clock[0],
    )
    service = core.Supervisor(deps, python="python")
    service.schedule.site_retries = facts()["site_retries"]
    at = service.schedule.site_retry_due(BASE + world.clock[0])["a"]
    world.clock[0] = at - BASE
    service._start_job("site_retry", at, world.clock[0])
    return layout.read_json(layout.schedule_path())


def test_slow_cold_start_continues_through_real_supervisor_check_chain_and_site_before_idle_stop(world):
    first = check.run_check(apply=True, notify=False, how="test")
    assert first["results"][0]["ok"] is False
    state = load_state()
    proof = state["mirrors"]["demo"]["module_warmup"]
    assert proof["phase"] == "pending"
    assert world.clock[0] < 80
    assert store.recoverable(
        "demo",
        ["https://site.example"],
        validate_settings(world.cfg["protection"]),
        world.cfg["protection"]["providers"],
        now=proof["retry_at"],
    ) == ["https://site.example"]
    schedule = reserve(world)
    assert schedule["site_retry_batch"]["a"]["module_warmup"] == NONCE
    assert schedule["site_retry_attempts"]["a"]["count"] == 1
    before = len(world.site_calls)
    final = check.run_check(apply=True, notify=False, how="retry", scheduled_scope="site_retry")
    assert final["results"], (schedule, load_state(), world.readiness_calls, world.clock, world.solver_calls)
    assert final["results"][0]["ok"] is True
    assert len(world.client.adds) == 1
    assert world.launches == [0.0]
    assert len(world.solver_calls) == 2  # initial startup, then the one completed request
    assert all(site_count == before and calls == 1 for _at, site_count, calls in world.readiness_calls)
    assert 80 <= world.clock[0] < 120
    assert world.native.ready is True
    assert world.native.idle() is False
    assert load_state()["mirrors"]["demo"]["module_warmup"]["phase"] == "consumed"
    assert load_state()["topics"][0]["last_error"] is None
    # A replay after a lost child result has no early authority left.
    before = len(world.site_calls), len(world.solver_calls)
    again = check.run_check(apply=True, notify=False, how="retry", scheduled_scope="site_retry")
    assert again["results"] == []
    assert (len(world.site_calls), len(world.solver_calls)) == before


@pytest.mark.parametrize(
    "damage", ["lost", "expired", "nonce", "config", "origin_lease", "server_pause", "exhausted", "quota", "old_broker"]
)
def test_early_continuation_never_restarts_or_requests_a_site_without_its_current_authority(world, damage, monkeypatch):
    check.run_check(apply=True, notify=False, how="test")
    schedule = reserve(world)
    state = load_state()
    proof = state["mirrors"]["demo"]["module_warmup"]
    if damage == "lost":
        state["mirrors"]["demo"].pop("module_warmup")
    elif damage == "expired":
        world.clock[0] = proof["deadline"] - BASE + 1
    elif damage == "nonce":
        proof["nonce"] = "b" * 32
    elif damage == "config":
        world.cfg["trackers"]["demo"]["download_path"] = "/changed/{id}"
    elif damage == "origin_lease":
        assert store.reserve(
            "demo", "https://site.example", validate_settings(world.cfg["protection"]), now=BASE + world.clock[0]
        )
    elif damage == "server_pause":
        state["mirrors"]["demo"]["rate_cool"] = {"https://site.example": BASE + 600}
    elif damage == "exhausted":
        # No new early batch may be reserved after the original three attempts.
        schedule["site_retry_attempts"]["a"]["count"] = 3
        layout.write_json(layout.schedule_path(), schedule)
        item = site_retries.policies(state, world.cfg)["a"]
        from tow.topic_timers import retry_deadline

        assert retry_deadline(item, schedule["site_retry_attempts"]["a"], anchor=BASE + world.clock[0]) is None
        schedule["site_retry_batch"] = {}
        layout.write_json(layout.schedule_path(), schedule)
    elif damage == "quota":
        state["daily_limit"] = {"demo": run._today()}
    else:
        monkeypatch.setattr(local_service, "readiness", lambda *a, **kw: "gone")
    save_state(state)
    before = len(world.site_calls), len(world.solver_calls)
    check.run_check(apply=True, notify=False, how="retry", scheduled_scope="site_retry")
    assert (len(world.site_calls), len(world.solver_calls)) == before
    assert world.launches == [0.0]
    assert layout.read_json(layout.schedule_path())["site_retry_attempts"]["a"]["count"] >= 1


@pytest.mark.parametrize(
    "change", ["bool_deadline", "long_deadline", "bad_nonce", "bad_origin", "disabled", "bad_config", "consumed"]
)
def test_malformed_or_consumed_saved_proof_cannot_plan_an_early_request(world, change):
    check.run_check(apply=True, notify=False, how="test")
    state = load_state()
    proof = state["mirrors"]["demo"]["module_warmup"]
    if change == "bool_deadline":
        proof["deadline"] = True
    elif change == "long_deadline":
        proof["deadline"] = proof["issued_at"] + 121
    elif change == "bad_nonce":
        proof["nonce"] = "not a nonce"
    elif change == "bad_origin":
        proof["origin"] = "http://127.0.0.1"
    elif change == "disabled":
        world.cfg["protection"]["enabled"] = False
    elif change == "bad_config":
        world.cfg["protection"] = {"unknown": True}
        assert (
            warmup.pending(state, world.cfg, "demo", observed_at=BASE + world.clock[0], now=BASE + world.clock[0]) == {}
        )
        return
    else:
        proof["phase"] = "consumed"
    item = site_retries.policies(state, world.cfg)["a"]
    assert "warmup_at" not in item


def test_slow_start_that_never_becomes_ready_consumes_only_local_wait_then_returns_to_the_original_budget(
    world, monkeypatch
):
    check.run_check(apply=True, notify=False, how="test")
    schedule = reserve(world)
    monkeypatch.setattr(local_service, "readiness", lambda *a, **kw: "warming")
    before = len(world.site_calls), len(world.solver_calls)
    result = check.run_check(apply=True, notify=False, how="retry", scheduled_scope="site_retry")
    assert result["results"] == []
    assert (len(world.site_calls), len(world.solver_calls)) == before
    assert world.clock[0] <= 120
    assert load_state()["mirrors"]["demo"]["module_warmup"]["phase"] == "failed"
    item = site_retries.policies(load_state(), world.cfg)["a"]
    assert "warmup_at" not in item
    from tow.topic_timers import retry_deadline

    at = retry_deadline(item, schedule["site_retry_attempts"]["a"], anchor=BASE + world.clock[0])
    assert at >= BASE + world.clock[0] + 3600
    assert world.launches == [0.0]
