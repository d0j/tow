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

    def due(self) -> float | None:
        service = self.supervisor
        service.schedule.site_retries = self.facts()["site_retries"]
        return service.schedule.site_retry_due(self.now).get("a")

    def dispatch(self) -> bool:
        """Start the retry job at its time, as the loop would; True when it was spawned."""
        before = len(self.spawned)
        at = self.due()
        if at is None:
            return False
        self.now = max(self.now, at)
        self.supervisor._start_job("site_retry", self.now, 1000.0)
        return len(self.spawned) > before

    def retry_failed(self) -> None:
        """The retry child ran and the site still failed: the topic's last check moved."""
        self.topics = [failed(str(topic["id"]), at=self.now + 60) for topic in self.topics]
        self.now += 60


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
