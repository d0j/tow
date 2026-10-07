"""A check with nothing to check asks no torrent client (a new install has none set up yet)."""

from __future__ import annotations

from typing import Any

from tow import check
from tow.store import load_state, save_state


def _never_opened(monkeypatch) -> list[str]:
    opened: list[str] = []

    def open_client(_cfg: dict[str, Any], _secrets: dict[str, Any], client_id: str, *, apply: bool) -> Any:
        opened.append(client_id)
        raise RuntimeError("no address given")

    monkeypatch.setattr("tow.check.client_ops._open_client", open_client)
    return opened


def test_a_check_without_topics_does_not_ask_or_log_the_client(monkeypatch):
    opened = _never_opened(monkeypatch)
    logged: list[str] = []
    monkeypatch.setattr("tow.check.run.log_event", lambda kind, **_fields: logged.append(kind))
    save_state({"topics": []})

    result = check.run_check(apply=True, notify=False, how="auto")

    assert result["results"] == []
    assert opened == []
    assert "client_unreachable" not in logged
    health = load_state()["health"]
    assert "qbit_ok" not in health  # the header says "not asked yet", never "down" or "answers"
    assert "at_ts" in health  # the run itself is recorded: the watchdog sees checks on time


def test_a_check_without_topics_keeps_what_the_last_check_saw(monkeypatch):
    _never_opened(monkeypatch)
    save_state(
        {
            "topics": [],
            "health": {"qbit": "ok", "client": "ok", "qbit_ok": True, "clients_ok": {"qbittorrent": True}},
        }
    )

    check.run_check(apply=True, notify=False, how="auto")

    health = load_state()["health"]
    assert health["qbit_ok"] is True
    assert health["clients_ok"] == {"qbittorrent": True}


def test_a_check_with_a_topic_still_asks_the_client(monkeypatch):
    opened = _never_opened(monkeypatch)
    # Paused: the run includes it (and asks the client) without fetching the site.
    save_state({"topics": [{"id": "t1", "title": "Show", "url": "http://rutor.info/torrent/1", "paused": True}]})

    check.run_check(apply=True, notify=False, how="auto")

    assert opened  # the main client is asked when there is something to check
    assert load_state()["health"]["qbit_ok"] is False
