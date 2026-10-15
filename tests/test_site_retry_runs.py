"""Site-outage retries through the real supervisor and the real check run: the finite budget
kept in ``data/run/schedule.json`` across restarts, and what a retry run checks. Fake clocks,
fake children and a fake site; no real network or processes."""

from __future__ import annotations

from datetime import UTC, datetime
from types import SimpleNamespace
from typing import Any

import pytest

from tow import errors, site_retries
from tow.config import load_config
from tow.supervisor import core, layout

T0 = datetime(2026, 10, 14, 9, tzinfo=UTC).timestamp()


def failed(identity: str = "a", reason: str = "mirrors.transport", at: float = T0) -> dict[str, Any]:
    error = errors.TowError("mirrors.all_failed", tracker="demo", error=errors.TowError(reason, error="x"))
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
        "last_check": datetime.fromtimestamp(at, UTC).isoformat(),
    }


def config() -> dict[str, Any]:
    return {"trackers": {"demo": {"fetch_hosts": ["https://demo.example"]}}, "protection": {"min_interval_sec": 1800}}


class Service:
    """A real ``Supervisor`` on a fake clock; its facts are the policies of ``self.topics``."""

    def __init__(self, *topics: dict[str, Any]) -> None:
        self.topics = list(topics)
        self.now = T0
        self.spawned: list[list[str]] = []
        self.supervisor = self.start()

    def facts(self) -> dict[str, Any]:
        state = {"topics": self.topics, "mirrors": {}}
        return {
            "site_retries": site_retries.policies(state, config()),
            "topic_ids": {str(topic["id"]) for topic in self.topics},
        }

    def start(self) -> core.Supervisor:
        """A new ``tow run``: everything it knows of earlier retries comes from schedule.json."""
        deps = core.Deps(
            spawn=lambda argv, *a: self.spawned.append(argv) or SimpleNamespace(pid=123, poll=lambda: 0),
            stop=lambda child: True,
            healthy=lambda port: True,
            port_open=lambda port: False,
            watchdog_pass=lambda now: None,
            send=lambda text: True,
            load_config=load_config,
            facts=self.facts,
            now=lambda: self.now,
            monotonic=lambda: 1000.0,
        )
        self.supervisor = core.Supervisor(deps, python="python")
        return self.supervisor

    def due(self, tid: str = "a") -> float | None:
        service = self.supervisor
        service.schedule.site_retries = self.facts()["site_retries"]
        return service.schedule.site_retry_due(self.now).get(tid)

    def dispatch(self, tid: str = "a") -> bool:
        """Start the retry job at ``tid``'s time, as the loop would; True when it was spawned."""
        before = len(self.spawned)
        at = self.due(tid)
        if at is None:
            return False
        self.now = max(self.now, at)
        self.supervisor._start_job("site_retry", self.now, 1000.0)
        return len(self.spawned) > before

    def retry_failed(self) -> None:
        """The retry child ran and the site still failed: the last check of its topics moved."""
        batch = layout.read_json(layout.schedule_path()).get("site_retry_batch") or {}
        self.now += 60
        for topic in self.topics:
            if topic["id"] in batch:
                topic["last_check"] = datetime.fromtimestamp(self.now, UTC).isoformat()

    def topic(self, tid: str) -> dict[str, Any]:
        return next(topic for topic in self.topics if topic["id"] == tid)


def saved_attempt(tid: str = "a") -> Any:
    return layout.read_json(layout.schedule_path()).get("site_retry_attempts", {}).get(tid)


@pytest.mark.parametrize("count", [0, 4, True, "1", None, 2.0])
def test_a_damaged_saved_budget_is_never_restarted(count):
    item = site_retries.policies({"topics": [failed()], "mirrors": {}}, config())["a"]
    damaged = {"revision": item["revision"], "count": count, "started_at": T0}
    layout.write_json(layout.schedule_path(), {"site_retry_attempts": {"a": damaged}})
    service = Service(failed())
    service.now = T0 + 7 * 86400
    assert service.due() is None
    assert service.dispatch() is False
    assert service.spawned == []
    assert saved_attempt() == damaged


def test_the_budget_counts_one_two_three_then_stops_across_restarts():
    service = Service(failed())
    for count in (1, 2, 3):
        assert service.dispatch()
        assert saved_attempt()["count"] == count
        assert layout.read_json(layout.schedule_path())["site_retry_batch"]["a"]["count"] == count
        assert "--site-retry-only" in service.spawned[-1]
        service.retry_failed()
        service.start()  # a new `tow run` knows the spent budget only from schedule.json
    assert service.supervisor.schedule.site_retry_attempts["a"]["count"] == 3
    service.now += 7 * 86400
    assert service.due() is None
    assert service.dispatch() is False
    assert len(service.spawned) == 3


def test_retries_back_off_between_attempts():
    service = Service(failed())
    starts = []
    for _ in range(3):
        assert service.dispatch()
        starts.append(service.now)
        service.retry_failed()
    assert starts[0] - T0 >= 300  # a site that did not answer: five minutes, then doubled
    assert starts[1] - starts[0] >= 2 * 300
    assert starts[2] - starts[1] >= 4 * 300


@pytest.mark.parametrize("leave", ["paused", "site_paused", "red_error"])
def test_a_topic_briefly_out_of_the_queue_keeps_its_spent_budget(leave):
    service = Service(failed("a"), failed("b"))
    assert service.dispatch("a")
    service.retry_failed()
    assert saved_attempt("a")["count"] == 1
    topic = service.topic("a")
    held = dict(topic)
    if leave == "paused":
        topic["paused"] = True
    elif leave == "site_paused":
        topic["last_error_class"] = "frozen"
    else:
        topic["last_error_class"] = "error"
    assert service.due("a") is None
    assert service.dispatch("b")  # another topic's retry rewrites the saved budgets
    service.retry_failed()
    assert saved_attempt("a")["count"] == 1
    service.start()
    topic.clear()
    topic.update(held)  # back in the queue, the same outage of the same topic
    assert service.dispatch("a")
    assert saved_attempt("a")["count"] == 2


def test_a_deleted_topic_or_a_week_out_of_the_queue_forgets_its_budget():
    service = Service(failed("a"), failed("b"), failed("c"))
    assert service.dispatch("a")
    service.retry_failed()
    service.topic("a")["paused"] = True
    service.topics = [topic for topic in service.topics if topic["id"] != "c"]
    service.now += 7 * 86400 + 1
    assert service.dispatch("b")
    attempts = layout.read_json(layout.schedule_path())["site_retry_attempts"]
    assert set(attempts) == {"b"}


# --- one bad saved value never stops the supervisor's facts or Home -----------------------------


@pytest.mark.parametrize(
    "damage",
    [
        {"last_error_params": ["demo"]},
        {"last_error_params": "demo"},
        {"last_error_params": None},
    ],
)
def test_a_damaged_topic_is_left_out_of_the_retries_of_the_others(damage):
    topics = [{**failed("a"), **damage}, failed("b")]
    assert set(site_retries.policies({"topics": topics, "mirrors": {}}, config())) == {"b"}


@pytest.mark.parametrize("mirrors", [{"demo": ["x"]}, {"demo": "x"}])
def test_a_damaged_mirror_record_is_read_as_none(mirrors):
    assert set(site_retries.policies({"topics": [failed()], "mirrors": mirrors}, config())) == {"a"}


def test_damaged_site_settings_mean_no_retries():
    assert site_retries.policies({"topics": [failed()], "mirrors": {}}, {"trackers": ["demo"]}) == {}


def _unreadable_retry_state() -> dict[str, Any]:
    from tow.store import save_state

    # A value no reader expects (a cooldown that is not a host map) in the saved site record.
    state = {"topics": [failed()], "mirrors": {"demo": {"cool": ["https://demo.example"]}}}
    save_state(state)
    return state


def test_supervisor_facts_survive_an_unreadable_retry_record(monkeypatch, caplog):
    from tow import config as config_module
    from tow.supervisor import _facts

    monkeypatch.setattr(config_module, "load_config", config)
    _unreadable_retry_state()
    facts = _facts()
    assert facts["site_retries"] == {}
    assert facts["topic_ids"] == {"a"}  # the other facts are still read
    assert "site retries not planned" in caplog.text


def test_home_shows_topics_with_an_unreadable_retry_record():
    from fastapi.testclient import TestClient

    from tow.config import save_config
    from tow.store import save_secrets
    from tow.web import app

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
    state = _unreadable_retry_state()
    assert layout.site_retry_status(state, cfg) == {}
    page = TestClient(app).get("/")
    assert page.status_code == 200
    assert "Show A" in page.text
