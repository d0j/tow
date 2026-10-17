"""Slow client verification, read-back races and crash-safe, quota-free finalization."""

from __future__ import annotations

import copy
from types import SimpleNamespace

import pytest
from helpers import make_torrent
from test_cov_managed_selection import E01, TORRENT, FakeClient, H

from tow import check
from tow.check import apply as apply_step
from tow.check import run as check_run
from tow.check import space, verification
from tow.check import topic as topic_step
from tow.clients import factory, files
from tow.clients.managed import ClientError, ClientVerificationPending, completed_progress
from tow.errors import TowError
from tow.folders import paths_equal
from tow.notify import NotificationBatch
from tow.progress import reconcile_topic
from tow.progress.summary import progress_summary
from tow.store import load_download_history, load_state, save_download_history, save_state
from tow.torrent import parse_torrent_metadata
from tow.web.views import topic_progress_summary


class VerifyingClient(FakeClient):
    """A client that checks the files it finds when a torrent is started, as real clients do:
    the state is checkingDL and the progress (of the check, not of the download) rises from 0
    with every read. While ``checking`` the check outlasts any start deadline; ``end_check``
    ends it with what it found on disk, and a client that dropped the start given meanwhile
    (Deluge) leaves the torrent stopped then."""

    client_id = "main"
    client_kind = "fake"
    START_POLLS = 3

    def __init__(self):
        super().__init__()
        self.checking = True
        # Transmission: a paused add checks the data it found, and a stop would cancel that.
        self.initial_check = False

    def _add_stopped(self, content, save_path, labels):
        super()._add_stopped(content, save_path, labels)
        if self.initial_check:
            self.torrents[H].update(state="checkingDL", progress=0.0)

    def _defers_initial_check(self, info):
        return self.initial_check and str(info.get("state") or "").startswith("checking")

    def _start(self, infohash):
        super()._start(infohash)
        row = self.torrents[infohash.upper()]
        if self.checking:
            row.update(state="checkingDL", progress=0.0)
        elif self.start_works and completed_progress(row.get("progress")):
            row["state"] = "uploading"

    def inspect_torrent(self, infohash):
        row = self.torrents.get(str(infohash).upper())
        if row is not None and self.checking and str(row.get("state")).startswith("checking"):
            row["progress"] = min(0.9, round(float(row.get("progress") or 0) + 0.1, 1))
        return super().inspect_torrent(infohash)

    def end_check(self, *, found: float, dropped_start: bool) -> None:
        self.checking = False
        done = completed_progress(found)
        state = ("stoppedUP" if done else "stoppedDL") if dropped_start else ("uploading" if done else "downloading")
        self.torrents[H].update(state=state, progress=found)


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


def _checks_for(client, pauses):
    """The client's own check of the data an add found ends after TOW paused ``pauses`` times
    between its reads (``seen["pauses"]``: how often TOW waited)."""
    seen = {"pauses": 0}

    def pause():
        seen["pauses"] += 1
        if seen["pauses"] >= pauses and str(client.torrents[H]["state"]).startswith("checking"):
            client.torrents[H]["state"] = "stoppedDL"

    client._sleep = pause
    return seen


@pytest.mark.parametrize("start", [True, False])
def test_a_short_initial_check_is_waited_for_and_the_add_finished_at_once(start):
    """Transmission checks the data an add found: a check that ends within the grace period (the
    real clients' 75 KB end within a second) is waited for, and the add goes on as any other -
    its selection applied stopped and read back, then started or left stopped, released."""
    client = VerifyingClient()
    client.initial_check, client.checking = True, False
    client.START_POLLS = 600
    _checks_for(client, 3)
    info = client.add_torrent_selected(TORRENT, "D:/tv", H, [E01], start=start)
    assert [call[0] for call in client.calls] == ["add", "set_wanted", *(["start"] if start else []), "labels"]
    assert info["tags"] == ["tow"]
    assert client.wanted_torrent_indices() == {E01}
    assert client.torrents[H]["state"] == ("downloading" if start else "stoppedDL")


def test_a_long_initial_check_is_handed_over_after_the_grace_period(monkeypatch):
    client = VerifyingClient()
    client.initial_check, client.checking = True, False
    client.START_POLLS = 600
    now = {"value": 0.0}
    seen = _checks_for(client, 10**6)  # never ends here

    def monotonic():
        now["value"] += 0.5
        return now["value"]

    monkeypatch.setattr("tow.clients.managed.time.monotonic", monotonic)
    with pytest.raises(ClientVerificationPending) as error:
        client.add_torrent_selected(TORRENT, "D:/tv", H, [E01], start=False)
    assert error.value.receipt["phase"] == "awaiting_initial_check"
    assert [call[0] for call in client.calls] == ["add"]  # never stopped: that cancels the check
    assert 1 < seen["pauses"] <= client.INITIAL_CHECK_GRACE_SEC / 0.5  # waited the grace period, no longer
    assert client.INITIAL_CHECK_GRACE_SEC <= 10  # never the minute-long wait again


def test_a_pending_selection_retry_waits_for_a_short_initial_check_too():
    client = VerifyingClient()
    client.initial_check, client.checking = True, False
    client._add_stopped(TORRENT, "D:/tv", ["tow", "tow-pending"])
    client.calls.clear()
    client.START_POLLS = 600
    _checks_for(client, 2)
    info = client.configure_torrent_selection(TORRENT, H, [E01], ensure_started=True)
    assert [call[0] for call in client.calls] == ["stop", "set_wanted", "start", "labels"]
    assert info["tags"] == ["tow"]
    assert client.wanted_torrent_indices() == {E01}


@pytest.mark.parametrize("start", [True, False])
def test_the_real_client_tests_finish_a_handover_as_the_passes_do(start):
    """``helpers.finish_handover`` (tests/integration: a real client's check may outlast the
    grace period) finishes a handed-over add as TOW's passes do: nothing while the client checks,
    then the selection applied, the start when asked, the release."""
    from helpers import finish_handover

    client = VerifyingClient()
    client.initial_check, client.checking = True, False
    with pytest.raises(ClientVerificationPending) as handed_over:
        client.add_torrent_selected(TORRENT, "D:/tv", H, [E01], start=start)
    client.calls.clear()
    real_inspect = client.inspect_torrent
    reads = {"count": 0}

    def inspect(infohash):
        reads["count"] += 1
        if reads["count"] == 4:
            client.torrents[H]["state"] = "stoppedDL"  # the check ends while the helper waits
        return real_inspect(infohash)

    client.inspect_torrent = inspect
    info = finish_handover(client, H, "D:/tv", handed_over.value, seconds=60, pause=0)
    assert [call[0] for call in client.calls] == ["set_wanted", *(["start"] if start else []), "labels"]
    assert info["tags"] == ["tow"]
    assert client.wanted_torrent_indices() == {E01}
    assert client.torrents[H]["state"] == ("downloading" if start else "stoppedDL")


def test_the_real_client_tests_give_up_on_a_check_that_never_ends():
    from helpers import finish_handover

    client = VerifyingClient()
    client.initial_check, client.checking = True, False
    with pytest.raises(ClientVerificationPending) as handed_over:
        client.add_torrent_selected(TORRENT, "D:/tv", H, [E01], start=False)
    client.calls.clear()
    with pytest.raises(AssertionError, match="did not end"):
        finish_handover(client, H, "D:/tv", handed_over.value, seconds=0, pause=0)
    assert client.calls == []


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


@pytest.mark.parametrize(("state", "preparing"), [("allocating", True), ("someFutureState", False)])
def test_unknown_state_never_releases_the_pending_label(state, preparing):
    """qBittorrent reserves disk space ("allocating") before it runs a torrent: preparation,
    still pending; a state TOW does not know is no confirmed start either."""
    client = VerifyingClient()
    receipt = _pending(client)
    client.checking = False
    client.torrents[H]["state"] = state
    client.calls.clear()
    with pytest.raises(ClientError) as error:
        client.finish_verification(H, "D:/tv", receipt)
    assert isinstance(error.value, ClientVerificationPending) is preparing
    assert (error.value.receipt == receipt) if preparing else error.value.code == "client.managed.start_unconfirmed"
    assert client.calls == []
    assert "tow-pending" in client.torrents[H]["tags"]


def test_qbittorrent_allocating_after_the_start_is_deferred_without_cleanup():
    client = VerifyingClient()
    client.checking = False
    start = client._start

    def allocating(infohash):
        start(infohash)
        client.torrents[H]["state"] = "allocating"

    client._start = allocating
    receipt = _pending(client)
    assert [call[0] for call in client.calls] == ["add", "set_wanted", "start"]
    assert client.torrents[H]["tags"] == ["tow", "tow-pending"]
    client.torrents[H]["state"] = "downloading"
    client.calls.clear()
    assert client.finish_verification(H, "D:/tv", receipt)["tags"] == ["tow"]
    assert [call[0] for call in client.calls] == ["labels"]


@pytest.mark.parametrize("seeding_limit", [False, True])
def test_a_complete_torrent_left_stopped_by_its_check_gets_one_start_before_release(seeding_limit):
    client = VerifyingClient()
    receipt = _pending(client)
    client.end_check(found=1.0, dropped_start=True)  # the client dropped the start given while it checked
    client.start_works = not seeding_limit  # a reached seeding limit keeps a complete torrent stopped
    client.calls.clear()
    info = client.finish_verification(H, "D:/tv", receipt, start_stopped=True)
    assert [call[0] for call in client.calls] == ["start", "labels"]
    assert info["state"] == ("stoppedUP" if seeding_limit else "uploading")
    assert info["tags"] == ["tow"]


def test_a_check_ending_during_a_pending_selection_retry_still_gets_its_start(monkeypatch):
    client = VerifyingClient()
    _pending(client)
    real_inspect = client.inspect_torrent
    first = {"read": True}

    def inspect(infohash):
        info = real_inspect(infohash)
        if first["read"]:  # checking when the retry looked; stopped right after, its start dropped
            first["read"] = False
            client.end_check(found=0.3, dropped_start=True)
        return info

    monkeypatch.setattr(client, "inspect_torrent", inspect)
    client.calls.clear()
    info = client.configure_torrent_selection(TORRENT, H, [E01], ensure_started=True)
    assert [call[0] for call in client.calls] == ["start", "labels"]
    assert info["state"] == "downloading"
    assert info["tags"] == ["tow"]


def test_keep_stopped_never_finishes_a_pending_check_with_a_start():
    client = VerifyingClient()
    _pending(client)
    client.calls.clear()
    client.checking = False
    client.configure_torrent_selection(TORRENT, H, [E01], keep_stopped=True)
    assert "start" not in [call[0] for call in client.calls]
    assert client.torrents[H]["state"] == "stoppedDL"


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
    client.checking = False
    client.torrents[H].update(state=state, progress=1.0 if state.endswith("UP") else 0.4)
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


NEW_TORRENT = make_torrent(
    {
        b"name": "Show S01 v2",
        b"piece length": 16384,
        b"pieces": b"\1" * 20 * 5,
        b"files": [{b"length": 40000, b"path": [b"e01.mkv"]}, {b"length": 30000, b"path": [b"e02.mkv"]}],
    }
)
NEW_H = parse_torrent_metadata(NEW_TORRENT).client_hash.upper()


class Tracker:
    """A site: its .torrent (``content``), and - when set - its revision probe (``spec`` with
    ``infohash_path``, answering ``site_hash``) and its topic page's magnet (``magnet``)."""

    name = "fake"

    def __init__(self):
        self.fetches = 0
        self.content = TORRENT
        self.spec: dict = {}
        self.site_hash: str | None = None
        self.magnet: str | None = None
        self.asked = 0  # probe and topic page requests: nothing downloaded
        self.fail: Exception | None = None

    def fetch_torrent(self, *args, **kwargs):
        self.fetches += 1
        return self.content

    def fetch_title(self, *args, **kwargs):
        return "Show S01"

    def fetch_revision_hash(self, *args, **kwargs):
        self.asked += 1
        if self.fail:
            raise self.fail
        return self.site_hash

    def fetch_magnet(self, *args, **kwargs):
        self.asked += 1
        if self.fail:
            raise self.fail
        if self.magnet is None:
            raise TowError("tracker.no_magnet")
        return f"magnet:?xt=urn:btih:{self.magnet}", self.magnet


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


@pytest.mark.parametrize("seeding_limit", [False, True])
def test_pass_starts_a_complete_add_its_client_left_stopped_once(world, seeding_limit):
    client, tracker, sent = world
    _run()
    client.end_check(found=1.0, dropped_start=True)
    client.start_works = not seeding_limit
    client.calls.clear()
    row = _run(space_only=True)
    assert row["ok"]
    assert [call[0] for call in client.calls] == ["start", "labels"]
    assert client.torrents[H]["state"] == ("stoppedUP" if seeding_limit else "uploading")
    assert "verification_pending" not in load_state()["topics"][0]
    client.calls.clear()
    check.run_check(apply=True, notify=True, how="test", space_only=True)
    check.run_check(apply=True, notify=True, how="test", progress_only=True)
    assert client.calls == []
    assert tracker.fetches == 1
    assert sent == ["updated"]


def test_an_owner_paused_complete_torrent_is_not_started_by_a_pass(world):
    client, _tracker, _sent = world
    _run()
    client.end_check(found=1.0, dropped_start=True)
    client.torrents[H]["tags"] = ["tow"]  # no unfinished add of TOW's any more: the owner's pause stands
    client.calls.clear()
    row = _run(space_only=True)
    assert row["ok"]
    assert client.calls == []
    assert client.torrents[H]["state"] == "stoppedUP"


def test_allocating_add_is_deferred_and_finished_without_stops_or_repeated_starts(world, monkeypatch):
    client, tracker, sent = world
    client.checking = False
    start = client._start

    def allocating(infohash):
        start(infohash)
        client.torrents[H]["state"] = "allocating"

    monkeypatch.setattr(client, "_start", allocating)
    first = _run()
    assert first["status"] == "verification_pending"
    assert [call[0] for call in client.calls] == ["add", "set_wanted", "start"]
    client.calls.clear()
    assert _run(space_only=True)["status"] == "verification_pending"
    assert _run()["status"] == "verification_pending"
    assert client.calls == []
    client.torrents[H]["state"] = "downloading"
    assert _run(space_only=True)["ok"]
    assert [call[0] for call in client.calls] == ["labels"]
    assert len(client.torrents) == 1
    assert tracker.fetches == 1
    assert sent == ["updated"]


def test_a_start_given_by_a_selection_retry_counts_as_the_receipts_one_start(world, monkeypatch):
    """A check finds TOW's unfinished add checking; the check ends before the finalization, which
    starts it once, and the client checks again: the persisted receipt knows its start was given."""
    client, _tracker, _sent = world
    disk = load_state()
    topic = disk["topics"][0]
    topic.update(hash=H, selection_hash=H, selection_verified=True)
    save_state(disk)
    client.seed(tags=["tow", "tow-pending"], state="checkingDL", wanted={E01}, path=topic["save_path"])
    real_finish = client.finish_verification

    def finish(*args, **kwargs):
        client.end_check(found=0.3, dropped_start=True)
        client.checking = True  # the start makes it check its files again
        return real_finish(*args, **kwargs)

    monkeypatch.setattr(client, "finish_verification", finish)
    assert _run()["status"] == "verification_pending"
    assert [call[0] for call in client.calls] == ["start"]
    assert load_state()["topics"][0]["verification_pending"]["resume_attempted"] is True
    monkeypatch.setattr(client, "finish_verification", real_finish)
    client.end_check(found=0.3, dropped_start=True)
    client.calls.clear()
    assert not _run(space_only=True)["ok"]
    assert client.calls == []


@pytest.mark.parametrize("fits", [True, False])
def test_an_add_checked_before_its_selection_is_finished_by_the_passes(world, monkeypatch, fits):
    """Transmission checks the data a paused add found for hours: the add is handed over at once,
    survives restarts in the saved receipt, and the passes apply its selection, stopped and read
    back, once the check ends - then start it, or let it wait for disk space - with no new
    download from the site."""
    client, tracker, sent = world
    client.initial_check, client.checking = True, False
    folder = load_state()["topics"][0]["save_path"]
    if not fits:
        full = space.Shortfall(needed=1024**3, free=0, path=folder)
        monkeypatch.setattr(apply_step, "free_space_problem", lambda *args, **kwargs: full)
        monkeypatch.setattr(space, "_measured", lambda *args, **kwargs: (True, full))
    first = _run()
    assert first["status"] == "verification_pending"
    assert not first.get("error")
    receipt = load_state()["topics"][0]["verification_pending"]["unapplied_receipt"]  # never read as applied
    assert (receipt["phase"], receipt["start"]) == ("awaiting_initial_check", fits)
    assert [call[0] for call in client.calls] == ["add"]
    assert client.wanted_torrent_indices() == {0, 1, 2}  # not applied, never running like this
    client.calls.clear()
    assert _run(space_only=True)["status"] == "verification_pending"  # still checking: nothing changes
    assert client.calls == []
    client.torrents[H]["state"] = "stoppedDL"  # the check ended
    row = _run(space_only=True)
    topic = load_state()["topics"][0]
    assert client.wanted_torrent_indices() == {E01}
    assert "verification_pending" not in topic
    if fits:
        assert row["ok"]
        assert [call[0] for call in client.calls] == ["set_wanted", "start", "labels"]
        assert client.torrents[H]["state"] == "downloading"
        assert sent == ["updated"]
    else:
        assert row["status"] == "waiting_space"
        assert [call[0] for call in client.calls] == ["set_wanted", "labels"]
        assert client.torrents[H]["state"] == "stoppedDL"
        assert topic["waiting_space"]["hash"] == H
        assert topic["last_error_code"] == "check.waiting_space"
        monkeypatch.setattr(space, "_measured", lambda *args, **kwargs: (True, None))
        assert _run(space_only=True)["ok"]
        assert client.torrents[H]["state"] == "downloading"
        assert sent == ["error", "updated", "recovered"]  # as for every reported wait for disk space
    assert tracker.fetches == 1


class Reader13010(VerifyingClient):
    """How TOW 1.30.10 finishes a saved receipt, as released up to its start: the client part of
    ``tow.check.verification._confirm``, ``_verify_receipt`` and the beginning of
    ``_finish_verification`` of ``tow.clients.managed`` (git show v1.30.10:<file>). It knows no
    receipt phase and reads ``receipt`` as an applied, read-back selection."""

    def confirm(self, waiting):
        info = self.inspect_torrent(H)
        stopped = str((info or {}).get("state") or "").casefold().startswith(("stopped", "paused"))
        pending_mark = "tow-pending" in {str(tag).casefold().strip() for tag in (info or {}).get("tags") or []}
        retry = stopped and (
            (pending_mark or waiting.get("resume_after_check")) and not waiting.get("resume_attempted")
        )
        return self.finish_13010(
            H,
            str(waiting.get("path") or ""),
            waiting.get("receipt") or {},
            start_stopped=retry,
            on_start=lambda: waiting.__setitem__("resume_attempted", True),
        )

    def verify_13010(self, infohash, destination, receipt, *, require_pending):
        if not isinstance(receipt, dict):
            raise self._fail("client.managed.wrong_selection")
        rows, wanted, ignored = receipt.get("files"), receipt.get("wanted"), receipt.get("ignored", [])
        if (
            not isinstance(rows, list)
            or not rows
            or any(not isinstance(row, dict) for row in rows)
            or not isinstance(wanted, list)
            or not wanted
            or not isinstance(ignored, list)
            or any(type(index) is not int or index < 0 for index in [*wanted, *ignored])
        ):
            raise self._fail("client.managed.wrong_selection")
        priorities = files.priorities(rows, fail=lambda: self._fail("client.managed.wrong_selection"))
        padding = {row["index"] for row in rows if self._padding_like(row.get("name"))}
        if (
            not set(ignored).issubset(padding)
            or set(wanted).intersection(ignored)
            or not set(wanted).issubset(priorities)
        ):
            raise self._fail("client.managed.wrong_selection")
        info = self.inspect_torrent(infohash)
        if info is None:
            raise self._fail("client.managed.missing")
        identities = {str(info.get(key) or "").upper() for key in ("hash", "infohash_v1", "infohash_v2")}
        identities.update(value[:40] for value in list(identities) if len(value) == 64)
        if infohash.upper() not in identities or str(receipt.get("hash") or "").upper() != infohash.upper():
            raise self._fail("client.managed.hash_changed_selection")
        if "tow" not in self._tags(info) or (require_pending and "tow-pending" not in self._tags(info)):
            raise self._fail("client.managed.not_owned")
        if not destination or not paths_equal(str(info.get("save_path") or ""), destination):
            raise self._fail("client.managed.wrong_folder")
        self._check_state(info)
        files.verify_selection(
            rows,
            list(info.get("files") or []),
            set(wanted),
            ignored=set(ignored),
            fail=lambda: self._fail("client.managed.wrong_selection"),
        )
        return info

    def finish_13010(self, infohash, destination, receipt, *, start_stopped, on_start):
        info = self.verify_13010(infohash, destination, receipt, require_pending=False)
        if str(info.get("state") or "").casefold().startswith("checking"):
            raise ClientVerificationPending(infohash, prefix=self.title, receipt=receipt)
        if self._stopped(info) and not completed_progress(info.get("progress")) and start_stopped:
            self._require_owned(infohash)
            on_start()
            self._start(infohash)
        return info


def test_a_rollback_to_1_30_10_never_starts_an_add_held_back_for_room(world, monkeypatch):
    """Transmission checks an add of all files that does not fit: it is handed over before its
    selection, to be released stopped. TOW 1.30.10 reads ``receipt`` as an applied selection - an
    add of all files passes it - and would start the torrent without measuring disk space. The
    receipt is saved where 1.30.10 does not look: it refuses the receipt before asking the client
    for anything (a red error the owner repairs with a check by hand, which measures the space)."""
    client, _tracker, _sent = world
    disk = load_state()
    disk["topics"][0]["selection"] = {"mode": "all", "value": ""}
    save_state(disk)
    client.initial_check, client.checking = True, False
    folder = disk["topics"][0]["save_path"]
    full = space.Shortfall(needed=1024**3, free=0, path=folder)
    monkeypatch.setattr(apply_step, "free_space_problem", lambda *args, **kwargs: full)
    monkeypatch.setattr(space, "_measured", lambda *args, **kwargs: (True, full))
    assert _run()["status"] == "verification_pending"
    waiting = load_state()["topics"][0]["verification_pending"]
    old = Reader13010()
    old.torrents = client.torrents
    old.torrents[H]["state"] = "stoppedDL"  # Transmission's check ended
    with pytest.raises(ClientError) as error:
        old.confirm(waiting)
    assert error.value.code == "client.managed.wrong_selection"  # blocked by 1.30.10, nothing started
    assert old.calls == []
    assert old.torrents[H]["state"] == "stoppedDL"
    assert not waiting.get("resume_attempted")
    assert "receipt" not in waiting  # the only key 1.30.10 reads
    assert (waiting["unapplied_receipt"]["phase"], waiting["unapplied_receipt"]["start"]) == (
        "awaiting_initial_check",
        False,
    )
    row = _run(space_only=True)  # this version still finishes it: released stopped, waiting for room
    assert row["status"] == "waiting_space"
    assert "start" not in [call[0] for call in client.calls]


@pytest.mark.parametrize("owner_paused", [False, True])
def test_a_lost_answer_after_the_initial_check_start_never_replays_it(world, monkeypatch, owner_paused):
    """The pass that applied the selection of an add handed over during Transmission's check and
    started it loses the client's answer right after the start: the saved receipt is the applied
    one and its one start is spent - the next pass neither stops, reselects nor starts it again,
    and a pause the owner gave meanwhile stands."""
    client, _tracker, _sent = world
    client.initial_check, client.checking = True, False
    assert _run()["status"] == "verification_pending"
    client.torrents[H]["state"] = "stoppedDL"  # Transmission's check ended
    real_inspect, real_start = client.inspect_torrent, client._start
    started = {"done": False}

    def start(infohash):
        real_start(infohash)
        started["done"] = True

    def inspect(infohash):
        if started["done"]:
            raise ClientError("client.transmission.no_connection", prefix="Fake")
        return real_inspect(infohash)

    monkeypatch.setattr(client, "_start", start)
    monkeypatch.setattr(client, "inspect_torrent", inspect)
    assert _run(space_only=True)["status"] == "failed"
    pending = load_state()["topics"][0]["verification_pending"]
    assert pending["resume_attempted"] is True
    assert "phase" not in pending["receipt"]  # applied: a lost answer does not rewind it
    assert not pending.get("blocked")
    monkeypatch.setattr(client, "_start", real_start)
    monkeypatch.setattr(client, "inspect_torrent", real_inspect)
    assert client.torrents[H]["state"] == "downloading"
    if owner_paused:
        client.torrents[H]["state"] = "stoppedDL"
    client.calls.clear()
    row = _run(space_only=True)
    assert not {"stop", "set_wanted", "start"} & {call[0] for call in client.calls}
    assert client.wanted_torrent_indices() == {E01}
    if owner_paused:
        assert not row["ok"]
        assert client.torrents[H]["state"] == "stoppedDL"
    else:
        assert row["ok"]
        assert [call[0] for call in client.calls] == ["labels"]


def test_a_spent_initial_check_start_saved_before_its_receipt_was_applied_is_not_replayed(world):
    """A receipt saved with its initial-check phase although its one start was given (an earlier
    pass failed right after it): the next pass finishes it as applied, without a new start."""
    client, _tracker, _sent = world
    client.initial_check, client.checking = True, False
    _run()
    disk = load_state()
    disk["topics"][0]["verification_pending"]["resume_attempted"] = True
    save_state(disk)
    client.torrents[H]["state"] = "stoppedDL"
    client._set_wanted(H, {client.client_id_of(E01)}, [0, 1, 2])  # applied before that start
    client.calls.clear()
    row = _run(space_only=True)
    assert not row["ok"]
    assert client.calls == []  # never stopped, reselected or started again
    assert client.torrents[H]["state"] == "stoppedDL"


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


def _watch():
    disk = load_state()
    disk["topics"][0]["tracking_mode"] = "watch"
    save_state(disk)


def test_a_queued_manual_check_readds_only_a_removal_seen_before_the_click(world):
    """A row's check that waited in the queue runs minutes after the click: a torrent removed in
    the client meanwhile - after the owner asked - is reported, not added back."""
    import time

    client, _tracker, _sent = world
    _watch()
    client.checking = False
    assert _run()["ok"]
    clicked = time.time()
    client.torrents.clear()  # removed in the client after the click
    client.calls.clear()
    row = check.run_check(apply=True, notify=False, how="manual", ids=["slow"], readd_since=clicked)["results"][0]
    assert not row["ok"]
    assert "add" not in [call[0] for call in client.calls]
    seen = load_state()["topics"][0]["client_removed_at"]  # the check saw it gone
    assert row["error_record"]["code"] == "check.removed_from_client"
    row = check.run_check(apply=True, notify=False, how="manual", ids=["slow"], readd_since=time.time() + 1)
    assert client.calls[0][0] == "add"
    assert row["results"][0]["ok"]
    assert "client_removed_at" not in load_state()["topics"][0]
    assert seen


@pytest.mark.parametrize("blocked", ["wrong_selection", "wrong_folder"])
def test_a_queued_repair_never_readds_a_torrent_removed_after_the_click(world, blocked):
    """A row's check that repairs a blocked receipt waited in the queue; the owner removed the
    torrent meanwhile: the repair is no reason to add it back - only a removal seen before the
    click is."""
    import time

    client, _tracker, _sent = world
    _run()
    client.torrents[H].update(state="downloading", progress=0.5)
    if blocked == "wrong_selection":
        client.torrents[H]["files"][0]["priority"] = 0
    else:
        client.torrents[H]["save_path"] = "E:/elsewhere"
    _run(space_only=True)
    assert _blocked() == f"client.managed.{blocked}"
    clicked = time.time()
    client.torrents.clear()  # removed in the client after the click
    client.calls.clear()
    row = check.run_check(apply=True, notify=False, how="manual", ids=["slow"], readd_since=clicked)["results"][0]
    assert "add" not in [call[0] for call in client.calls]
    assert not row["ok"]
    assert row["error_record"]["code"] == "check.removed_from_client"
    assert load_state()["topics"][0]["client_removed_at"]  # seen gone now: a later click adds it back
    client.checking = False
    row = check.run_check(apply=True, notify=False, how="manual", ids=["slow"], readd_since=time.time() + 1)
    assert client.calls[0][0] == "add"
    assert row["results"][0]["ok"]


@pytest.mark.parametrize("queued", [False, True])
def test_a_new_file_selection_never_readds_a_torrent_the_owner_removed(world, queued):
    """The owner chose other files of a topic whose torrent they removed in the client: neither
    a scheduled check nor a row's check asked before the removal adds it back for the selection."""
    import time

    client, _tracker, _sent = world
    _watch()
    client.checking = False
    assert _run()["ok"]
    clicked = time.time()
    disk = load_state()
    disk["topics"][0].update(selection={"mode": "files", "value": "*e02.mkv"}, selection_dirty=True)
    save_state(disk)
    client.torrents.clear()
    client.calls.clear()
    if queued:
        row = check.run_check(apply=True, notify=False, how="manual", ids=["slow"], readd_since=clicked)
        row = row["results"][0]
    else:
        row = _run()
    assert "add" not in [call[0] for call in client.calls]
    assert not row["ok"]
    assert row["error_record"]["code"] == "check.removed_from_client"


def test_a_removal_noted_in_the_clicks_own_second_is_not_before_the_click():
    """``client_removed_at`` (and a receipt's ``missing_since``) is stored to the whole second: a
    removal another check saw at :40.8, after a click at :40.3, is stored as :40 and read as seen
    before the click - and the queued row check added the torrent back."""
    from datetime import datetime, timedelta, timezone

    from tow.check.rows import removal_seen_by

    second = datetime(2026, 10, 16, 10, 0, 40, tzinfo=timezone(timedelta(hours=3)))
    stamp = second.isoformat(timespec="seconds")
    click = second.timestamp() + 0.3
    assert removal_seen_by({"client_removed_at": stamp}, click) is False
    assert removal_seen_by({"verification_pending": {"missing_since": stamp}}, click) is False
    assert removal_seen_by({"client_removed_at": stamp}, click + 1) is True  # a click a second later
    # A time stored to the microsecond is exact.
    assert removal_seen_by({"client_removed_at": second.replace(microsecond=100000).isoformat()}, click) is True
    assert removal_seen_by({"client_removed_at": second.replace(microsecond=800000).isoformat()}, click) is False


def test_a_manual_check_started_at_once_still_readds_a_removed_torrent(world):
    client, _tracker, _sent = world
    _watch()
    client.checking = False
    assert _run()["ok"]
    client.torrents.clear()
    client.calls.clear()
    row = check.run_check(apply=True, notify=False, how="manual", ids=["slow"])["results"][0]
    assert row["ok"]
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


@pytest.fixture
def clock(monkeypatch):
    """The wall clock verification reads, moved by the test (minutes from a fixed start)."""
    from datetime import UTC, datetime, timedelta

    start = datetime(2026, 10, 14, 12, 0, tzinfo=UTC)
    moment = {"minutes": 0.0}
    monkeypatch.setattr(verification, "machine_now", lambda: start + timedelta(minutes=moment["minutes"]))
    return moment


def _blocked():
    return load_state()["topics"][0]["verification_pending"].get("blocked")


def test_a_torrent_not_listed_once_is_not_blocked_and_recovers(world, clock):
    client, tracker, _sent = world
    _run()
    kept = client.torrents.pop(H)
    row = _run(space_only=True)
    assert row["status"] == "verification_pending"
    assert not row.get("error")
    assert not _blocked()
    clock["minutes"] = 5
    client.torrents[H] = {**kept, "state": "downloading", "progress": 0.2}
    assert _run(space_only=True)["ok"]
    assert "verification_pending" not in load_state()["topics"][0]
    assert tracker.fetches == 1


def test_a_torrent_absent_from_two_answers_ten_minutes_apart_is_blocked(world, clock):
    client, _tracker, _sent = world
    _run()
    client.torrents.clear()
    client.calls.clear()
    for minutes in (0, 1, 2, 5, 9.9):
        clock["minutes"] = minutes
        assert _run(space_only=True)["status"] == "verification_pending"
        assert not _blocked()
    clock["minutes"] = 10
    row = _run(space_only=True)
    assert row["status"] == "failed"
    assert _blocked() == "client.managed.missing"
    assert client.calls == []  # never added again by a pass


def test_a_torrent_gone_from_the_finishing_read_waits_for_the_same_confirmation(world, clock, monkeypatch):
    """The pass's first look lists the torrent, the client's own read while finishing it does not
    (a client restarting meanwhile): that absence counts as one answer without it, as every other
    one - not blocked before two answers ten minutes apart."""
    client, _tracker, _sent = world
    _run()
    client.torrents[H].update(state="downloading", progress=0.4)
    kept = client.torrents[H]
    real_inspect = client.inspect_torrent
    reads = {"count": 0}

    def flaky(infohash):
        reads["count"] += 1
        return real_inspect(infohash) if reads["count"] == 1 else None  # gone after the first look

    monkeypatch.setattr(client, "inspect_torrent", flaky)
    client.calls.clear()
    row = _run(space_only=True)
    assert row["status"] == "verification_pending"
    assert not row.get("error")
    assert not _blocked()
    assert load_state()["topics"][0]["verification_pending"]["missing_seen"] == 1
    monkeypatch.setattr(client, "inspect_torrent", real_inspect)
    clock["minutes"] = 5
    client.torrents[H] = kept  # listed again
    assert _run(space_only=True)["ok"]
    assert client.calls == [("labels", ("tow",))]


def test_a_finishing_read_without_the_torrent_blocks_after_ten_minutes(world, clock, monkeypatch):
    client, _tracker, _sent = world
    _run()
    client.torrents[H].update(state="downloading", progress=0.4)
    real_inspect = client.inspect_torrent
    reads = {"count": 0}

    def flaky(infohash):
        reads["count"] += 1
        return real_inspect(infohash) if reads["count"] == 1 else None

    monkeypatch.setattr(client, "inspect_torrent", flaky)
    assert _run(space_only=True)["status"] == "verification_pending"
    monkeypatch.setattr(client, "inspect_torrent", real_inspect)
    client.torrents.clear()
    clock["minutes"] = 9
    assert _run(space_only=True)["status"] == "verification_pending"
    clock["minutes"] = 10
    assert _run(space_only=True)["status"] == "failed"
    assert _blocked() == "client.managed.missing"


def test_a_client_that_does_not_answer_does_not_count_as_an_absence(world, clock, monkeypatch):
    client, _tracker, _sent = world
    _run()
    real_inspect = client.inspect_torrent

    def unreachable(_h):
        raise ClientError("client.transmission.no_connection", prefix="Fake")

    monkeypatch.setattr(client, "inspect_torrent", unreachable)
    _run(space_only=True)
    pending = load_state()["topics"][0]["verification_pending"]
    assert "missing_since" not in pending
    monkeypatch.setattr(client, "inspect_torrent", real_inspect)
    client.torrents.clear()
    clock["minutes"] = 10
    assert _run(space_only=True)["status"] == "verification_pending"
    clock["minutes"] = 15
    assert _run(space_only=True)["status"] == "verification_pending"
    clock["minutes"] = 20
    assert _run(space_only=True)["status"] == "failed"


def test_a_clock_set_back_does_not_confirm_an_absence_early(world, clock):
    client, _tracker, _sent = world
    _run()
    client.torrents.clear()
    clock["minutes"] = 60
    _run(space_only=True)
    clock["minutes"] = 0  # set back an hour
    assert _run(space_only=True)["status"] == "verification_pending"
    clock["minutes"] = 9
    assert _run(space_only=True)["status"] == "verification_pending"
    assert not _blocked()


def test_a_verification_waiting_twelve_hours_is_a_red_client_error_not_an_end(world, clock):
    from tow.status import project_home_status

    client, tracker, sent = world
    _run()
    before = load_state()["topics"][0]
    clock["minutes"] = 12 * 60 - 1
    assert _run(space_only=True)["status"] == "verification_pending"
    assert not load_state()["topics"][0]["last_error"]
    clock["minutes"] = 12 * 60
    row = _run(space_only=True)
    topic = load_state()["topics"][0]
    assert row["status"] == "failed"
    assert topic["last_error_code"] == "check.verification_overdue"
    assert topic["last_error_class"] == "qbit"
    status = project_home_status(topic, {})
    assert (status.torrent_tone, status.tracker_tone) == ("bad", "ok")  # the site answered; not its fault
    pending = topic["verification_pending"]
    assert not pending.get("blocked")
    assert (pending["hash"], pending["receipt"], pending["since"]) == (
        H,
        before["verification_pending"]["receipt"],
        before["verification_pending"]["since"],
    )
    assert topic["hash"] == H
    assert sent == ["error"]
    _run(space_only=True)
    assert sent == ["error"]  # told once
    client.calls.clear()
    client.end_check(found=0.4, dropped_start=False)
    assert _run(space_only=True)["ok"]
    assert not load_state()["topics"][0]["last_error"]
    assert [call[0] for call in client.calls] == ["labels"]
    assert tracker.fetches == 1


def test_a_redeferred_transaction_keeps_its_first_anchor(world, clock):
    client, _tracker, _sent = world
    _run()
    since = "2026-10-14T11:00:00+00:00"  # an hour before the clock's start: its first deferral
    disk = load_state()
    disk["topics"][0]["verification_pending"]["since"] = since
    save_state(disk)
    client.torrents[H].update(state="downloading", progress=0.5)
    client.torrents[H]["files"][0]["priority"] = 0
    clock["minutes"] = 30
    _run(space_only=True)
    client.checking = True
    clock["minutes"] = 60
    row = check.run_check(apply=True, notify=False, how="manual", ids=["slow"])["results"][0]
    assert row["status"] == "verification_pending"
    assert load_state()["topics"][0]["verification_pending"]["since"] == since


@pytest.mark.parametrize("evidence", ["magnet", "probe"])
def test_a_waiting_receipt_asks_the_site_on_its_cadence_without_any_client_write(world, evidence):
    client, tracker, sent = world
    _run()
    receipt = copy.deepcopy(load_state()["topics"][0]["verification_pending"])
    if evidence == "probe":
        tracker.spec = {"infohash_path": "/details?id={id}"}
        tracker.site_hash = NEW_H
    else:
        tracker.magnet = NEW_H
    client.calls.clear()
    row = _run()
    topic = load_state()["topics"][0]
    assert row["status"] == "verification_pending"
    assert topic["verification_pending"]["observed_hash"] == NEW_H  # kept apart from the transaction
    assert {key: value for key, value in topic["verification_pending"].items() if key != "observed_hash"} == receipt
    assert topic["hash"] == H
    assert topic["site_ok_at"] == topic["last_check"]
    assert client.calls == []
    assert tracker.fetches == 1  # no .torrent downloaded
    assert sent == []
    asked = tracker.asked
    check.run_check(apply=True, notify=True, how="test", space_only=True)
    check.run_check(apply=True, notify=True, how="test", progress_only=True)
    assert tracker.asked == asked  # the five-minute passes never ask the site
    tracker.magnet = tracker.site_hash = H
    _run()
    assert "observed_hash" not in load_state()["topics"][0]["verification_pending"]


def test_a_site_failure_while_the_client_verifies_leaves_the_receipt_alone(world):
    client, tracker, _sent = world
    _run()
    receipt = copy.deepcopy(load_state()["topics"][0]["verification_pending"])
    tracker.fail = TowError("mirrors.transport", error="ConnectTimeout")
    client.calls.clear()
    row = _run()
    topic = load_state()["topics"][0]
    assert row["status"] == "failed"
    assert topic["last_error_class"] == "tracker"
    assert topic["verification_pending"] == receipt
    assert client.calls == []
    tracker.fail = None
    client.end_check(found=0.4, dropped_start=False)
    assert _run(space_only=True)["ok"]


def _block(client):
    _run()
    client.torrents[H].update(state="stoppedDL", progress=0.3, tags=["tow"])
    _run(space_only=True)
    assert load_state()["topics"][0]["verification_pending"]["blocked"]
    client.calls.clear()


def test_a_blocked_unchanged_revision_is_confirmed_by_the_probe_without_a_download(world):
    client, tracker, _sent = world
    tracker.spec = {"infohash_path": "/details?id={id}", "download_limit": True}
    _block(client)
    tracker.site_hash = H
    row = _run()
    topic = load_state()["topics"][0]
    assert row["status"] == "failed"
    assert topic["last_error_code"] == "client.managed.start_unconfirmed"  # equality never clears it
    assert topic["site_ok_at"] == topic["last_check"]
    assert tracker.fetches == 1
    assert client.calls == []


def test_a_blocked_receipt_still_finds_a_newer_revision(world):
    client, tracker, _sent = world
    tracker.spec = {"infohash_path": "/details?id={id}", "download_limit": True}
    _block(client)
    tracker.site_hash = NEW_H
    tracker.content = NEW_TORRENT
    client.checking = False
    row = _run()
    assert tracker.fetches == 2
    assert row["ok"]
    assert load_state()["topics"][0]["hash"] == NEW_H
    assert client.calls[0][0] == "add"


def test_a_limited_site_without_revision_evidence_is_not_asked_for_a_blocked_torrent(world):
    client, tracker, _sent = world
    tracker.spec = {"download_limit": True}
    _run()
    client.torrents[H].update(state="downloading", progress=0.5)
    client.torrents[H]["files"][0]["priority"] = 0  # the owner changed it: only a new handover repairs it
    _run(space_only=True)
    client.calls.clear()
    row = _run()
    assert row["status"] == "failed"
    assert tracker.fetches == 1
    assert "by hand" in row["error"] or "вручную" in row["error"]
    assert load_state()["topics"][0]["verification_pending"]["blocked"] == "client.managed.wrong_selection"
    assert client.calls == []
    check.run_check(apply=True, notify=False, how="manual", ids=["slow"])
    assert tracker.fetches == 2  # a check started by hand still fetches it


@pytest.mark.parametrize("pending", [False, True])
def test_the_unfinished_add_question_uses_the_runs_client_listing(pending):
    """Whether TOW's own add is unfinished is an observation (qBittorrent answers it from one
    listing per run), not a read-back: no full read of every topic's torrent for it."""
    observed = []

    class Listed:
        def observe_torrent(self, h):
            observed.append(h)
            return {"hash": h, "tags": ["tow", "tow-pending"] if pending else ["tow"]}

        def inspect_torrent(self, h):
            raise AssertionError("a full read for an observation")

    topic = {"hash": H, "selection_hash": H, "selection_verified": True}
    work = SimpleNamespace(topic=topic, old=H, row={}, client=Listed(), run=SimpleNamespace(apply=True))
    assert topic_step._needs_selection_update(work, SimpleNamespace(mode="files"), H) is pending
    assert observed == [H]


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


def test_a_completion_event_read_from_the_state_is_only_a_known_kind(world):
    """The log kind of a confirmed add comes from state.json: an edited or foreign state could put
    any string into the log as a kind. Only "client_started" is taken; anything else is the
    default (1.33.1 QA-06)."""
    from tow.log import read_events

    client, _tracker, _sent = world
    _run()
    disk = load_state()
    disk["topics"][0]["verification_pending"]["completion_event"] = "forged_kind"
    save_state(disk)
    client.torrents[H].update(state="downloading", progress=0.4)
    assert _run(space_only=True)["added"]
    kinds = [event.get("kind") for event in read_events()]
    assert "forged_kind" not in kinds
    assert "client_added" in kinds


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


@pytest.mark.parametrize("drift", ["selection", "folder"])
def test_drift_during_a_space_wait_start_is_stopped_and_stays_red(world, monkeypatch, drift):
    client, _tracker, _sent = world
    folder = load_state()["topics"][0]["save_path"]
    full = space.Shortfall(needed=1024**3, free=0, path=folder)
    monkeypatch.setattr(apply_step, "free_space_problem", lambda *args, **kwargs: full)
    assert _run()["status"] == "waiting_space"
    monkeypatch.setattr(space, "_measured", lambda *args, **kwargs: (True, None))
    start = client._start

    def drifting(infohash):
        start(infohash)
        if drift == "selection":
            client.torrents[H]["files"][0]["priority"] = 1 - client.torrents[H]["files"][0]["priority"]
        else:
            client.torrents[H]["save_path"] = "E:/elsewhere"

    monkeypatch.setattr(client, "_start", drifting)
    client.calls.clear()
    row = _run(space_only=True)
    topic = load_state()["topics"][0]
    assert row["status"] == "failed"
    assert client.calls[-1] == ("stop",)  # stopped, never left downloading
    assert not topic.get("waiting_space")
    pending = topic["verification_pending"]
    assert pending["blocked"]
    assert pending["hash"] == H
    assert topic["last_error"]
    monkeypatch.setattr(client, "_start", start)
    client.calls.clear()
    for scope in ({}, {"space_only": True}, {"progress_only": True}):
        check.run_check(apply=True, notify=False, how="test", **scope)
        topic = load_state()["topics"][0]
        assert topic["last_error"]  # never green
        assert not topic["last_ok"]
    assert "start" not in [call[0] for call in client.calls]  # never started again by itself
    if drift == "selection":
        monkeypatch.setattr(apply_step, "free_space_problem", lambda *args, **kwargs: None)
        client.checking = False
        row = check.run_check(apply=True, notify=False, how="manual", ids=["slow"])["results"][0]
        assert row["ok"]
        assert client.torrents[H]["state"] == "downloading"
        assert client.wanted_torrent_indices() == {E01}
        assert "verification_pending" not in load_state()["topics"][0]


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
    client.checking = False  # the check's own progress reached 1.0 before its state changed
    client.torrents[H].update(state="checkingDL", progress=1.0)
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
