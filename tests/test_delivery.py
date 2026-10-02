"""G4: grouped messages, links, quiet hours, daily digest."""

from __future__ import annotations

import json
from datetime import datetime, timedelta

import pytest

from tow import delivery
from tow.log import log_path
from tow.notify import PendingNotification
from tow.store import load_state, save_state


def _error(topic_id: str, title: str, tracker: str = "nnmclub", error: str = "nnmclub: all hosts failed: http 503"):
    topic = {"id": topic_id, "title": title, "url": f"https://nnmclub.to/forum/viewtopic.php?t={topic_id}"}
    return PendingNotification("error", f"op-{topic_id}", topic, tracker, error, "")


def _sender():
    sent: list[str] = []

    def send(*, text, operation_id, topic):
        sent.append(text)
        return True

    return sent, send


@pytest.fixture(autouse=True)
def _state():
    save_state({"topics": [], "mirrors": {}})


def test_one_message_for_a_tracker_failing_for_several_topics():
    sent, send = _sender()
    items = [_error(str(n), f"Show {n}") for n in range(7)] + [_error("x", "Other", tracker="rutor")]

    delivery.deliver(items, cfg={}, send=send)

    assert len(sent) == 2
    assert sent[0].startswith(
        "Сбой — nnmclub: сайт недоступен у 7 раздач (Show 0, Show 1, Show 2, Show 3, Show 4 и ещё 2)"
    )
    assert sent[1].startswith("Сбой — Other: ")
    assert sent[1].endswith("\nhttps://nnmclub.to/forum/viewtopic.php?t=x")


def test_topic_messages_carry_the_link_but_client_messages_do_not():
    sent, send = _sender()
    topic = {"id": "t", "title": "Show", "url": "http://rutor.info/torrent/1"}
    items = [
        PendingNotification("completed", "a", topic, "rutor", "", "S01E02"),
        PendingNotification("qbit_down", "b", {"id": "__client__:main", "title": ""}, "", "", ""),
    ]

    delivery.deliver(items, cfg={}, send=send)

    assert sent[0].endswith("\nhttp://rutor.info/torrent/1")
    assert sent[1] == "Торрент-клиент недоступен"


@pytest.mark.parametrize(
    ("spec", "hour", "quiet"),
    [("23-8", 23, True), ("23-8", 3, True), ("23-8", 8, False), ("1-6", 5, True), ("1-6", 6, False), ("", 3, False)],
)
def test_quiet_hours_window(spec, hour, quiet):
    now = datetime(2026, 10, 1, hour, 30).astimezone()
    assert delivery.quiet_now({"quiet_hours": spec}, now) is quiet


def test_quiet_hours_hold_messages_and_send_them_together_afterwards():
    sent, send = _sender()
    cfg = {"quiet_hours": "23-8"}
    night = datetime(2026, 10, 1, 2, 0).astimezone()
    morning = datetime(2026, 10, 1, 9, 0).astimezone()

    delivery.deliver([_error("1", "Night show", tracker="")], cfg=cfg, send=send, now=night)
    assert sent == []
    assert len(load_state()["notify_queue"]) == 1

    delivery.deliver([], cfg=cfg, send=send, now=morning)
    assert len(sent) == 1
    assert sent[0].startswith("Пока были тихие часы:\n\nСбой — Night show")
    assert "notify_queue" not in load_state()


def test_daily_digest_once_a_day_after_its_hour():
    sent, send = _sender()
    now = datetime(2026, 10, 1, 9, 30).astimezone()
    path = log_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    recent = (now - timedelta(hours=2)).isoformat()
    events = [
        {"kind": "client_added", "title": "Сериал А", "created_at": recent},
        {"kind": "episode_completed", "title": "Сериал А / Show A", "created_at": recent},
        {"kind": "file_completed", "title": "Сериал Б", "created_at": recent},
        {"kind": "episode_completed", "title": "Old", "created_at": (now - timedelta(days=3)).isoformat()},
    ]
    path.write_text("\n".join(json.dumps(e, ensure_ascii=False) for e in events) + "\n", encoding="utf-8")
    cfg = {"daily_digest_hour": 9}

    assert delivery.maybe_digest(cfg=cfg, send=send, now=now.replace(hour=8)) is False
    assert delivery.maybe_digest(cfg=cfg, send=send, now=now) is True
    assert delivery.maybe_digest(cfg=cfg, send=send, now=now + timedelta(hours=3)) is False

    assert sent == [
        "TOW за сутки: добавлено в клиент 1, новых серий и файлов 0, загружено 2\nЗагружено: Сериал А, Сериал Б"
    ]


def test_the_digest_is_queued_in_the_same_write_as_its_day(monkeypatch):
    now = datetime(2026, 10, 1, 9, 30).astimezone()

    def stopped(_send):
        raise SystemExit("TOW stopped before handing the digest over")

    real_dispatch = delivery.dispatch
    monkeypatch.setattr(delivery, "dispatch", stopped)
    with pytest.raises(SystemExit):
        delivery.maybe_digest(cfg={"daily_digest_hour": 9}, send=lambda **_k: True, now=now)
    state = load_state()
    assert state["digest_sent_on"] == "2026-10-01"
    assert [record["operation_id"] for record in state["notify_pending"]] == ["daily-digest"]  # not lost

    monkeypatch.setattr(delivery, "dispatch", real_dispatch)
    sent, send = _sender()
    assert delivery.maybe_digest(cfg={"daily_digest_hour": 9}, send=send, now=now) is False  # not twice
    delivery.dispatch(send)
    assert len(sent) == 1
    assert sent[0].startswith("TOW за сутки")


def test_no_digest_without_its_setting():
    sent, send = _sender()

    assert delivery.maybe_digest(cfg={}, send=send) is False
    assert sent == []
