"""G4: grouped messages, links, quiet hours, daily digest."""

from __future__ import annotations

import json
from datetime import datetime, timedelta
from typing import ClassVar

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


@pytest.mark.parametrize("kind", ["error", "recovered"])
def test_a_site_message_links_the_site_not_one_of_its_topics(kind):
    # One message for several topics linked the first topic's page, as if it alone were affected.
    sent, send = _sender()
    items = [
        PendingNotification(
            kind,
            f"op-{n}",
            {"id": str(n), "title": f"Show {n}", "url": f"https://site-a.example/topic?t={n}"},
            "site-a",
            "site-a: all hosts failed: http 503" if kind == "error" else "",
        )
        for n in range(3)
    ]

    delivery.deliver(items, cfg={}, send=send)

    (text,) = sent
    assert text.endswith("\nhttps://site-a.example/")
    assert "topic?t=" not in text


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


def test_quiet_hours_overflow_is_counted_in_the_morning_message():
    state: dict = {}
    night = datetime(2026, 10, 7, 2, 0).astimezone()
    items = [
        PendingNotification("added", f"op{i}", {"id": f"t{i}", "title": f"Show {i}", "url": ""}) for i in range(250)
    ]
    delivery.stage(state, items[:150], cfg={"quiet_hours": "23-8"}, now=night)
    delivery.stage(state, items[150:], cfg={"quiet_hours": "23-8"}, now=night)
    assert len(state["notify_queue"]) == delivery.QUEUE_LIMIT
    assert state["notify_queue_dropped"] == 50
    delivery.stage(state, [], cfg={"quiet_hours": "23-8"}, now=datetime(2026, 10, 7, 9, 0).astimezone())
    (record,) = state["notify_pending"]
    heading = record["text"].split("\n\n")[0]
    assert heading == "Пока были тихие часы:\n(50 более ранних сообщений не поместились и пропущены)"
    assert "notify_queue_dropped" not in state


def test_recoveries_of_one_site_are_one_message_as_its_failure_was():
    sent, send = _sender()
    recovered = [
        PendingNotification("recovered", f"op-{n}", {"id": str(n), "title": f"Сериал {n}"}, "kinozal") for n in range(4)
    ]
    alone = [PendingNotification("recovered", "op-x", {"id": "x", "title": "Другой"}, "rutor")]

    delivery.deliver(recovered + alone, cfg={}, send=send)

    assert sent == [
        "kinozal: снова работает у 4 раздач (Сериал 0, Сериал 1, Сериал 2, Сериал 3)",
        "Другой — снова работает",
    ]


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


def test_the_digest_counts_no_add_for_a_torrent_already_in_the_client():
    sent, send = _sender()
    now = datetime(2026, 10, 1, 9, 30).astimezone()
    recent = (now - timedelta(hours=1)).isoformat()
    path = log_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    events = [
        {"kind": "client_added", "title": "Сериал А", "status": "succeeded", "created_at": recent},
        # Already in the client: verified (or its hash relabelled), nothing was added.
        {"kind": "client_updated", "title": "Сериал Б", "status": "skipped", "created_at": recent},
    ]
    path.write_text("\n".join(json.dumps(e, ensure_ascii=False) for e in events) + "\n", encoding="utf-8")

    assert delivery.maybe_digest(cfg={"daily_digest_hour": 9}, send=send, now=now) is True

    assert sent == ["TOW за сутки: добавлено в клиент 1, новых серий и файлов 0, загружено 0"]


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


def test_a_check_with_several_messages_delivers_the_queue_once(monkeypatch):
    # dispatch() calls send() per record: each delivered the whole queue again, so five messages
    # to three dead chats retried each chat fifteen times (minutes, under the check lock).
    from tow.check import notices

    deliveries = []
    monkeypatch.setattr("tow.notifiers.connected", lambda _s: [("telegram", None, None)])
    monkeypatch.setattr(
        "tow.notifiers.deliver_queued", lambda _s: deliveries.append(1) or {"telegram": (False, "chat gone")}
    )
    state = load_state()
    delivery.stage(state, [_error(str(n), f"Show {n}", tracker=f"site{n}") for n in range(5)], cfg={})
    save_state(state)

    notices.flush_notifications({}, {}, how="auto", notify=False)

    assert len(deliveries) == 1
    kinds = [json.loads(line)["kind"] for line in log_path().read_text(encoding="utf-8").splitlines()]
    assert kinds.count("bot_delivery_started") == 5  # every message is still audited
    assert kinds.count("bot_delivery_failed") == 5


def test_a_digest_whose_hour_falls_in_the_evening_quiet_hours_comes_when_they_end():
    # daily_digest_hour 23 with quiet hours 22-7: at 23 it was quiet, after 7 it was "before 23" -
    # the digest was never sent.
    sent, send = _sender()
    cfg = {"daily_digest_hour": 23, "quiet_hours": "22-7"}
    evening = datetime(2026, 10, 1, 23, 30).astimezone()
    morning = datetime(2026, 10, 2, 7, 30).astimezone()

    assert delivery.maybe_digest(cfg=cfg, send=send, now=evening) is False  # held
    assert delivery.maybe_digest(cfg=cfg, send=send, now=morning) is True
    assert delivery.maybe_digest(cfg=cfg, send=send, now=morning + timedelta(hours=3)) is False  # once
    assert load_state()["digest_sent_on"] == "2026-10-01"
    assert len(sent) == 1
    # Before its hour, a digest that was not held by the quiet hours is not due yet.
    assert delivery.maybe_digest(cfg={"daily_digest_hour": 9}, send=send, now=morning) is False


def test_a_failed_first_check_that_was_reported_is_closed_by_works_again(monkeypatch):
    # notify_topic_error sent "Problem" for a new topic's crashed first check but left no mark:
    # the check that then worked never said "works again".
    from tow.check import notices

    save_state({"topics": [{"id": "t1", "title": "Show A", "url": "https://site-a.example/t/1"}], "mirrors": {}})
    notices.notify_topic_error(dict(load_state()["topics"][0]), "site-a: all hosts failed", how="manual")
    assert load_state()["topics"][0]["error_notified"] is True

    class Run:
        notify = True
        queued: ClassVar[list[str]] = []

        def queue_notification(self, _topic, **kw):
            self.queued.append(kw["kind"])

    notices.queue_recoveries(load_state(), [{"id": "t1", "ok": True, "tracker": "site-a"}], Run())
    assert Run.queued == ["recovered"]
