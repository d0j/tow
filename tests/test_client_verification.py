"""Slow client verification, read-back races and crash-safe, quota-free finalization."""

from __future__ import annotations

import copy
from types import SimpleNamespace

import pytest
from test_cov_managed_selection import E01, TORRENT, FakeClient, H

from tow import check
from tow.check import apply as apply_step
from tow.check import run as check_run
from tow.check import space, verification
from tow.check import topic as topic_step
from tow.clients import factory
from tow.clients.managed import ClientError, ClientVerificationPending
from tow.notify import NotificationBatch
from tow.progress import reconcile_topic
from tow.progress.summary import progress_summary
from tow.store import load_download_history, load_state, save_download_history, save_state
from tow.web.views import topic_progress_summary


class VerifyingClient(FakeClient):
    client_id = "main"
    client_kind = "fake"
    START_POLLS = 3

    def __init__(self):
        super().__init__()
        self.checking = True

    def _start(self, infohash):
        super()._start(infohash)
        if self.checking:
            self.torrents[infohash.upper()]["state"] = "checkingDL"
            self.torrents[infohash.upper()]["progress"] = 1.0


def _pending(client, path="D:/tv"):
    with pytest.raises(ClientVerificationPending) as error:
        client.add_torrent_selected(TORRENT, path, H, [E01])
    assert error.value.receipt
    return error.value.receipt


def test_timeout_keeps_verified_selection_and_does_not_stop_the_check():
    client = VerifyingClient()
    receipt = _pending(client)
    assert client.torrents[H]["state"] == "checkingDL"
    assert client.wanted_torrent_indices() == {E01}
    assert client.torrents[H]["tags"] == ["tow", "tow-pending"]
    assert [call[0] for call in client.calls] == ["add", "set_wanted", "start"]
    assert receipt["hash"] == H


def test_retry_during_check_is_read_only_and_keeps_the_same_task():
    client = VerifyingClient()
    _pending(client)
    client.calls.clear()
    with pytest.raises(ClientVerificationPending) as error:
        client.configure_torrent_selection(TORRENT, H, [E01], ensure_started=True)
    assert error.value.receipt
    assert client.calls == []
    assert len(client.torrents) == 1


def test_matching_pending_check_does_not_enter_a_blocking_client_preparation(monkeypatch):
    client = VerifyingClient()
    _pending(client)
    monkeypatch.setattr(client, "_ready_for_selection", lambda _info: pytest.fail("verification was restarted"))
    with pytest.raises(ClientVerificationPending):
        client.configure_torrent_selection(TORRENT, H, [E01], ensure_started=True)


def test_unknown_state_never_releases_the_pending_label():
    client = VerifyingClient()
    receipt = _pending(client)
    client.torrents[H]["state"] = "allocating"
    client.calls.clear()
    with pytest.raises(ClientError):
        client.finish_verification(H, "D:/tv", receipt)
    assert client.calls == []
    assert "tow-pending" in client.torrents[H]["tags"]


@pytest.mark.parametrize(
    "malformed",
    ["bad", [1], True, {"files": [None], "wanted": [0]}, {"files": [], "wanted": [0]}, {"files": [], "wanted": [True]}],
)
def test_malformed_receipt_cannot_mutate_any_client_task(malformed):
    client = VerifyingClient()
    _pending(client)
    client.calls.clear()
    with pytest.raises(ClientError) as error:
        client.finish_verification(H, "D:/tv", malformed, start_stopped=True)
    assert error.value.code == "client.managed.wrong_selection"
    assert client.calls == []


def test_slow_checking_read_at_the_deadline_is_not_a_failed_start(monkeypatch):
    client = VerifyingClient()
    client.seed(tags=["tow", "tow-pending"], state="checkingDL")
    now = {"value": 0.0}
    real_inspect = client.inspect_torrent

    def inspect(h):
        now["value"] = 61.0
        return real_inspect(h)

    monkeypatch.setattr(client, "inspect_torrent", inspect)
    monkeypatch.setattr("tow.clients.managed.time.monotonic", lambda: now["value"])
    with pytest.raises(ClientVerificationPending):
        client._wait_started(H)
    assert client.calls == []


def test_selection_change_timeout_never_rolls_back_a_verified_selection():
    client = VerifyingClient()
    client.seed(tags=["tow"], state="downloading", wanted={1})
    with pytest.raises(ClientVerificationPending) as error:
        client.configure_torrent_selection(TORRENT, H, [E01])
    assert error.value.receipt
    assert [call[0] for call in client.calls] == ["stop", "set_wanted", "start"]
    assert client.wanted_torrent_indices() == {E01}


@pytest.mark.parametrize("state", ["downloading", "stalledDL", "queuedDL", "uploading", "stoppedUP"])
def test_completion_only_releases_the_pending_label(state):
    client = VerifyingClient()
    receipt = _pending(client)
    client.torrents[H]["state"] = state
    client.calls.clear()
    info = client.finish_verification(H, "D:/tv", receipt)
    assert info["tags"] == ["tow"]
    assert [call[0] for call in client.calls] == ["labels"]


def test_changed_selection_after_our_resume_is_stopped_not_left_downloading(monkeypatch):
    client = VerifyingClient()
    receipt = _pending(client)
    client.torrents[H].update(state="stoppedDL", progress=0.3)
    client.checking = False
    start = client._start

    def raced(h):
        start(h)
        client.torrents[H]["files"][0]["priority"] = 0

    monkeypatch.setattr(client, "_start", raced)
    client.calls.clear()
    with pytest.raises(ClientError) as error:
        client.finish_verification(H, "D:/tv", receipt, start_stopped=True)
    assert error.value.code == "client.managed.wrong_selection"
    assert client.calls == [("start",), ("stop",)]


@pytest.mark.parametrize(
    ("change", "code"),
    [
        ("hash", "hash_changed_selection"),
        ("owner", "not_owned"),
        ("path", "wrong_folder"),
        ("priority", "wrong_selection"),
        ("size", "wrong_selection"),
        ("name", "wrong_selection"),
        ("missing", "missing"),
        ("error", "error_state"),
        ("moving", "start_unconfirmed"),
    ],
)
def test_changed_evidence_cannot_release_or_start_a_task(change, code):
    client = VerifyingClient()
    receipt = _pending(client)
    row = client.torrents[H]
    row["state"] = "downloading"
    if change == "hash":
        row["hash"] = "F" * 40
    elif change == "owner":
        row["tags"] = ["tow-pending"]
    elif change == "path":
        row["save_path"] = "E:/elsewhere"
    elif change in {"priority", "size", "name"}:
        row["files"][0][change] = 0 if change == "priority" else (999 if change == "size" else "different.mkv")
    elif change == "missing":
        client.torrents.clear()
    else:
        row["state"] = change
    client.calls.clear()
    with pytest.raises(ClientError) as error:
        client.finish_verification(H, "D:/tv", receipt, start_stopped=True)
    assert error.value.code == f"client.managed.{code}"
    assert client.calls == []


def test_readback_change_at_timeout_is_a_real_failure_not_a_pending_receipt(monkeypatch):
    client = VerifyingClient()
    original = client._wait_started

    def changed(infohash):
        try:
            return original(infohash)
        finally:
            client.torrents[H]["files"][0]["priority"] = 0

    monkeypatch.setattr(client, "_wait_started", changed)
    with pytest.raises(ClientError) as error:
        client.add_torrent_selected(TORRENT, "D:/tv", H, [E01])
    assert error.value.code == "client.managed.wrong_selection"
    assert client.calls[-1] == ("stop",)


class Tracker:
    name = "fake"

    def __init__(self):
        self.fetches = 0

    def fetch_torrent(self, *args, **kwargs):
        self.fetches += 1
        return TORRENT

    def fetch_title(self, *args, **kwargs):
        return "Show S01"


@pytest.fixture
def world(monkeypatch, tmp_path):
    client, tracker, sent = VerifyingClient(), Tracker(), []
    cfg = {"trackers": {}, "client": {"id": "main", "kind": "fake"}}
    monkeypatch.setattr(check_run, "load_config", lambda: cfg)
    monkeypatch.setattr(check_run, "load_trackers", lambda _cfg: {"fake": tracker})
    monkeypatch.setattr(topic_step, "match_tracker", lambda *_args: tracker)
    monkeypatch.setattr(factory, "default_client_id", lambda _cfg: "main")
    monkeypatch.setattr(factory, "from_secrets", lambda *_args, **_kwargs: client)
    monkeypatch.setattr(apply_step, "free_space_problem", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(NotificationBatch, "queue", lambda self, topic, *, kind, **kwargs: sent.append(kind))
    save_download_history({"schema_version": 1, "topics": {}})
    save_state(
        {
            "topics": [
                {
                    "id": "slow",
                    "title": "Show S01",
                    "url": "https://tracker.example/topic/1",
                    "save_path": str(tmp_path / "tv"),
                    "client_id": "main",
                    "tracking_mode": "once",
                    "selection": {"mode": "files", "value": "*e01.mkv"},
                    "hash": "A" * 40,
                }
            ]
        }
    )
    return client, tracker, sent


def _run(**kwargs):
    return check.run_check(apply=True, notify=True, how="test", **kwargs)["results"][0]


def test_deferred_revision_is_saved_but_never_reported_as_success(world):
    client, tracker, sent = world
    row = _run()
    topic = load_state()["topics"][0]
    assert row["status"] == "verification_pending"
    assert row["ok"] is False
    assert row["added"] is False
    assert topic["hash"] == H
    assert topic["previous_hashes"] == ["A" * 40]
    assert topic["selection_verified"] is True
    assert topic["once_done"] is False
    assert topic["verification_pending"]["hash"] == H
    assert not topic["last_error"]
    assert not topic["last_ok"]
    assert sent == []
    assert tracker.fetches == 1
    assert client.torrents[H]["state"] == "checkingDL"


@pytest.mark.parametrize("scope", [{}, {"progress_only": True}, {"space_only": True}])
def test_next_pass_finishes_without_tracker_fetch_or_reconfiguration(world, scope):
    client, tracker, sent = world
    _run()
    before_check = load_state()["topics"][0]["last_check"]
    disk = load_state()
    disk["mirrors"] = {"fake": {"frozen": True}}
    save_state(disk)
    client.torrents[H]["state"] = "downloading"
    client.torrents[H]["progress"] = 0.4
    client.calls.clear()
    row = _run(**scope)
    topic = load_state()["topics"][0]
    assert row["ok"]
    assert row["added"]
    assert topic["once_done"]
    assert "verification_pending" not in topic
    assert client.torrents[H]["tags"] == ["tow"]
    assert [call[0] for call in client.calls] == ["labels"]
    assert tracker.fetches == 1
    assert sent == ["updated"]
    if scope:
        assert topic["last_check"] == before_check
    check.run_check(apply=True, notify=True, how="test", **scope)
    assert sent == ["updated"]


def test_still_checking_pass_is_read_only_nonblocking_and_not_a_success(world):
    client, tracker, sent = world
    _run()
    client.calls.clear()
    row = _run(space_only=True)
    assert row["status"] == "verification_pending"
    assert row["ok"] is False
    assert client.calls == []
    assert tracker.fetches == 1
    assert sent == []


def test_stopped_after_check_is_retried_once_not_forever(world):
    client, tracker, _sent = world
    _run()
    client.checking = False
    client.start_works = False
    client.torrents[H].update(state="stoppedDL", progress=0.3)
    client.calls.clear()
    first = _run(space_only=True)
    assert first["ok"] is False
    starts = len(client.calls)
    second = _run(progress_only=True)
    assert second["ok"] is False
    assert len(client.calls) == starts
    assert load_state()["topics"][0]["verification_pending"]["resume_attempted"] is True
    assert tracker.fetches == 1


def test_preflight_transport_failure_does_not_consume_the_resume_attempt(world, monkeypatch):
    client, _tracker, _sent = world
    _run()
    client.torrents[H].update(state="stoppedDL", progress=0.3)
    require_owned = client._require_owned

    def unavailable(_h):
        raise ClientError("client.transmission.no_connection")

    monkeypatch.setattr(client, "_require_owned", unavailable)
    _run(space_only=True)
    assert not load_state()["topics"][0]["verification_pending"].get("resume_attempted")
    monkeypatch.setattr(client, "_require_owned", require_owned)
    client.checking = False
    assert _run(space_only=True)["ok"]


def test_blocked_unchanged_revision_is_not_green_but_site_is_still_checked(world):
    client, tracker, _sent = world
    _run()
    client.torrents[H].update(state="stoppedDL", progress=0.3, tags=["tow"])
    _run(space_only=True)
    client.calls.clear()
    row = _run()
    assert not row["ok"]
    assert tracker.fetches == 2
    assert not load_state()["topics"][0]["once_done"]
    assert client.calls == []


def test_owner_explicit_check_can_retry_a_stopped_unconfirmed_start(world):
    client, _tracker, _sent = world
    _run()
    client.checking = False
    client.start_works = False
    client.torrents[H].update(state="stoppedDL", progress=0.3)
    _run(space_only=True)
    client.start_works = True
    row = check.run_check(apply=True, notify=False, how="manual", ids=["slow"])["results"][0]
    assert row["ok"]
    assert "verification_pending" not in load_state()["topics"][0]


def test_nonpending_selection_change_does_not_override_client_pause(world):
    client, _tracker, _sent = world
    _run()
    client.torrents[H].update(state="stoppedDL", progress=0.3, tags=["tow"])
    client.calls.clear()
    row = _run(space_only=True)
    assert not row["ok"]
    assert client.calls == []


def test_removed_task_is_not_readded_by_background_but_manual_check_can_readd(world):
    client, tracker, _sent = world
    _run()
    client.torrents.clear()
    client.calls.clear()
    row = _run(space_only=True)
    assert not row["ok"]
    assert client.calls == []
    assert tracker.fetches == 1
    row = check.run_check(apply=True, notify=False, how="manual", ids=["slow"])["results"][0]
    assert row["status"] == "verification_pending"
    assert tracker.fetches == 2
    assert client.calls[0][0] == "add"


def test_manual_readd_finishes_as_added_not_selection_updated(world):
    client, _tracker, sent = world
    _run()
    client.torrents.clear()
    check.run_check(apply=True, notify=True, how="manual", ids=["slow"])
    client.torrents[H].update(state="downloading", progress=0.5)
    row = _run(space_only=True)
    assert row["added"]
    assert sent == ["added"]


def test_terminal_failure_is_not_scheduled_every_five_minutes(world):
    client, _tracker, _sent = world
    _run()
    client.torrents[H]["tags"] = ["tow-pending"]
    _run(space_only=True)
    disk = load_state()
    assert disk["topics"][0]["verification_pending"]["blocked"]
    assert verification.waiting_topics(disk) == []
    assert check_run._wanted_ids(disk, None, "", space_only=True) == set()


def test_failed_manual_repair_does_not_queue_an_automatic_priority_overwrite(world):
    client, tracker, _sent = world
    _run()
    client.torrents[H].update(state="downloading", progress=0.5)
    client.torrents[H]["files"][0]["priority"] = 0
    _run(space_only=True)
    disk = load_state()
    pending = copy.deepcopy(disk["topics"][0]["verification_pending"])
    disk["mirrors"] = {"fake": {"frozen": True}}
    save_state(disk)
    client.calls.clear()
    check.run_check(apply=True, notify=False, how="manual", ids=["slow"])
    disk = load_state()
    assert disk["topics"][0]["verification_pending"] == pending
    assert not disk["topics"][0]["selection_dirty"]
    disk["mirrors"] = {}
    save_state(disk)
    _run()
    assert client.calls == []
    assert tracker.fetches == 2


def test_successful_manual_repair_preserves_the_original_added_notification(world):
    client, _tracker, sent = world
    _run()
    client.torrents[H].update(state="downloading", progress=0.5)
    client.torrents[H]["files"][0]["priority"] = 0
    _run(space_only=True)
    client.checking = False
    row = check.run_check(apply=True, notify=True, how="manual", ids=["slow"])["results"][0]
    assert row["ok"]
    assert row["added"]
    assert sent == ["error", "updated", "recovered"]
    assert "verification_pending" not in load_state()["topics"][0]


def test_redeferred_manual_repair_retains_space_recovery_intent(world):
    client, _tracker, _sent = world
    _run()
    disk = load_state()
    disk["topics"][0]["verification_pending"].update(completion_event="client_started", resume_after_check=True)
    save_state(disk)
    client.torrents[H].update(state="downloading", progress=0.5)
    client.torrents[H]["files"][0]["priority"] = 0
    _run(space_only=True)
    row = check.run_check(apply=True, notify=False, how="manual", ids=["slow"])["results"][0]
    assert row["status"] == "verification_pending"
    pending = load_state()["topics"][0]["verification_pending"]
    assert pending["kind"] == "updated"
    assert pending["completion_event"] == "client_started"
    assert pending["resume_after_check"]


def test_space_wait_start_can_defer_verification_without_false_disk_error(world, monkeypatch):
    client, tracker, sent = world
    folder = load_state()["topics"][0]["save_path"]
    full = space.Shortfall(needed=1024**3, free=0, path=folder)
    monkeypatch.setattr(apply_step, "free_space_problem", lambda *args, **kwargs: full)
    first = _run()
    assert first["status"] == "waiting_space"
    monkeypatch.setattr(space, "_measured", lambda *args, **kwargs: (True, None))
    client.calls.clear()
    second = _run(space_only=True)
    topic = load_state()["topics"][0]
    assert second["status"] == "verification_pending"
    assert not topic.get("waiting_space")
    assert not topic["last_error"]
    assert not topic["once_done"]
    assert client.torrents[H]["state"] == "checkingDL"
    assert [call[0] for call in client.calls] == ["start"]
    client.torrents[H].update(state="downloading", progress=0.5)
    third = _run(space_only=True)
    assert third["ok"]
    assert load_state()["topics"][0]["once_done"]
    assert tracker.fetches == 1
    assert "updated" in sent


@pytest.mark.parametrize("change", ["paused", "selection_dirty", "selection", "save_path", "client_id"])
def test_owner_changes_prevent_background_confirmation(world, change):
    client, tracker, _sent = world
    _run()
    disk = load_state()
    topic = disk["topics"][0]
    topic[change] = True if change in {"paused", "selection_dirty"} else ({} if change == "selection" else "changed")
    save_state(disk)
    client.torrents[H]["state"] = "downloading"
    client.calls.clear()
    check.run_check(apply=True, notify=False, how="test", space_only=True)
    assert client.calls == []
    assert tracker.fetches == 1


@pytest.mark.parametrize("scope", [{}, {"progress_only": True}, {"space_only": True}])
def test_an_unexpected_verification_failure_is_the_topics_result_not_the_runs_end(world, monkeypatch, scope):
    client, _tracker, _sent = world
    _run()
    disk = load_state()
    other = {**copy.deepcopy(disk["topics"][0]), "id": "other", "url": "https://tracker.example/topic/2"}
    other.pop("verification_pending")
    disk["topics"].append(other)
    save_state(disk)
    client.torrents[H]["state"] = "downloading"

    def broken(topic, *_args, **_kwargs):
        raise RuntimeError("synthetic defect")

    monkeypatch.setattr(verification, "withdrawn_meanwhile", broken)
    results = check.run_check(apply=True, notify=False, how="test", **scope)["results"]
    row = next(item for item in results if item["id"] == "slow")
    assert row["ok"] is False
    assert row["status"] == "failed"
    assert any(item["id"] == "other" for item in results) or scope.get("space_only")
    topic = load_state()["topics"][0]
    assert topic["verification_pending"]["hash"] == H  # the receipt is kept for the next pass
    assert topic["last_error"]


def test_preview_changes_nothing_in_client_state_history_or_marker(world):
    client, tracker, _sent = world
    _run()
    before, history = load_state(), load_download_history()
    client.torrents[H]["state"] = "downloading"
    client.calls.clear()
    check.run_check(apply=False, notify=False, how="test")
    assert load_state() == before
    assert load_download_history() == history
    assert client.calls == []
    assert tracker.fetches == 1


def test_checking_progress_one_cannot_emit_files_or_completion_events(tmp_path):
    client = VerifyingClient()
    _pending(client, str(tmp_path))
    topic = {"id": "slow", "hash": H, "save_path": str(tmp_path), "title": "Show S01 [1 of 1]"}
    history = {"topics": {}}
    result = reconcile_topic(topic, client, history, "2026-10-14T12:00:00+03:00")
    assert history["topics"]["slow"]["items"] == {}
    assert not set(result["events"]) & {"episode_completed", "file_completed", "new_file"}
    assert result["summary"]["verifying"]
    assert not result["summary"]["is_complete"]
    assert progress_summary("slow", history)["verifying"]


def test_absent_client_task_is_not_displayed_as_checking(tmp_path):
    client = VerifyingClient()
    topic = {"id": "slow", "hash": H, "save_path": str(tmp_path), "verification_pending": {"hash": H}}
    history = {"topics": {}}
    result = reconcile_topic(topic, client, history, "2026-10-14T12:00:00+03:00")
    assert not result["summary"].get("verifying")


def test_verification_does_not_rearm_an_already_complete_season():
    from tow.check.reconcile import _note_season_complete

    record = {"season_complete_at": "2026-10-13T12:00:00+03:00"}
    _note_season_complete({}, {}, None, record, {"summary": {"verifying": True, "is_complete": False}})
    assert record["season_complete_at"] == "2026-10-13T12:00:00+03:00"


def test_web_and_cli_show_pending_as_a_phase_not_as_an_error():
    from tow.cli.render import human
    from tow.web.views import manual_check_flash

    row = {"id": "slow", "title": "Show", "ok": False, "status": "verification_pending", "skipped": "Checking files"}
    response = manual_check_flash(row, topic_id="slow")
    assert response is not None
    assert response.status_code == 303
    output = human({"results": [row]})
    assert "ERR" not in output
    assert "Checking files" in output


def test_home_html_and_history_api_show_pending_without_complete_badge(world):
    from fastapi.testclient import TestClient

    from tow.web import app

    _run()
    browser = TestClient(app, headers={"Origin": "http://127.0.0.1"})
    page = browser.get("/")
    assert page.status_code == 200
    assert "Проверка файлов" in page.text
    data = browser.get("/topics/slow/downloads.json").json()
    assert data["summary"]["verifying"]
    assert not data["summary"]["is_complete"]


def test_pending_marker_hides_stale_complete_history_in_home(world):
    _run()
    topic = load_state()["topics"][0]
    summary = topic_progress_summary(topic, {"topics": {}})
    assert summary["verifying"]
    assert not summary["is_complete"]


def test_receipt_is_detached_from_live_state_and_exception():
    client = VerifyingClient()
    receipt = _pending(client)
    saved = copy.deepcopy(receipt)
    client.torrents[H]["files"][0]["name"] = "changed"
    assert receipt == saved


def test_scheduler_selects_only_unpaused_clean_pending_receipts():
    topics = [
        {"id": "a", "verification_pending": {"hash": H}},
        {"id": "b", "verification_pending": {"hash": H}, "paused": True},
        {"id": "c", "verification_pending": {"hash": H}, "selection_dirty": True},
    ]
    assert [topic["id"] for topic in verification.waiting_topics({"topics": topics})] == ["a"]


def test_malformed_marker_does_not_crash_scheduler_or_home():
    topic = {"id": "broken", "verification_pending": [True], "hash": H}
    assert verification.waiting_topics({"topics": [topic]}) == []
    assert not topic_progress_summary(topic, {"topics": {}}).get("verifying")


def test_blocked_handover_cannot_be_confirmed_by_matching_magnet():
    topic = {"hash": H, "verification_pending": {"blocked": "client.managed.wrong_selection"}, "once_done": False}
    assert not topic_step._confirm_matching_magnet(topic, None, None, "", H, {}, None, {}, RuntimeError())
    assert topic["once_done"] is False


def test_blocked_handover_cannot_be_confirmed_by_the_optional_hash_probe():
    topic = {"hash": H, "verification_pending": {"blocked": "client.managed.start_unconfirmed"}, "once_done": False}
    work = SimpleNamespace(
        topic=topic,
        old=H,
        client=VerifyingClient(),
        run=SimpleNamespace(apply=True),
        tracker=SimpleNamespace(spec={"infohash_path": "/hash"}),
    )
    assert not topic_step._probe_unchanged_revision(work, {"mode": "all", "tracking_mode": "once"})
    assert topic["once_done"] is False
