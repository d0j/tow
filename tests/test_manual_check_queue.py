"""Manual checks: truthful outcomes, FIFO backpressure and linked explanations."""

from __future__ import annotations

import copy
import time
from urllib.parse import parse_qs, urlsplit

import pytest
from bs4 import BeautifulSoup
from fastapi.testclient import TestClient
from helpers import flash_of, flash_record, manual_check_consumers, wait_for_check_job

from tow.check_outcomes import counts_of, outcome_of
from tow.log import format_event
from tow.store import CheckBusyError, SecretStoreError, save_state
from tow.web import app, check_actions, check_jobs, views
from tow.web.views import check_report


@pytest.fixture
def client():
    return TestClient(app, headers={"Origin": "http://127.0.0.1"})


def _job(response):
    return parse_qs(urlsplit(response.headers["location"]).query)["check_job"][0]


@pytest.fixture
def consumers(monkeypatch):
    return manual_check_consumers(monkeypatch)


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
        # A new version the client already had: nothing to add, but the topic changed.
        ({"ok": True, "status": "skipped", "skipped": "already in the client", "changed": True}, "new"),
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
    assert finished["redirect"] == f"/?check_result={job}#check-result"
    result = BeautifulSoup(client.get(finished["redirect"]).text, "html.parser")
    assert len(result.select(".check-result-row")) == 4
    assert "Клиент проверяет существующие файлы" in result.get_text()
    assert "Итоги этой проверки" in result.get_text()
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


def test_a_click_during_a_running_check_gets_one_follow_up_check(client, monkeypatch, consumers):
    # The running check may have read the topic before the change the owner clicks for: a click
    # then needs a check of its own, but further clicks share that one waiting follow-up.
    calls, seen = [], {}

    def check(**kw):
        calls.append(kw.get("ids"))
        if len(calls) == 1:
            seen["follow_up"] = _job(client.post("/topics/b/check", follow_redirects=False))
            seen["again"] = _job(client.post("/topics/b/adopt", follow_redirects=False))
            seen["message"] = flash_of(client.post("/topics/b/check", follow_redirects=False).headers["location"])
        return {"results": [{"id": "b", "title": "Show B", "ok": True}]}

    monkeypatch.setattr("tow.web.services.run_check", check)
    monkeypatch.setattr("tow.web.services.adopt_topic", lambda *_a, **_kw: None)
    first = check_jobs.submit(topic_id="b", apply=True, lang="ru", perform=check_actions.perform_check)[0]
    consumers[0].run()
    assert seen["follow_up"] != first["id"]
    assert seen["again"] == seen["follow_up"]
    assert "в очередь" in seen["message"]
    assert calls == [["b"], ["b"]]  # exactly one later check, never two at once
    assert check_jobs.get(seen["follow_up"])["adopted"] is True  # the shared request kept the adoption


def test_a_queued_topic_check_re_adds_only_what_was_gone_by_the_last_click(client, monkeypatch, consumers, clock):
    # A row check that waited must not add back a torrent the owner removed after clicking: the
    # check gets the time of the request it serves (the latest click on a shared job).
    seen = []
    monkeypatch.setattr(
        "tow.web.services.run_check",
        lambda **kw: seen.append(kw.get("readd_since")) or {"results": [{"id": "b", "title": "Show B", "ok": True}]},
    )
    first_click = clock.wall
    first = check_jobs.submit(topic_id="b", apply=True, lang="en", perform=check_actions.perform_check)[0]
    clock.wall += 60
    shared = check_jobs.submit(topic_id="b", apply=True, lang="en", perform=check_actions.perform_check)[0]
    every = check_jobs.submit(topic_id="", apply=True, lang="en", perform=check_actions.perform_check)[0]
    assert shared["id"] == first["id"]
    consumers[0].run()
    assert seen == [first_click + 60, None]  # the latest click; checking every topic re-adds nothing by request
    assert every["id"] != first["id"]


def test_a_waiting_preview_never_delays_an_applying_check(client, monkeypatch, consumers):
    # A preview never takes the check lock, so it waits in a lane of its own.
    calls = []
    busy = [True]

    def check(**kw):
        calls.append((kw["apply"], kw.get("ids")))
        if kw["apply"] and busy[0]:
            raise CheckBusyError("a scheduled check holds the lock")
        return {"results": [{"id": (kw.get("ids") or ["a"])[0], "title": "Show A", "ok": True}]}

    monkeypatch.setattr("tow.web.services.run_check", check)
    preview = _job(client.post("/check", data={"mode": "dry"}, follow_redirects=False))
    busy[0] = False
    at_once = client.post("/topics/b/check", follow_redirects=False)
    assert "check_job" not in at_once.headers["location"]  # checked now, not queued behind the preview
    assert calls == [(True, ["b"])]
    busy[0] = True
    queued = _job(client.post("/topics/c/check", follow_redirects=False))
    assert _status(client, queued)["position"] == 1  # first of its own lane
    assert len(consumers) == 2  # the applying lane has its own consumer
    busy[0] = False
    consumers[1].run()
    assert _status(client, queued)["status"] == "done"
    assert _status(client, preview)["status"] == "queued"  # still waiting: it delayed nothing
    consumers[0].run()
    assert _status(client, preview)["status"] == "done"
    assert calls == [(True, ["b"]), (True, ["c"]), (True, ["c"]), (False, None)]


class _Clock:
    """The queue's time: a monotonic clock the test moves, and a wall clock that is put back."""

    def __init__(self):
        self.mono, self.wall = 1000.0, 1_791_000_000.0

    def monotonic(self):
        return self.mono

    def time(self):
        return self.wall


@pytest.fixture
def clock(monkeypatch):
    fake = _Clock()
    monkeypatch.setattr(check_jobs, "time", fake)
    return fake


def _pauses(monkeypatch, clock, step, during=None):
    """Each wait for the check lock moves the clocks: ``step`` seconds on, the wall clock an hour back."""
    pauses = []

    def pause(seconds):
        pauses.append(seconds)
        clock.mono += step
        clock.wall -= 3600
        if during:
            during(len(pauses))

    monkeypatch.setattr(check_jobs, "_pause", pause)
    return pauses


def test_scheduled_lock_busy_is_retried_without_recording_a_failure(client, monkeypatch, consumers, clock):
    attempts = []

    def check(**kw):
        attempts.append(kw)
        if len(attempts) < 3:
            raise CheckBusyError("busy")
        return {"results": [{"id": "b", "ok": True}]}

    monkeypatch.setattr("tow.web.services.run_check", check)
    failures = []
    monkeypatch.setattr("tow.web.services.record_check_failure", lambda *a, **kw: failures.append(a))
    pauses = _pauses(monkeypatch, clock, 1)
    job = _job(client.post("/topics/b/check", follow_redirects=False))
    consumers[0].run()
    assert _status(client, job)["status"] == "done"
    assert len(attempts) == 3
    assert all(call["wait"] is False and call["ids"] == ["b"] for call in attempts)
    assert pauses == [1.0]  # once a second, never a busy loop
    assert failures == []


def test_a_check_waiting_too_long_for_the_lock_ends_as_not_run(client, monkeypatch, consumers, clock):
    attempts, failures, seen = [], [], []

    def check(**kw):
        attempts.append(kw["ids"])
        if kw["ids"] == ["b"]:
            raise CheckBusyError("a scheduled check holds the lock")
        return {"results": [{"id": "c", "title": "Show C", "ok": True}]}

    def during(count):
        if count in {1, 7}:
            seen.append((_status(client, job), client.get(f"/?check_job={job}").text))

    monkeypatch.setattr("tow.web.services.run_check", check)
    monkeypatch.setattr("tow.web.services.record_check_failure", lambda *a, **kw: failures.append(a))
    monkeypatch.setattr("tow.web.services.log_event", lambda *a, **kw: failures.append(a))
    pauses = _pauses(monkeypatch, clock, 600, during)
    job = _job(client.post("/topics/b/check", follow_redirects=False))
    behind = _job(client.post("/topics/c/check", follow_redirects=False))
    consumers[0].run()
    # Refused at 0, 10, ..., 60 minutes of waiting; at 70 minutes (over 3900 s) it gives up.
    assert len(pauses) == 7
    assert attempts == [["b"]] * 9 + [["c"]]
    first, last = seen
    assert first[0]["status"] == "queued"
    assert first[0]["progress"].startswith("ждёт завершения другой проверки (с ")
    assert last[0]["progress"] == first[0]["progress"]  # the wall clock was put back: the start stays
    assert first[0]["progress"] in first[1]
    ended = _status(client, job)
    assert ended["status"] == "failed"
    assert "не запущена" in ended["flash"]
    assert failures == []  # waiting is not a site or topic failure
    assert _status(client, behind)["status"] == "done"  # the queue moved on


def test_a_long_running_check_says_since_when_and_keeps_its_lane(client, monkeypatch, consumers, clock):
    # No watchdog kills a hung check (that could overlap client writes): the page says how long it runs.
    from datetime import UTC, datetime

    from tow.clock import format_ui_timestamp

    calls, seen = [], {}
    began = format_ui_timestamp(datetime.fromtimestamp(clock.wall, UTC).isoformat())

    def check(**kw):
        calls.append(kw["ids"])
        if kw["ids"] == ["b"]:
            clock.wall += 2 * 3600  # it runs for hours
            seen["status"] = _status(client, job)
            seen["page"] = client.get(f"/?check_job={job}").text
            seen["behind"] = _status(client, _job(client.post("/topics/c/check", follow_redirects=False)))
        return {"results": [{"id": kw["ids"][0], "title": "Show", "ok": True}]}

    monkeypatch.setattr("tow.web.services.run_check", check)
    job = check_jobs.submit(topic_id="b", apply=True, lang="ru", perform=check_actions.perform_check)[0]["id"]
    consumers[0].run()
    assert seen["status"]["progress"].startswith(f"идёт проверка с {began}.")
    assert seen["status"]["progress"] in seen["page"]
    assert (seen["behind"]["status"], seen["behind"]["position"]) == ("queued", 2)
    assert calls == [["b"], ["c"]]


def test_a_check_that_gets_the_lock_just_in_time_runs(client, monkeypatch, consumers, clock):
    start = clock.mono

    def check(**_kw):
        if clock.mono - start < check_jobs.LOCK_WAIT_SEC - 1:
            raise CheckBusyError("busy")
        return {"results": [{"id": "b", "title": "Show B", "ok": True}]}

    monkeypatch.setattr("tow.web.services.run_check", check)
    pauses = _pauses(monkeypatch, clock, 1299.9)
    job = _job(client.post("/topics/b/check", follow_redirects=False))
    consumers[0].run()
    assert len(pauses) == 3
    assert _status(client, job)["status"] == "done"


def test_a_check_waiting_for_the_lock_can_be_cancelled(client, monkeypatch, consumers, clock):
    attempts, seen = [], {}

    def check(**kw):
        attempts.append(kw["ids"])
        raise CheckBusyError("busy")

    def during(_count):
        if "cancelled" not in seen:
            seen["cancelled"] = flash_of(
                client.post(f"/check/{job}/cancel", follow_redirects=False).headers["location"]
            )

    monkeypatch.setattr("tow.web.services.run_check", check)
    _pauses(monkeypatch, clock, 1, during)
    job = _job(client.post("/topics/b/check", follow_redirects=False))
    consumers[0].run()
    assert "удалена из очереди" in seen["cancelled"]
    assert attempts == [["b"], ["b"]]  # the click, then the queue's one attempt before the cancel
    assert _status(client, job)["status"] == "failed"
    assert not check_jobs.has_pending(apply=True)


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


def test_cancelling_an_unknown_check_does_not_say_it_already_started(client):
    response = client.post("/check/not-a-job/cancel", follow_redirects=False)
    message = flash_of(response.headers["location"])
    assert "уже началась" not in message
    assert message == "эта проверка больше не известна: TOW перезапущен или сведения устарели; ничего не отменено"


@pytest.mark.parametrize("query", ["check_job", "check_result"])
def test_restart_or_expired_result_never_says_success(client, query):
    page = client.get(f"/?{query}=not-a-job").text
    assert "TOW перезапущен или сведения устарели" in page
    unknown = client.get("/check/status?job=not-a-job").json()
    assert unknown["status"] == "unknown"
    assert unknown["message"]["kind"] == "warn"  # unavailable, unconfirmed: never success


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


def test_a_long_result_does_not_send_the_rest_to_history(client, monkeypatch, consumers):
    # Unchanged topics never appear in History: "the rest is in History" sent the owner nowhere.
    rows = [{"id": f"t{index}", "title": "Show", "ok": True} for index in range(150)]
    monkeypatch.setattr("tow.web.services.run_check", lambda **_kw: {"results": rows})
    job = _job(client.post("/check", data={"mode": "apply"}, follow_redirects=False))
    consumers[0].run()
    card = BeautifulSoup(client.get(f"/?check_result={job}").text, "html.parser").select_one("#check-result")
    assert len(card.select(".check-result-row")) == 100
    assert "Всего результатов: 150; показаны первые 100, сначала проблемы" in card.get_text()
    assert "в истории" not in card.get_text()


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
def test_a_broken_result_handler_does_not_strand_the_queue(client, consumers, crash):
    def broken(_job):
        if crash:
            raise ValueError("synthetic handler failure")

    job, _ = check_jobs.submit(topic_id="a", apply=True, lang="ru", perform=broken)
    consumers[0].run()
    finished = _status(client, job["id"])
    assert finished["status"] == "failed"
    assert not check_jobs.has_pending(apply=True)
    assert "не подтвержден" not in finished.get("flash", "")  # never fabricated success
    # Not "TOW restarted or the record expired": the job is here, and it ended in an error.
    assert finished["flash"].startswith("проверка остановилась из-за внутренней ошибки TOW")
    page = client.get(f"/?check_result={job['id']}").text
    assert "Проверка остановилась из-за внутренней ошибки TOW" in page
    assert "Результат недоступен" not in page


NNM = {
    "title": "NNM-Club",
    "url_regex": r"^https?://(?:www\.)?nnmclub\.to/forum/viewtopic\.php\?t=(\d+)",
    "login_hosts": ["https://nnmclub.to"],
    "fetch_hosts": ["https://nnmclub.to"],
    "login_path": "/forum/login.php",
    "topic_path": "/forum/viewtopic.php?t={id}",
    "download_href_regex": r"download\\.php\\?id=(\\d+)",
    "page_download": True,
    "cookie_names": [],
}
WAITING_SPACE = "ждёт места на диске: не хватает 40,5 ГБ; TOW запустит раздачу сам, когда место появится"


def _shown(client, location):
    """What the owner meets after a check: the one message with its kind (also while "Needs
    attention" lists the topic), never a result card, and the sign-in prompt."""
    page = BeautifulSoup(client.get(location).text, "html.parser")
    query = parse_qs(urlsplit(location).query)
    assert not page.select("#check-result")
    (flash,) = page.select("#flash")
    message = (flash.get_text(" ", strip=True), " ".join(flash.get("class")))
    return message, (query.get("credential_topic") or [""])[0]


@pytest.mark.parametrize(
    ("route", "row"),
    [
        ("check", {"ok": False, "error": "nnmclub: no download link on page", "error_class": "tracker_auth"}),
        (
            "check",
            {"ok": False, "status": "waiting_space", "added": True, "error": WAITING_SPACE, "error_class": "disk"},
        ),
        ("check", {"ok": False, "status": "verification_pending", "changed": True}),
        ("check", {"ok": False, "error": "client refused", "error_class": "qbit"}),
        ("check", {"ok": False, "error": "tracker: all hosts failed", "error_class": "tracker"}),
        ("check", {"ok": True, "added": True}),
        ("check", {"ok": True, "status": "skipped", "skipped": "paused"}),
        ("adopt", {"ok": True}),
        ("check", None),  # deleted while the check waited
    ],
)
def test_a_queued_topic_check_says_what_a_check_at_once_says(client, monkeypatch, consumers, route, row):
    monkeypatch.setattr("tow.web.services.load_config", lambda: {"trackers": {"nnmclub": NNM}})
    monkeypatch.setattr("tow.web.services.adopt_topic", lambda *_a, **_kw: None)
    topic = {"id": "t1", "title": "Show A", "url": "https://nnmclub.to/forum/viewtopic.php?t=1234567"}
    if row and row.get("error"):  # what the failed check saved
        topic.update(last_ok=False, last_error=row["error"], last_error_class=row["error_class"])
    save_state({"topics": [topic]})
    busy = [False]

    def check(**_kw):
        if busy[0]:
            busy[0] = False
            raise CheckBusyError("busy")
        return {"results": [{"id": "t1", "title": "Show A", **row}] if row is not None else []}

    monkeypatch.setattr("tow.web.services.run_check", check)
    location = client.post(f"/topics/t1/{route}", follow_redirects=False).headers["location"]
    at_once = _shown(client, location)
    busy[0] = True
    job = _job(client.post(f"/topics/t1/{route}", follow_redirects=False))
    consumers[0].run()
    assert _shown(client, _status(client, job)["redirect"]) == at_once
    assert at_once[0][0].startswith("Show A — ")
    # The topic it is about, named once: the queued one as it was when the check was asked for.
    assert views.check_result_flash(check_jobs.get(job))["subject"] == flash_record(location)["subject"] == "Show A"


@pytest.mark.parametrize("error", [None, SecretStoreError("secret gate"), ValueError("bad state")])
def test_a_manual_preview_writes_nothing_even_when_it_fails(client, monkeypatch, consumers, error):
    # AGENTS.md: a dry run writes no TOW state, history or log - its failures included.
    writes = []
    for name in ("record_check_failure", "log_event", "save_state", "save_secrets"):
        monkeypatch.setattr(f"tow.web.services.{name}", lambda *a, _name=name, **kw: writes.append(_name))

    def check(**kw):
        assert kw["apply"] is False
        if error is not None:
            raise error
        return {"results": [{"id": "a", "title": "Show A", "ok": False, "error": "site refused"}]}

    monkeypatch.setattr("tow.web.services.run_check", check)
    job = _job(client.post("/check", data={"mode": "dry"}, follow_redirects=False))
    consumers[0].run()
    ended = _status(client, job)
    assert ended["status"] == ("done" if error is None else "failed")
    assert ended["flash"]  # the page still says what happened
    assert writes == []


@pytest.mark.parametrize("topic_id", ["", "a"])
def test_a_check_that_ran_but_whose_result_cannot_be_shown_says_so(client, monkeypatch, consumers, topic_id):
    # The check ran and saved what it found: "could not confirm the check result" and a
    # "check did not complete" entry said the opposite.
    events = []
    monkeypatch.setattr("tow.web.services.log_event", lambda kind, **kw: events.append((kind, kw.get("error_class"))))
    monkeypatch.setattr("tow.web.services.run_check", lambda **_kw: {"results": [{"id": "a", "ok": True}]})

    def broken_report(*_a, **_kw):
        raise KeyError("synthetic report failure")

    monkeypatch.setattr("tow.web.check_actions.check_report", broken_report)
    job = check_jobs.submit(topic_id=topic_id, apply=True, lang="ru", perform=check_actions.perform_check)[0]
    consumers[0].run()
    ended = _status(client, job["id"])
    assert ended["status"] == "done"
    assert ended["flash"].startswith("проверка прошла, но TOW не смог показать её результат (KeyError)")
    assert [kind for kind, _ in events] == ["check_report_failed"]


def test_a_result_has_one_address_rendered_from_its_job(client, monkeypatch, consumers):
    # The address the page goes to holds no message token (those last 600 s, the job an hour):
    # it shows the result from the job each time it opens, and following the job writes nothing.
    from tow.web import views

    save_state(
        {
            "topics": [
                {
                    "id": "t1",
                    "title": "Show A",
                    "url": "https://rutor.info/torrent/1234567/show-a",
                    "last_ok": False,
                    "last_error": "refused",
                    "last_error_class": "qbit",
                }
            ]
        }
    )
    busy = [True]

    def check(**kw):
        if kw.get("ids") == ["t1"] and busy[0]:
            busy[0] = False
            raise CheckBusyError("busy")
        if kw.get("ids") == ["t2"]:
            raise CheckBusyError("busy")
        return {"results": [{"id": "t1", "title": "Show A", "ok": False, "error": "refused", "error_class": "qbit"}]}

    monkeypatch.setattr("tow.web.services.run_check", check)
    job = _job(client.post("/topics/t1/check", follow_redirects=False))
    cancelled = _job(client.post("/topics/t2/check", follow_redirects=False))
    client.post(f"/check/{cancelled}/cancel", follow_redirects=False)
    consumers[0].run()
    before = len(views._FLASHES._records)
    for _ in range(3):
        done, gone = _status(client, job), _status(client, cancelled)
    assert len(views._FLASHES._records) == before
    # A topic's check ends in one message: no result card to scroll to.
    assert done["redirect"] == f"/?check_result={job}"
    assert gone["redirect"] == f"/?check_result={cancelled}"
    views._FLASHES.clear()  # every message token has expired meanwhile
    page = BeautifulSoup(client.get(done["redirect"]).text, "html.parser")
    # Shown as after a check at once, also while "Needs attention" shows the topic's error.
    assert done["flash"][1:] in page.select_one("#flash.error").get_text()
    assert page.select_one("#flash .msg-text").get_text().startswith("Show A — ")
    assert not page.select("#check-result")
    other = BeautifulSoup(client.get(gone["redirect"]).text, "html.parser")
    assert "удалена из очереди" in other.select_one("#flash").get_text()
    assert not other.select("#check-result")


@pytest.mark.parametrize("ends", ["done", "failed"])
def test_the_following_page_of_a_finished_check_says_it_finished(client, monkeypatch, consumers, ends):
    # Opened again (reload, Back, no script) after its check ended: the job is there, so not
    # "TOW restarted or the record expired".
    def check(**_kw):
        if ends == "failed":
            raise ValueError("synthetic")
        return {"results": []}

    monkeypatch.setattr("tow.web.services.run_check", check)
    job = _job(client.post("/check", data={"mode": "apply"}, follow_redirects=False))
    consumers[0].run()
    note = BeautifulSoup(client.get(f"/?check_job={job}").text, "html.parser").select_one("#check-activity")
    # The end of "<subject> — <result>", so in lower case (1.32 QA L6: "… — Проверка в очереди").
    assert " — проверка завершена." in note.get_text()
    assert "перезапущен" not in note.get_text()
    assert note.select_one(f'a[href="/?check_result={job}#check-result"]')
    assert not note.select_one("form")  # nothing left to cancel


def test_a_result_is_dated_when_its_check_ended_not_when_it_was_asked_for(client, monkeypatch, consumers, clock):
    from datetime import UTC, datetime

    from tow.web.text import short_time

    def at(seconds):  # the one time rule (1.32): the short form shown, the full time its tooltip
        return short_time(datetime.fromtimestamp(seconds, UTC)).striptags()

    asked = clock.wall

    def check(**_kw):
        clock.wall += 2 * 3600  # it waited and ran for two hours
        return {"results": [{"id": "a", "title": "Show A", "ok": True}]}

    monkeypatch.setattr("tow.web.services.run_check", check)
    job = _job(client.post("/check", data={"mode": "apply"}, follow_redirects=False))
    consumers[0].run()
    # 1.32.1 CR1: the time sits in the title line ("Results · 20:31"), not on a line of its own.
    when = BeautifulSoup(client.get(f"/?check_result={job}").text, "html.parser").select_one("#check-result h2 time")
    assert when.get_text() == at(clock.wall)
    assert at(asked) != at(clock.wall)


def test_a_sign_in_whose_check_must_wait_is_checked_from_the_queue(client, monkeypatch, consumers):
    # "Sign-in saved; press Check in a minute" left the new password unconfirmed until the owner came back.
    monkeypatch.setattr("tow.web.services.load_config", lambda: {"trackers": {"nnmclub": NNM}})
    save_state({"topics": [{"id": "t1", "title": "Show A", "url": "https://nnmclub.to/forum/viewtopic.php?t=1234567"}]})
    secrets = {"trackers": {"nnmclub": {}}}
    monkeypatch.setattr("tow.web.services.load_secrets", lambda: copy.deepcopy(secrets))
    monkeypatch.setattr("tow.web.services.save_secrets", lambda value: secrets.update(copy.deepcopy(value)))
    monkeypatch.setattr("tow.web.services.log_event", lambda *_a, **_kw: None)
    calls = []

    def check(**kw):
        calls.append(kw["ids"])
        if len(calls) == 1:
            raise CheckBusyError("a scheduled check holds the lock")
        return {"results": [{"id": "t1", "title": "Show A", "ok": True}]}

    monkeypatch.setattr("tow.web.services.run_check", check)
    response = client.post(
        "/topics/t1/tracker-login", data={"username": "user", "password": "pass"}, follow_redirects=False
    )
    job = _job(response)
    consumers[0].run()
    assert calls == [["t1"], ["t1"]]
    assert _status(client, job)["status"] == "done"
    assert secrets["trackers"]["nnmclub"] == {"username": "user", "password": "pass"}


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
    assert not check_jobs.has_pending(apply=True)
    # The next request gets a new consumer, which never runs the ended job (a re-add) again.
    second, _ = check_jobs.submit(topic_id="b", apply=True, lang="ru", perform=ended)
    assert len(consumers) == 2
    consumers[1].run()
    assert performed == ["a", "b"]
    assert check_jobs.get(second["id"])["status"] == "failed"  # ended() never reported a result


def test_a_missing_queued_topic_is_not_found_not_changed_into_check_all(client, monkeypatch, consumers):
    # Deleted while its check waited: "not found", as a check at once says - not an invented
    # "skipped" row that counted as a result.
    attempts = []

    def check(**kw):
        attempts.append(kw.get("ids"))
        if len(attempts) == 1:
            raise CheckBusyError("busy")
        return {"results": []}

    monkeypatch.setattr("tow.web.services.run_check", check)
    job = _job(client.post("/topics/deleted/check", follow_redirects=False))
    consumers[0].run()
    result = _status(client, job)
    assert result["status"] == "failed"
    assert attempts == [["deleted"], ["deleted"]]
    assert result["flash"] == "раздача не найдена"
    assert "report" not in check_jobs.get(job)


def test_a_check_of_only_skipped_or_unsaved_rows_is_no_success():
    # A check whose every row was skipped (paused) or not saved (changed meanwhile) ended in a
    # green "done" as if it confirmed something.
    rows = [
        {"id": "a", "title": "Show A", "ok": True, "status": "skipped", "skipped": "paused"},
        {"id": "b", "title": "Show B", "ok": True, "note": "changed meanwhile"},
    ]
    assert check_report({"results": rows}, apply=True)["kind"] == "info"
    assert check_report({"results": [*rows, {"id": "c", "title": "Show C", "ok": True}]}, apply=True)["kind"] == "ok"
    assert check_report({"results": []}, apply=True)["kind"] == "info"


def test_the_same_click_again_says_its_check_started_not_queued(client, consumers):
    # A page that sent its Check again (the answer was lost) got "queued; it will start after the
    # current check" for the very check at the head of the lane.
    import json as _json

    from tow import i18n

    first = check_actions.accept_check(topic_id="a", apply=True, action="click-1")
    again = check_actions.accept_check(topic_id="a", apply=True, action="click-1")
    started = i18n.t("web.check_all.started")

    assert _json.loads(first.body)["message"]["text"] == started
    assert _json.loads(again.body)["message"]["text"] == started
    assert _json.loads(again.body)["job"] == _json.loads(first.body)["job"]
    behind = check_actions.accept_check(topic_id="b", apply=True, action="click-2")
    assert _json.loads(behind.body)["message"]["text"] == i18n.t("web.check_all.queued")


@pytest.mark.parametrize("refused", [False, True])
def test_a_sign_in_checked_from_the_queue_is_confirmed_or_put_back(client, monkeypatch, consumers, refused):
    # The queued check of a new password never said the site was signed in (its other topics kept
    # asking for a sign-in), and a password the site refused stayed saved.
    from tow import i18n

    monkeypatch.setattr("tow.web.services.load_config", lambda: {"trackers": {"nnmclub": NNM}})
    save_state({"topics": [{"id": "t1", "title": "Show A", "url": "https://nnmclub.to/forum/viewtopic.php?t=1234567"}]})
    secrets = {"trackers": {"nnmclub": {"username": "old", "password": "old-pass"}}}
    monkeypatch.setattr("tow.web.services.load_secrets", lambda: copy.deepcopy(secrets))
    monkeypatch.setattr("tow.web.services.save_secrets", lambda value: secrets.update(copy.deepcopy(value)))
    monkeypatch.setattr("tow.web.services.log_event", lambda *_a, **_kw: None)
    signed_in = []
    monkeypatch.setattr("tow.web.services.note_signed_in", signed_in.append)
    calls = []
    row = (
        {"id": "t1", "title": "Show A", "ok": False, "error": "nnmclub: sign-in refused", "error_class": "tracker_auth"}
        if refused
        else {"id": "t1", "title": "Show A", "ok": True}
    )

    def check(**kw):
        calls.append(kw["ids"])
        if len(calls) == 1:
            raise CheckBusyError("a scheduled check holds the lock")
        return {"results": [row]}

    monkeypatch.setattr("tow.web.services.run_check", check)
    job = _job(
        client.post("/topics/t1/tracker-login", data={"username": "user", "password": "pass"}, follow_redirects=False)
    )
    consumers[0].run()
    ended = _status(client, job)
    if refused:
        assert secrets["trackers"]["nnmclub"]["password"] == "old-pass"  # put back
        assert signed_in == []
        assert "credential_topic=t1" in ended["redirect"]
    else:
        assert secrets["trackers"]["nnmclub"]["password"] == "pass"
        assert signed_in == ["nnmclub"]
        assert ended["flash"] == i18n.t("web.login_retry.ok", site="NNM-Club")
    assert job not in check_jobs.FOLLOW_UPS  # no password kept beside a finished job


def test_sign_ins_queued_one_after_another_put_back_the_password_before_the_first(client, monkeypatch, consumers):
    # Working password A, then B and C typed while another check ran: C's follow-up replaced B's
    # with B as its baseline, so a refused C put back the unverified B instead of A.
    monkeypatch.setattr("tow.web.services.load_config", lambda: {"trackers": {"nnmclub": NNM}})
    save_state({"topics": [{"id": "t1", "title": "Show A", "url": "https://nnmclub.to/forum/viewtopic.php?t=1234567"}]})
    secrets = {"trackers": {"nnmclub": {"username": "user", "password": "pass-a"}}}
    monkeypatch.setattr("tow.web.services.load_secrets", lambda: copy.deepcopy(secrets))
    monkeypatch.setattr("tow.web.services.save_secrets", lambda value: secrets.update(copy.deepcopy(value)))
    monkeypatch.setattr("tow.web.services.log_event", lambda *_a, **_kw: None)
    calls = []

    def check(**kw):
        calls.append(kw["ids"])
        if len(calls) == 1:
            raise CheckBusyError("a scheduled check holds the lock")
        refused = {"id": "t1", "title": "Show A", "ok": False, "error": "refused", "error_class": "tracker_auth"}
        return {"results": [refused]}

    monkeypatch.setattr("tow.web.services.run_check", check)
    first = _job(
        client.post("/topics/t1/tracker-login", data={"username": "user", "password": "pass-b"}, follow_redirects=False)
    )
    second = _job(
        client.post("/topics/t1/tracker-login", data={"username": "user", "password": "pass-c"}, follow_redirects=False)
    )
    assert first == second  # one queued check for both
    assert secrets["trackers"]["nnmclub"]["password"] == "pass-c"
    consumers[0].run()
    assert secrets["trackers"]["nnmclub"]["password"] == "pass-a"  # the last one known to work


def _sign_in_site(monkeypatch, password: str = "pass-a") -> dict:
    """One NNM-Club topic and a working password ``password``, the stores in memory."""
    monkeypatch.setattr("tow.web.services.load_config", lambda: {"trackers": {"nnmclub": NNM}})
    save_state({"topics": [{"id": "t1", "title": "Show A", "url": "https://nnmclub.to/forum/viewtopic.php?t=1234567"}]})
    secrets = {"trackers": {"nnmclub": {"username": "user", "password": password}}}
    monkeypatch.setattr("tow.web.services.load_secrets", lambda: copy.deepcopy(secrets))
    monkeypatch.setattr("tow.web.services.save_secrets", lambda value: secrets.update(copy.deepcopy(value)))
    monkeypatch.setattr("tow.web.services.log_event", lambda *_a, **_kw: None)
    monkeypatch.setattr("tow.web.services.record_check_failure", lambda *_a, **_kw: None)
    return secrets


def _sign_in(client, password: str):
    data = {"username": "user", "password": password}
    return client.post("/topics/t1/tracker-login", data=data, follow_redirects=False)


@pytest.mark.parametrize("ends", ["store", "refused"])
def test_a_queued_sign_in_whose_check_fails_puts_the_previous_password_back(client, monkeypatch, consumers, ends):
    """The check after a queued sign-in raised instead of answering: a store that cannot be read
    blocks it, or the site refused the new password. Either way the new one is not left saved."""
    from tow import i18n
    from tow.errors import TowError
    from tow.web.text import t as web_t

    secrets = _sign_in_site(monkeypatch)
    calls = []

    def check(**kw):
        calls.append(kw["ids"])
        if len(calls) == 1:
            raise CheckBusyError("a scheduled check holds the lock")
        if ends == "store":
            raise SecretStoreError("the store cannot be opened")
        raise TowError("mirrors.not_torrent_login", cls="tracker_auth")

    monkeypatch.setattr("tow.web.services.run_check", check)
    job = _job(_sign_in(client, "pass-b"))
    assert secrets["trackers"]["nnmclub"]["password"] == "pass-b"
    consumers[0].run()
    assert secrets["trackers"]["nnmclub"]["password"] == "pass-a"
    ended = check_jobs.get(job)
    assert ended["feedback"]["credential_topic"] == "t1"
    assert ended["kind"] == "err"
    if ends == "store":
        assert ended["flash"] == i18n.t("web.topics.login_blocked")
    else:
        refusal = TowError("mirrors.not_torrent_login", cls="tracker_auth")
        assert ended["flash"] == web_t("web.login_retry.failed", site="NNM-Club", error=refusal)
    assert job not in check_jobs.FOLLOW_UPS


def test_a_follow_up_that_fails_keeps_the_checks_own_words(client, monkeypatch, consumers):
    """What follows a check failed itself: the check's own message stays, and the failure is logged."""
    from tow import i18n

    events = []
    monkeypatch.setattr("tow.web.services.log_event", lambda kind, **kw: events.append(kind))
    monkeypatch.setattr("tow.web.services.record_check_failure", lambda *_a, **_kw: None)

    def blocked(**_kw):
        raise SecretStoreError("the store cannot be opened")

    def broken(_out, _error):
        raise RuntimeError("the follow-up broke")

    monkeypatch.setattr("tow.web.services.run_check", blocked)
    job, _ = check_jobs.submit(
        topic_id="t1", apply=True, lang="ru", perform=check_actions.perform_check, follow_up=broken
    )
    consumers[0].run()
    ended = check_jobs.get(job["id"])
    assert ended["flash"] == i18n.t("web.check.blocked", "ru")
    assert "check_report_failed" in events


@pytest.mark.parametrize("how", ["cancelled", "lock_timeout"])
def test_a_queued_sign_in_that_never_runs_puts_the_previous_password_back(client, monkeypatch, consumers, how):
    """A sign-in waiting for its check was cancelled, or never got the check lock: its password,
    never checked, is not left saved in place of the one that worked - and the owner is told."""
    from tow import i18n

    secrets = _sign_in_site(monkeypatch)
    monkeypatch.setattr("tow.web.services.run_check", lambda **_kw: (_ for _ in ()).throw(CheckBusyError("busy")))
    if how == "lock_timeout":
        monkeypatch.setattr(check_jobs, "LOCK_WAIT_SEC", 0)
    job = _job(_sign_in(client, "pass-b"))
    assert secrets["trackers"]["nnmclub"]["password"] == "pass-b"
    if how == "cancelled":
        assert client.post(f"/check/{job}/cancel", headers={"x-tow-fetch": "1"}).json()["cancelled"] is True
    else:
        consumers[0].run()
    assert secrets["trackers"]["nnmclub"]["password"] == "pass-a"
    message = _status(client, job)
    lang = check_jobs.get(job)["lang"]  # said as its check would have said it
    assert message["flash"] == i18n.translate("web.topics.login_not_checked", lang, site="NNM-Club")
    assert job not in check_jobs.FOLLOW_UPS
    assert not check_jobs.UNCHECKED


@pytest.mark.parametrize("how", ["cancelled", "lock_timeout", "refused"])
def test_a_password_is_put_back_without_holding_the_queues_lock(client, monkeypatch, consumers, how):
    """Putting a password back writes the stores (their lock): never while the queue's lock is held."""
    import threading

    from tow.web import routes_topic_login

    _sign_in_site(monkeypatch)
    free = []

    def take_and_give_back():
        taken = check_jobs.LOCK.acquire(timeout=2)
        free.append(taken)
        if taken:
            check_jobs.LOCK.release()  # an RLock: released by the thread that took it

    def restore(*_args):
        probe = threading.Thread(target=take_and_give_back)
        probe.start()
        probe.join(timeout=10)
        return True

    monkeypatch.setattr(routes_topic_login, "restore_unverified_password", restore)
    calls = []

    def check(**_kw):
        calls.append(1)
        if how != "refused" or len(calls) == 1:
            raise CheckBusyError("busy")
        return {"results": [{"id": "t1", "title": "Show A", "ok": False, "error": "x", "error_class": "tracker_auth"}]}

    monkeypatch.setattr("tow.web.services.run_check", check)
    if how == "lock_timeout":
        monkeypatch.setattr(check_jobs, "LOCK_WAIT_SEC", 0)
    job = _job(_sign_in(client, "pass-b"))
    if how == "cancelled":
        client.post(f"/check/{job}/cancel", headers={"x-tow-fetch": "1"})
    else:
        consumers[0].run()
    assert free == [True]


def test_a_sign_in_the_full_queue_refuses_puts_the_password_back_and_says_so(client, monkeypatch, consumers):
    """The queue was full: the sign-in's check never ran, and the owner heard only "queue full"."""
    from helpers import flash_of

    from tow import i18n

    secrets = _sign_in_site(monkeypatch)
    monkeypatch.setattr("tow.web.services.run_check", lambda **_kw: (_ for _ in ()).throw(CheckBusyError("busy")))
    monkeypatch.setattr(check_jobs, "MAX_PENDING", 0)
    response = _sign_in(client, "pass-b")
    assert secrets["trackers"]["nnmclub"]["password"] == "pass-a"
    said = flash_of(response.headers["location"])
    assert i18n.translate("web.check_all.queue_full", "en") in said
    assert i18n.translate("web.topics.login_not_checked", "en", site="NNM-Club") in said
    assert not check_jobs.UNCHECKED


def test_unchecked_sign_ins_share_one_baseline_by_generation(monkeypatch):
    """The password to put back is the one saved before the first unchecked sign-in of a site; a
    confirmed sign-in starts a new generation with its own password; a sign-in no longer in the
    current generation changes nothing; the table empties as sign-ins end and stays bounded."""
    from types import SimpleNamespace

    from tow.web.routes_topic_login import _SignIn

    site = SimpleNamespace(name="nnmclub", spec={"title": "NNM-Club"})
    entry = {"pass-a": {"username": "user", "password": "pass-a"}}
    entry["pass-b"] = {"username": "user", "password": "pass-b"}
    b = _SignIn("t1", site, entry["pass-a"], "user", "pass-b")
    c = _SignIn("t1", site, entry["pass-b"], "user", "pass-c")
    assert c.baseline() == entry["pass-a"]  # what was saved before the first, not the unchecked B
    b.settle(confirmed=True)  # B worked: the next generation puts B back
    assert c.baseline() == entry["pass-b"]
    d = _SignIn("t1", site, {"username": "user", "password": "pass-c"}, "user", "pass-d")
    assert d.baseline() == entry["pass-b"]
    b.settle()  # no longer a member: it neither clears nor changes the generation
    assert check_jobs.UNCHECKED["nnmclub"]["members"] == [c, d]
    c.settle()
    d.settle()
    assert check_jobs.UNCHECKED == {}

    # Bounded: a generation outlives no job (it expires) and the table holds at most its limit.
    clock = [1000.0]
    monkeypatch.setattr(check_jobs.time, "monotonic", lambda: clock[0])
    stale = _SignIn("t1", site, entry["pass-a"], "user", "pass-x")
    clock[0] += check_jobs.UNCHECKED_TTL + 1
    fresh = _SignIn("t1", site, entry["pass-b"], "user", "pass-y")
    assert fresh.baseline() == entry["pass-b"]
    assert stale.baseline() == entry["pass-a"]  # its own, the generation gone
    monkeypatch.setattr(check_jobs, "MAX_UNCHECKED", 3)
    for n in range(5):
        _SignIn("t1", SimpleNamespace(name=f"site{n}", spec={}), None, "user", "pass")
    assert len(check_jobs.UNCHECKED) == 3


def test_a_sign_in_typed_while_the_last_ones_check_runs_puts_back_the_password_before_both(
    client, monkeypatch, consumers
):
    """A works; B is typed and its check already runs when C is typed (a check of its own). Both
    refused: what comes back is A, not B - a password the site had already refused."""
    secrets = _sign_in_site(monkeypatch)
    refused = {"id": "t1", "title": "Show A", "ok": False, "error": "refused", "error_class": "tracker_auth"}
    seen = {}

    def check(**_kw):
        if not seen:
            seen["first"] = True
            raise CheckBusyError("a scheduled check holds the lock")
        if "c" not in seen:  # B's check runs: C is typed now
            seen["c"] = _job(_sign_in(client, "pass-c"))
        return {"results": [refused]}

    monkeypatch.setattr("tow.web.services.run_check", check)
    first = _job(_sign_in(client, "pass-b"))
    consumers[0].run()
    assert seen["c"] != first
    assert secrets["trackers"]["nnmclub"]["password"] == "pass-a"
    assert not check_jobs.UNCHECKED


def test_a_sign_in_typed_while_a_refused_one_is_put_back_waits_for_it(monkeypatch):
    """A works; B is refused, and C is typed while B's password is being put back. B had left
    its generation before it put A back: C started a new one with the refused B as its baseline,
    B found C saved and put nothing back, and a refused C brought back B. Now C waits for B's
    restore (the stores' lock): its baseline is A."""
    import threading
    from types import SimpleNamespace

    from tow.web import routes_topic_login, services
    from tow.web.routes_topic_login import _SignIn
    from tow.web.site_store import save_tracker_login

    secrets = _sign_in_site(monkeypatch)
    site = SimpleNamespace(name="nnmclub", spec={"title": "NNM-Club"})

    def sign_in(password: str) -> _SignIn:  # what the sign-in route does under the stores' lock
        with services.persistence_lock():
            old = copy.deepcopy(services.load_secrets())
            save_tracker_login("nnmclub", "user", password, base=old)
            return _SignIn("t1", site, copy.deepcopy(old["trackers"]["nnmclub"]), "user", password)

    b = sign_in("pass-b")
    typed: dict = {}
    real_restore = routes_topic_login.restore_unverified_password

    def restore(*args):
        if not typed:  # B's restore: C is typed now, from another request
            typed["thread"] = threading.Thread(target=lambda: typed.setdefault("c", sign_in("pass-c")))
            typed["thread"].start()
            typed["thread"].join(timeout=1)  # saved at once, unless it waits for this restore
        return real_restore(*args)

    monkeypatch.setattr(routes_topic_login, "restore_unverified_password", restore)
    assert b.restore() is True  # A back
    typed["thread"].join(timeout=10)
    assert secrets["trackers"]["nnmclub"]["password"] == "pass-c"
    assert typed["c"].restore() is True  # C refused too
    assert secrets["trackers"]["nnmclub"]["password"] == "pass-a"
    assert not check_jobs.UNCHECKED


@pytest.mark.parametrize("ends", ["added", "crash"])
def test_a_new_topics_first_check_from_the_queue_says_what_a_check_at_once_says(client, monkeypatch, consumers, ends):
    # Queued behind another check, a new topic's first check lost "topic added; ..." and a crash of
    # it was neither logged as the add's failed check nor sent to the messengers.
    from tow import i18n

    monkeypatch.setattr("tow.web.services.guess_topic_title", lambda _url: "")
    notified, events = [], []
    monkeypatch.setattr("tow.web.services.notify_topic_error", lambda topic, error, **_kw: notified.append(error))
    real_log = views.services.log_event
    monkeypatch.setattr("tow.web.services.log_event", lambda kind, **kw: events.append(kind) or real_log(kind, **kw))
    calls = []

    def check(**kw):
        calls.append(kw["ids"])
        if len(calls) == 1:
            raise CheckBusyError("a scheduled check holds the lock")
        if ends == "crash":
            raise RuntimeError("storage exploded")
        return {"results": [{"id": kw["ids"][0], "title": "Show", "ok": True, "added": True}]}

    monkeypatch.setattr("tow.web.services.run_check", check)
    response = client.post(
        "/topics/add",
        data={"url": "http://rutor.info/torrent/101/show", "title": "Show", "save_path": r"M:\anime"},
        follow_redirects=False,
    )
    job = _job(response)
    consumers[0].run()
    ended = _status(client, job)
    if ends == "added":
        assert ended["flash"] == i18n.t("web.topics.added_confirmed")
        assert notified == []
    else:
        assert ended["flash"] == i18n.t("web.topics.added_check_failed")
        assert "add_check_fail" in events
        assert [str(error) for error in notified] == ["storage exploded"]


def test_a_direct_row_check_that_crashes_says_so_instead_of_a_500(client, monkeypatch):
    # Without the page's script a row's Check ran at once; a store or transaction failure there
    # answered a bare HTTP 500 and logged nothing, unlike the same failure of a queued check.
    from tow.check_transaction import CheckTransactionError

    events = []
    monkeypatch.setattr("tow.web.services.log_event", lambda kind, **kw: events.append((kind, kw.get("reason"))))

    def crash(**_kw):
        raise CheckTransactionError("synthetic journal failure")

    monkeypatch.setattr("tow.web.services.run_check", crash)
    response = client.post("/topics/t1/check", follow_redirects=False)
    assert response.status_code == 303
    assert "CheckTransactionError" in flash_of(response.headers["location"])
    assert events == [("check_blocked", "result_unconfirmed")]


def test_the_action_ids_kept_are_bounded(consumers):
    # Every click on a waiting check registered its action id for as long as the check lived:
    # a page clicking again and again grew the table without bound.
    for n in range(check_jobs.MAX_ACTIONS + 50):
        check_jobs.submit(topic_id="a", apply=True, lang="ru", perform=lambda _job: None, action=f"click-{n}")
    assert len(check_jobs.ACTIONS) == check_jobs.MAX_ACTIONS
    assert f"click-{check_jobs.MAX_ACTIONS + 49}" in check_jobs.ACTIONS  # the newest are kept
    assert "click-0" not in check_jobs.ACTIONS


def test_a_waiting_checks_words_follow_its_subject_in_lower_case():
    """1.32 QA L6: "Северное сияние — Проверка в очереди: позиция 1." had a capital after the dash
    (a finished one said "— новая версия"). The words are the end of "<subject> — <result>"; on
    their own (Check all) they are said as a sentence."""
    from tow.web.messages import message_text
    from tow.web.views import check_progress

    queued = check_progress({"status": "queued", "position": 1})
    assert queued.startswith("проверка в очереди: позиция 1.")
    assert message_text({"subject": "Северное сияние", "text": queued}).startswith(
        "Северное сияние — проверка в очереди"
    )
    assert message_text({"subject": "", "text": queued}).startswith("Проверка в очереди")
