"""A running check does not hold up the UI; edits made meanwhile are kept; messages survive a crash."""

from __future__ import annotations

import threading

import pytest
from test_check_contract import FakeClient, _wire_fake_check

from tow import check
from tow.check import client_ops
from tow.check import notices as check_notices
from tow.notify import event_text
from tow.store import load_state, persistence_lock, save_state

TOPIC = {"id": "t", "title": "Show", "url": "https://tracker/1", "save_path": r"M:\TV", "hash": None}


def _lock_free_within(seconds: float) -> bool:
    got = threading.Event()

    def take():
        with persistence_lock():
            got.set()

    worker = threading.Thread(target=take, daemon=True)
    worker.start()
    worker.join(seconds)
    return got.is_set()


def test_the_network_phase_does_not_hold_the_data_lock(monkeypatch):
    save_state({"topics": [dict(TOPIC)]})
    tracker = _wire_fake_check(monkeypatch, FakeClient())
    seen = {}
    original = tracker.fetch_torrent

    def fetch(*args, **kwargs):
        seen["free"] = _lock_free_within(2.0)  # a web request taking the lock now
        return original(*args, **kwargs)

    tracker.fetch_torrent = fetch
    check.run_check(apply=True, notify=False)
    assert seen == {"free": True}


def test_an_edit_made_during_the_check_is_kept(monkeypatch):
    save_state({"topics": [dict(TOPIC)]})
    tracker = _wire_fake_check(monkeypatch, FakeClient())
    original = tracker.fetch_torrent

    def fetch(*args, **kwargs):
        with persistence_lock():  # the owner renames the topic in the UI meanwhile
            state = load_state()
            state["topics"][0]["title"] = "Моё название"
            state["topics"][0]["selection_dirty"] = True
            save_state(state)
        return original(*args, **kwargs)

    tracker.fetch_torrent = fetch
    check.run_check(apply=True, notify=False)
    topic = load_state()["topics"][0]
    assert topic["title"] == "Моё название"
    assert topic["selection_dirty"] is True  # the new selection is applied next time
    assert topic["hash"] == "HASH-NEW"  # the check's facts still land


def _during_fetch(monkeypatch, tracker, edit):
    original = tracker.fetch_torrent

    def fetch(*args, **kwargs):
        with persistence_lock():
            state = load_state()
            edit(state["topics"][0])
            save_state(state)
        return original(*args, **kwargs)

    tracker.fetch_torrent = fetch


def test_a_selection_edit_during_the_check_reaches_the_client_next_time(monkeypatch):
    """M1: the topic stores ``selection``; an edit of it while the check ran was lost forever."""
    save_state({"topics": [{**TOPIC, "selection": {"mode": "all", "value": ""}}]})
    tracker = _wire_fake_check(monkeypatch, FakeClient())
    new = {"mode": "episodes", "value": "1-3"}
    # No hash yet, so the edit form sets no selection_dirty: only `selection` changes.
    _during_fetch(monkeypatch, tracker, lambda topic: topic.update(selection=new))
    out = check.run_check(apply=True, notify=False)
    topic = load_state()["topics"][0]
    assert not topic.get("hash")  # nothing added with the selection the check started with...
    assert out["results"][0]["status"] == "skipped"  # ...the next check adds it...
    assert topic["selection"] == new  # ...with the owner's selection


def test_an_undo_of_a_selection_edit_during_the_check_is_not_lost(monkeypatch):
    from tow import undo

    edited = {**TOPIC, "selection": {"mode": "all", "value": ""}}
    save_state({"topics": [edited]})
    tracker = _wire_fake_check(monkeypatch, FakeClient())
    before_edit = {**TOPIC, "selection": {"mode": "episodes", "value": "1-3"}}
    original = tracker.fetch_torrent

    def fetch(*args, **kwargs):
        with persistence_lock():
            state = load_state()
            undo.stamp(state, "topic_put", item=before_edit)
            save_state(state)
            undo.apply()
        return original(*args, **kwargs)

    tracker.fetch_torrent = fetch
    check.run_check(apply=True, notify=False)
    topic = load_state()["topics"][0]
    assert not topic.get("hash")  # not added with "all", the selection the check started with
    assert topic["selection"] == {"mode": "episodes", "value": "1-3"}  # the next check uses this one


def test_a_move_started_during_the_check_is_kept(monkeypatch):
    """M2: the check's copy had no move_pending, and the merge removed the owner's marker
    (the next reconcile then failed red with "path differs")."""
    save_state({"topics": [{**TOPIC, "hash": "HASH-NEW"}]})
    tracker = _wire_fake_check(monkeypatch, FakeClient())
    move = {"from": r"M:\TV", "to": r"N:\TV", "since": "2026-10-01T12:00:00+03:00"}
    _during_fetch(monkeypatch, tracker, lambda topic: topic.update(save_path=r"N:\TV", move_pending=move))
    check.run_check(apply=True, notify=False)
    topic = load_state()["topics"][0]
    assert topic["move_pending"] == move
    assert topic["save_path"] == r"N:\TV"


def test_a_check_ends_a_move_only_when_it_sees_the_client_at_the_new_folder():
    from tow.progress import _check_save_path

    move = {"from": r"M:\old", "to": r"N:\new"}
    topic = {"save_path": r"M:\old", "move_pending": dict(move)}  # an undo put the old folder back
    _check_save_path(topic, {"save_path": r"M:\old", "state": "uploading"})
    assert topic["move_pending"] == move  # the client is not at "to": the move is not over

    topic = {"save_path": r"N:\new", "move_pending": dict(move)}
    _check_save_path(topic, {"save_path": r"N:\new", "state": "uploading"})
    assert "move_pending" not in topic


def test_a_topic_paused_during_the_check_is_not_added_to_the_client(monkeypatch):
    """The run's copy is from its start: right before changing the client, the current state is asked."""
    save_state({"topics": [dict(TOPIC)]})
    client = FakeClient()
    tracker = _wire_fake_check(monkeypatch, client)
    _during_fetch(monkeypatch, tracker, lambda topic: topic.update(paused=True))
    out = check.run_check(apply=True, notify=False)
    assert client.add_calls == 0
    row = out["results"][0]
    assert (row["status"], row["skipped"]) == (
        "skipped",
        "приостановлено во время проверки — в торрент-клиент ничего не передано",
    )
    topic = load_state()["topics"][0]
    assert topic["paused"] is True
    assert topic["hash"] is None


def test_a_topic_deleted_during_the_check_is_not_added_to_the_client(monkeypatch):
    save_state({"topics": [dict(TOPIC)]})
    client = FakeClient()
    tracker = _wire_fake_check(monkeypatch, client)
    original = tracker.fetch_torrent

    def fetch(*args, **kwargs):
        with persistence_lock():
            save_state({"topics": []})
        return original(*args, **kwargs)

    tracker.fetch_torrent = fetch
    out = check.run_check(apply=True, notify=False)
    assert client.add_calls == 0
    assert out["results"][0]["skipped"].startswith("удалено во время проверки")
    assert load_state()["topics"] == []


def test_a_topic_added_or_deleted_during_the_check_is_respected(monkeypatch):
    save_state({"topics": [dict(TOPIC), {**TOPIC, "id": "gone", "url": "https://tracker/2"}]})
    tracker = _wire_fake_check(monkeypatch, FakeClient())
    original = tracker.fetch_torrent

    def fetch(*args, **kwargs):
        with persistence_lock():
            state = load_state()
            state["topics"] = [t for t in state["topics"] if t["id"] != "gone"]
            if all(t["id"] != "new" for t in state["topics"]):
                state["topics"].append({**TOPIC, "id": "new", "url": "https://tracker/3"})
            save_state(state)
        return original(*args, **kwargs)

    tracker.fetch_torrent = fetch
    check.run_check(apply=True, notify=False)
    assert [t["id"] for t in load_state()["topics"]] == ["t", "new"]


def test_messages_are_staged_with_the_result_and_survive_a_crash(monkeypatch):
    from tow import delivery

    save_state({"topics": [dict(TOPIC)]})
    _wire_fake_check(monkeypatch, FakeClient())

    def crash(*_args, **_kwargs):
        raise SystemExit("TOW stopped before sending")

    real_dispatch = delivery.dispatch
    monkeypatch.setattr(delivery, "dispatch", crash)
    with pytest.raises(SystemExit):
        check.run_check(apply=True, notify=True)
    pending = load_state()["notify_pending"]
    assert len(pending) == 1
    assert "добавлено в торрент-клиент" in pending[0]["text"]

    sent = []
    real_dispatch(lambda *, text, operation_id, topic: sent.append(text.split("\n")[0]) or True)
    assert len(sent) == 1
    assert "добавлено в торрент-клиент" in sent[0]
    assert "notify_pending" not in load_state()


def test_a_second_client_that_is_down_is_reported(monkeypatch):
    save_state({"topics": [{**TOPIC, "client_id": "nas"}]})
    _wire_fake_check(monkeypatch, FakeClient())
    main = FakeClient()

    class Down(FakeClient):
        def ping(self):
            raise RuntimeError("Transmission: нет связи")

    monkeypatch.setattr(
        check.client_factory,
        "from_secrets",
        lambda cfg, secrets, client_id=None: Down() if client_id == "nas" else main,
    )
    monkeypatch.setattr(client_ops, "_client_title", lambda cfg, client_id: "NAS")
    sent = []
    monkeypatch.setattr(check_notices, "_audited_send", lambda _s, *, text, **_k: sent.append(text) or True)
    check.run_check(apply=True, notify=True)
    assert "Торрент-клиент «NAS» недоступен" in sent
    assert load_state()["health"]["clients_ok"]["nas"] is False

    sent.clear()
    check.run_check(apply=True, notify=True)
    assert "Торрент-клиент «NAS» недоступен" not in sent  # once, not on every run


def test_event_text_names_the_client():
    assert event_text(title="", kind="qbit_down") == "Торрент-клиент недоступен"
    assert event_text(title="NAS", kind="qbit_down") == "Торрент-клиент «NAS» недоступен"
    assert event_text(title="NAS", kind="qbit_up") == "Торрент-клиент «NAS» снова доступен"


def test_an_unconfirmed_add_in_new_wording_is_recovered():
    from tow.check_steps import is_owned_add_recovery

    for error in (
        "торрент-клиент: добавление не подтверждено read-back проверкой",
        "Transmission: клиент принял раздачу, но не показывает её (read-back)",
        "Deluge: клиент не сохранил метку tow (read-back)",
        "Transmission: метка tow-pending не снята (read-back)",
        "qBit ownership was not confirmed after add",
    ):
        assert is_owned_add_recovery({"last_error": error}), error
    assert not is_owned_add_recovery({"last_error": "rutor: all hosts failed"})


def test_a_pass_without_history_changes_skips_the_two_file_journal(monkeypatch):
    from tow import check_transaction

    save_state({"topics": [dict(TOPIC)]})
    _wire_fake_check(monkeypatch, FakeClient())
    journals = []
    real = check_transaction.check_store_transaction

    def counting():
        journals.append(1)
        return real()

    monkeypatch.setattr(check.check_transaction, "check_store_transaction", counting)
    check.run_check(apply=True, notify=False)  # nothing new in the history
    assert journals == []
    assert load_state()["health"]["at_ts"]  # the state was still committed

    real_reconcile = check._reconcile_all

    def records_history(state, results, run, history, records, want):
        real_reconcile(state, results, run, history, records, want)
        history.setdefault("topics", {})["t"] = {"items": {"x": {"label": "S01E01"}}}

    monkeypatch.setattr(check, "_reconcile_all", records_history)
    check.run_check(apply=True, notify=False)
    assert journals == [1]  # a history change goes through the two-file journal
    from tow.store import load_download_history

    assert load_download_history()["topics"]["t"]["items"]["x"]["label"] == "S01E01"


def test_reconcile_does_not_hold_the_data_lock(monkeypatch):
    """Client and file checks (a sleeping NAS) run outside the lock the web UI waits on (H1)."""
    from tow.store import load_download_history, save_download_history

    save_state({"topics": [{**TOPIC, "hash": "HASH-NEW"}, {**TOPIC, "id": "other", "hash": "H2"}]})
    save_download_history({"schema_version": 1, "topics": {"other": {"items": {"old": {"label": "S01E01"}}}}})
    _wire_fake_check(monkeypatch, FakeClient())
    seen = {}

    def reconcile(topic, adapter, history, now):
        if topic["id"] == "t":
            seen["free"] = _lock_free_within(2.0)
            with persistence_lock():  # meanwhile the owner renames it, a restore rewrites "other"
                state = load_state()
                state["topics"][0]["title"] = "Переименовано"
                save_state(state)
                disk = load_download_history()
                disk["topics"]["other"] = {"items": {"restored": {"label": "S02E01"}}}
                save_download_history(disk)
            history.setdefault("topics", {})["t"] = {"items": {"x": {"label": "S01E05"}}}
        return {"events": [], "summary": {}}

    monkeypatch.setattr(check, "reconcile_topic", reconcile)
    check.run_check(apply=True, notify=False)
    assert seen == {"free": True}
    assert load_state()["topics"][0]["title"] == "Переименовано"
    topics = load_download_history()["topics"]
    assert topics["t"]["items"]["x"]["label"] == "S01E05"  # the reconcile's change landed
    assert topics["other"]["items"] == {"restored": {"label": "S02E01"}}  # and the other write was kept


def test_a_corrupt_download_history_does_not_block_the_check(monkeypatch):
    """The history is rebuildable: the check goes on with an empty one, keeps the damaged file
    aside and says so on Home, instead of failing every run."""
    from tow.paths import download_history_path
    from tow.store import load_download_history
    from tow.web.views import attention

    save_state({"topics": [{**TOPIC, "hash": "HASH-NEW"}]})
    download_history_path().write_text("{ not json", encoding="utf-8")
    _wire_fake_check(monkeypatch, FakeClient())

    def reconcile(topic, adapter, history, now):
        history.setdefault("topics", {})[topic["id"]] = {"items": {}, "last_scan_at": now}
        return {"events": [], "summary": {}}

    monkeypatch.setattr(check, "reconcile_topic", reconcile)
    out = check.run_check(apply=True, notify=False)
    assert out["results"][0]["ok"] is True
    assert list(download_history_path().parent.glob("download_history.json.corrupt-*"))  # kept aside
    assert load_download_history()["topics"]["t"]["items"] == {}  # a new history is written
    health = load_state()["health"]
    assert health["history_rebuilt_at"] == health["at_ts"]
    from tow.config import load_config

    assert any("download_history.json.corrupt" in item for item in attention(load_state(), load_config()))

    check.run_check(apply=True, notify=False)  # the next run reads the new file; the notice stays a while
    assert load_state()["health"]["history_rebuilt_at"] == health["history_rebuilt_at"]


def test_an_applying_check_prunes_the_history_and_writes_it_once(monkeypatch):
    from tow.store import load_download_history, save_download_history

    save_state({"topics": []})
    save_download_history(
        {
            "schema_version": 1,
            "topics": {
                "deleted-long-ago": {"last_scan_at": "2020-01-01T00:00:00+00:00", "items": {}},
                "deleted-lately": {"last_scan_at": check.iso_now(), "items": {}},
            },
        }
    )
    _wire_fake_check(monkeypatch, FakeClient())
    writes = []
    real = check.save_download_history
    monkeypatch.setattr(check, "save_download_history", lambda data: writes.append(1) or real(data))
    check.run_check(apply=True, notify=False)
    assert set(load_download_history()["topics"]) == {"deleted-lately"}
    assert writes == [1]
    check.run_check(apply=True, notify=False)  # nothing left to drop: no history write
    assert writes == [1]


def test_a_history_quarantined_earlier_with_nothing_to_record_is_still_written(monkeypatch):
    from tow.paths import download_history_path

    save_state({"topics": []})
    download_history_path().with_name("download_history.json.corrupt-20261001-1-1").write_text("x", encoding="utf-8")
    _wire_fake_check(monkeypatch, FakeClient())
    check.run_check(apply=True, notify=False)
    assert download_history_path().is_file()  # the next run reads it without a warning


def test_merge_history_takes_only_what_the_reconcile_changed():
    from tow.check import _merge_history

    seen = {"schema_version": 1, "topics": {"a": {"n": 1}, "b": {"n": 1}, "gone": {"n": 1}}}
    reconciled = {"schema_version": 1, "topics": {"a": {"n": 2}, "b": {"n": 1}, "new": {"n": 1}}}
    disk = {"schema_version": 1, "topics": {"a": {"n": 1}, "b": {"n": 9}, "gone": {"n": 1}, "c": {"n": 5}}}
    _merge_history(disk, seen, reconciled)
    assert disk["topics"] == {"a": {"n": 2}, "b": {"n": 9}, "new": {"n": 1}, "c": {"n": 5}}


def test_a_new_scan_time_alone_does_not_count_as_a_history_change():
    import copy

    from tow.check import _merge_history

    before = {"topics": {"t": {"last_scan_at": "10:00", "items": {"a": {"status": "completed"}}}}}
    after = {"topics": {"t": {"last_scan_at": "10:30", "items": {"a": {"status": "completed"}}}}}
    changed = {"topics": {"t": {"last_scan_at": "10:30", "items": {"a": {"status": "missing"}}}}}
    disk = copy.deepcopy(before)
    assert _merge_history(disk, before, after) is False
    assert disk == after  # the new scan time is kept, it just does not force a write
    assert _merge_history(copy.deepcopy(before), before, changed) is True


def test_what_the_merge_reports_as_a_change():
    import copy

    from tow.check import _merge_history

    seen = {"schema_version": 1, "topics": {"a": {"n": 1}, "gone": {"n": 1}}}
    # A topic dropped by the reconcile but already gone from the disk: nothing changes.
    assert _merge_history({"schema_version": 1, "topics": {"a": {"n": 1}}}, seen, {"topics": {"a": {"n": 1}}}) is False
    # The same record written meanwhile: nothing changes.
    disk = {"schema_version": 1, "topics": {"a": {"n": 2}, "gone": {"n": 1}}}
    assert _merge_history(copy.deepcopy(disk), seen, {"schema_version": 1, "topics": disk["topics"]}) is False
    # A new topic, a removed one, a new top-level value: changes.
    assert _merge_history(copy.deepcopy(seen), seen, {**seen, "topics": {**seen["topics"], "b": {}}}) is True
    assert _merge_history(copy.deepcopy(seen), seen, {**seen, "topics": {"a": {"n": 1}}}) is True
    assert _merge_history(copy.deepcopy(seen), seen, {**seen, "schema_version": 2}) is True
