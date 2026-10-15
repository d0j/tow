"""Manual checks: truthful outcomes, FIFO backpressure and linked explanations."""

from __future__ import annotations

import time
from urllib.parse import parse_qs, urlsplit

import pytest
from bs4 import BeautifulSoup
from fastapi.testclient import TestClient
from helpers import flash_of, wait_for_check_job

from tow.check_outcomes import counts_of, outcome_of
from tow.log import format_event
from tow.store import CheckBusyError, save_state
from tow.web import app, check_jobs
from tow.web.views import check_report


@pytest.fixture
def client():
    return TestClient(app, headers={"Origin": "http://127.0.0.1"})


def _job(response):
    return parse_qs(urlsplit(response.headers["location"]).query)["check_job"][0]


class _Consumer:
    """A queue consumer the test runs itself, in its own thread: what happens "while a check
    runs" happens inside the fake check, in order - no second thread, no timing."""

    def __init__(self, target, args):
        self.target, self.args, self.alive = target, args, True

    def is_alive(self):
        return self.alive

    def run(self):
        try:
            self.target(*self.args)
        finally:
            self.alive = False


@pytest.fixture
def consumers(monkeypatch):
    """The consumers the queue started, not running until the test calls ``run()``."""
    started: list[_Consumer] = []

    def start(_name, target, *args):
        started.append(_Consumer(target, args))
        return started[-1]

    monkeypatch.setattr(check_jobs, "_start", start)
    monkeypatch.setattr(check_jobs, "_worker", None)  # restored after the test: no fake outlives it
    return started


def _status(client, job):
    return client.get(f"/check/status?job={job}").json()


@pytest.mark.parametrize(
    ("row", "expected"),
    [
        ({"ok": True}, "same"),
        ({"ok": True, "changed": True}, "new"),
        ({"ok": False, "changed": True}, "fail"),
        ({"ok": False, "status": "verification_pending", "changed": True}, "pending"),
        ({"ok": False, "status": "waiting_space", "error": "disk"}, "pending"),
        ({"ok": True, "status": "skipped"}, "skipped"),
        ({"ok": True, "added": True, "note": "changed meanwhile"}, "skipped"),
        ({"ok": False, "status": "skipped", "error": "site refused"}, "fail"),
        ({"ok": True, "selection_updated": True}, "new"),
        ({"ok": True, "started": True}, "new"),
    ],
)
def test_outcomes_are_mutually_exclusive(row, expected):
    assert outcome_of(row) == expected
    counts = counts_of([row])
    assert counts[expected] == sum(counts.values()) == 1


def test_pending_verification_is_not_a_failure_or_a_confirmed_add(client, monkeypatch):
    rows = [
        {"id": "a", "title": "Show A", "ok": True},
        {"id": "b", "title": "Show B", "ok": False, "status": "verification_pending", "changed": True},
        {"id": "c", "title": "Show C", "ok": True, "status": "skipped", "skipped": "paused"},
        {"id": "d", "title": "Show D", "ok": False, "error": "client refused"},
    ]
    monkeypatch.setattr("tow.web.services.run_check", lambda **_kw: {"results": rows})
    job = _job(client.post("/check", data={"mode": "apply"}, follow_redirects=False))
    finished = wait_for_check_job(client, job)
    assert "ожидают подтверждения 1" in finished["flash"]
    assert "с ошибкой 1" in finished["flash"]
    page = BeautifulSoup(client.get(finished["redirect"]).text, "html.parser")
    assert page.select_one(f'a[href="/?check_result={job}#check-result"]')
    result = BeautifulSoup(client.get(f"/?check_result={job}").text, "html.parser")
    assert len(result.select(".check-result-row")) == 4
    assert "Клиент проверяет существующие файлы" in result.get_text()
    assert "Это результат данного запуска" in result.get_text()
    assert result.select_one('a[href="/?open=d&spot=row-d#row-d"]')
    assert not result.select_one("[data-check-result-pending]")


def test_busy_topics_wait_fifo_and_duplicate_clicks_share_the_job(client, monkeypatch, consumers):
    calls, seen = [], {}

    def check(**kw):
        ids = kw.get("ids")
        calls.append(ids)
        if ids is None:  # while the check of every topic runs, rows are clicked
            seen["second"] = _job(client.post("/topics/b/check", follow_redirects=False))
            seen["third"] = _job(client.post("/topics/c/check", follow_redirects=False))
            seen["duplicate"] = _job(client.post("/topics/c/check", follow_redirects=False))
            seen["second_status"] = _status(client, seen["second"])["status"]
            seen["third_position"] = _status(client, seen["third"])["position"]
            seen["calls"] = list(calls)
            seen["page"] = client.get(f"/?check_job={seen['third']}").status_code
        return {"results": [{"id": (ids or ["a"])[0], "title": "Show A", "ok": True}]}

    monkeypatch.setattr("tow.web.services.run_check", check)
    first = _job(client.post("/check", data={"mode": "apply"}, follow_redirects=False))
    assert len(consumers) == 1
    consumers[0].run()
    assert seen["duplicate"] == seen["third"]
    assert (seen["second_status"], seen["third_position"], seen["calls"], seen["page"]) == ("queued", 3, [None], 200)
    assert len(consumers) == 1  # one consumer served the whole queue
    for job in (first, seen["second"], seen["third"]):
        assert _status(client, job)["status"] == "done"
    assert calls == [None, ["b"], ["c"]]


def test_scheduled_lock_busy_is_retried_without_recording_a_failure(client, monkeypatch):
    attempts = []

    def check(**kw):
        attempts.append(kw)
        if len(attempts) < 3:
            raise CheckBusyError("busy")
        return {"results": [{"id": "b", "ok": True}]}

    monkeypatch.setattr("tow.web.services.run_check", check)
    failures = []
    monkeypatch.setattr("tow.web.services.record_check_failure", lambda *a, **kw: failures.append(a))
    job = _job(client.post("/topics/b/check", follow_redirects=False))
    assert wait_for_check_job(client, job)["status"] == "done"
    assert len(attempts) == 3
    assert all(call["wait"] is False and call["ids"] == ["b"] for call in attempts)
    assert failures == []


def test_cancel_only_removes_queued_work(client, monkeypatch, consumers):
    calls, seen = [], {}

    def check(**kw):
        calls.append(kw.get("ids"))
        if kw.get("ids") is None:  # while it runs, a row is clicked and its wait cancelled
            seen["queued"] = queued = _job(client.post("/topics/b/check", follow_redirects=False))
            cancelled = client.post(f"/check/{queued}/cancel", follow_redirects=False)
            seen["cancelled"] = flash_of(cancelled.headers["location"])
            seen["queued_status"] = _status(client, queued)["status"]
            refused = client.post(f"/check/{first}/cancel", follow_redirects=False)
            seen["refused"] = flash_of(refused.headers["location"])
            seen["page"] = client.get(f"/?check_job={first}").text
        return {"results": []}

    monkeypatch.setattr("tow.web.services.run_check", check)
    first = _job(client.post("/check", data={"mode": "apply"}, follow_redirects=False))
    consumers[0].run()
    assert "удалена из очереди" in seen["cancelled"]
    assert seen["queued_status"] == "failed"
    assert "уже началась" in seen["refused"]
    assert "Отменить ожидание" not in seen["page"]
    assert _status(client, first)["status"] == "done"
    assert calls == [None]


@pytest.mark.parametrize("query", ["check_job", "check_result"])
def test_restart_or_expired_result_never_says_success(client, query):
    page = client.get(f"/?{query}=not-a-job").text
    assert "TOW перезапущен или сведения устарели" in page
    assert client.get("/check/status?job=not-a-job").json() == {"status": "unknown"}


def test_reports_escape_markup_and_bound_untrusted_details(client, monkeypatch):
    row = {
        "id": 'bad"><svg',
        "title": '<img src=x onerror="x">',
        "ok": False,
        "error": "<script>x</script>" + "x" * 5000,
    }
    monkeypatch.setattr("tow.web.services.run_check", lambda **_kw: {"results": [row]})
    job = _job(client.post("/check", data={"mode": "apply"}, follow_redirects=False))
    wait_for_check_job(client, job)
    page = BeautifulSoup(client.get(f"/?check_result={job}").text, "html.parser")
    assert not page.select(".check-results img, .check-results script, .check-result-row a")
    report = check_jobs.get(job)["report"]
    assert len(report["rows"][0]["reason"]) <= 1000
    assert report["rows"][0]["topic_id"] == ""


def test_result_rows_are_bounded_but_all_outcomes_are_counted():
    report = check_report(
        {"results": [{"ok": True}] * 150 + [{"id": "last", "ok": False, "error": "failed"}]}, apply=True
    )
    assert len(report["rows"]) == 100
    assert report["total"] == 151
    assert report["counts"]["same"] == 150
    assert report["rows"][0]["topic_id"] == "last"


def test_preview_never_claims_that_changes_were_applied():
    report = check_report({"results": [{"ok": True, "changed": True}]}, apply=False)
    assert "изменится при применении 1" in report["summary"]
    assert report["rows"][0]["label"] == "Изменится"


def test_pending_is_visible_on_home_without_fake_green(client):
    save_state(
        {
            "topics": [
                {
                    "id": "a",
                    "title": "Show A",
                    "url": "https://rutor.info/torrent/1234567/show-a",
                    "save_path": "D:\\TV",
                    "last_ok": False,
                    "verification_pending": {"since": "2026-10-15T10:00:00+03:00"},
                }
            ]
        }
    )
    page = BeautifulSoup(client.get("/").text, "html.parser")
    assert page.select_one("#row-a .dot.mut")
    assert "Проверка файлов" in page.select_one("#row-a .topic-event").get_text()


def test_log_distinguishes_waiting_from_errors():
    result = format_event(
        {"kind": "check", "n": 3, "ok": 1, "outcomes": {"new": 0, "same": 1, "pending": 1, "skipped": 0, "fail": 1}}
    )
    assert "ожидание: 1" in result["detail"]
    assert "ошибки: 1" in result["detail"]


def test_queue_and_result_memory_are_bounded(consumers):
    for index in range(check_jobs.MAX_PENDING):
        job, added = check_jobs.submit(topic_id=str(index), apply=True, lang="ru", perform=lambda _: None)
        assert job
        assert added
    job, added = check_jobs.submit(topic_id="overflow", apply=True, lang="ru", perform=lambda _: None)
    assert job is None
    assert not added
    duplicate, added = check_jobs.submit(topic_id="0", apply=True, lang="ru", perform=lambda _: None)
    assert duplicate
    assert not added
    check_jobs.PENDING.clear()
    check_jobs.JOBS.clear()
    for index in range(64):
        check_jobs.JOBS[str(index)] = {"status": "done", "finished_at": time.monotonic()}
    job, added = check_jobs.submit(topic_id="new", apply=True, lang="ru", perform=lambda _: None)
    assert job
    assert added
    assert len(check_jobs.JOBS) == 64


def test_expired_result_is_not_exposed_or_retained():
    check_jobs.JOBS["old"] = {"status": "done", "finished_at": time.monotonic() - check_jobs.RESULT_TTL - 1}
    assert check_jobs.get("old") is None


@pytest.mark.parametrize("crash", [True, False])
def test_a_broken_result_handler_does_not_strand_the_queue(client, crash):
    def broken(_job):
        if crash:
            raise ValueError("synthetic handler failure")

    job, _ = check_jobs.submit(topic_id="a", apply=True, lang="ru", perform=broken)
    finished = wait_for_check_job(client, job["id"])
    assert finished["status"] == "failed"
    assert not check_jobs.has_pending()
    assert "не подтвержден" not in finished.get("flash", "")  # never fabricated success
    assert "Результат недоступен" in client.get(f"/?check_result={job['id']}").text


class _ThreadStop(BaseException):
    """Not an Exception: what ends a thread (SystemExit, KeyboardInterrupt) in the middle of a check."""


def test_a_consumer_ended_mid_check_never_leaves_its_job_running_or_replays_it(client, consumers):
    performed = []

    def ended(job):
        performed.append(job["topic_id"])
        if job["topic_id"] == "a":
            raise _ThreadStop

    first, _ = check_jobs.submit(topic_id="a", apply=True, lang="ru", perform=ended)
    with pytest.raises(_ThreadStop):
        consumers[0].run()
    assert _status(client, first["id"])["status"] == "failed"
    assert not check_jobs.has_pending()
    # The next request gets a new consumer, which never runs the ended job (a re-add) again.
    second, _ = check_jobs.submit(topic_id="b", apply=True, lang="ru", perform=ended)
    assert len(consumers) == 2
    consumers[1].run()
    assert performed == ["a", "b"]
    assert check_jobs.get(second["id"])["status"] == "failed"  # ended() never reported a result


def test_a_missing_queued_topic_is_skipped_not_changed_into_check_all(client, monkeypatch):
    attempts = []

    def check(**kw):
        attempts.append(kw.get("ids"))
        if len(attempts) == 1:
            raise CheckBusyError("busy")
        return {"results": []}

    monkeypatch.setattr("tow.web.services.run_check", check)
    job = _job(client.post("/topics/deleted/check", follow_redirects=False))
    result = wait_for_check_job(client, job)
    assert result["status"] == "done"
    assert attempts == [["deleted"], ["deleted"]]
    assert "пропущено 1" in result["flash"]
    assert check_jobs.get(job)["report"]["rows"][0]["outcome"] == "skipped"
