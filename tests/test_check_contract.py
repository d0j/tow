from pathlib import Path
from types import SimpleNamespace
from typing import ClassVar

import pytest

from tow import check, check_transaction
from tow.check import rows as check_rows
from tow.notify import event_text
from tow.store import (
    StoreCorruptionError,
    load_download_history,
    load_state,
    persistence_lock,
    save_download_history,
    save_secrets,
    save_state,
)
from tow.torrent import TorrentFile


class FakeTracker:
    name = "fake"
    fetch_calls = 0

    def fetch_torrent(self, url, secrets, ua, *, ignore_cool=False, persist=True):
        self.fetch_calls += 1
        self.persist = persist
        return b"torrent"

    def fetch_title(self, url, secrets, ua, *, ignore_cool=False, persist=True):
        self.title_persist = persist
        return "Show [01x01-05 из 5]"


class FakeClient:
    client_id = "main"
    client_kind = "fake"
    capabilities: ClassVar[dict[str, bool]] = {
        "inspect": True,
        "add": True,
        "stopped_add": True,
        "file_selection": True,
        "priority_readback": True,
        "start_stop": True,
    }

    def __init__(self, *, add_error=None):
        self.present = False
        self.add_error = add_error
        self.add_calls = 0
        self.selected_indices = ()
        self.configure_calls = 0

    def ping(self):
        return "ok"

    def has_hash(self, infohash):
        return self.present

    def add_torrent_selected(self, content, save_path, infohash, selected_indices):
        self.add_calls += 1
        self.selected_indices = tuple(selected_indices)
        if self.add_error:
            raise RuntimeError(self.add_error)
        self.present = True
        return self.inspect_torrent(infohash)

    def configure_torrent_selection(self, content, infohash, selected_indices, *, ensure_started=False):
        self.configure_calls += 1
        self.ensure_started = ensure_started
        if self.add_error:
            raise RuntimeError(self.add_error)
        return self.inspect_torrent(infohash)

    def inspect_torrent(self, infohash):
        if not self.present:
            return None
        return {
            "hash": infohash,
            "save_path": r"M:\\TV",
            "tags": ["tow"],
            "files": [{"index": 0, "name": "Show.mkv", "size": 1, "priority": 1}],
        }


def _wire_fake_check(monkeypatch, client):
    tracker = FakeTracker()
    cfg = {"trackers": {}, "client": {"id": "main", "kind": "fake"}}
    monkeypatch.setattr(check, "load_config", lambda: cfg)
    monkeypatch.setattr(check, "load_trackers", lambda cfg: {"fake": tracker})
    monkeypatch.setattr(check, "match_tracker", lambda trackers, url: tracker)
    monkeypatch.setattr(
        check,
        "parse_torrent_metadata",
        lambda blob: SimpleNamespace(
            infohash="HASH-NEW",
            client_hash="HASH-NEW",
            name="Show",
            files=(TorrentFile(0, "Show.mkv", 1),),
        ),
    )
    monkeypatch.setattr(check.client_factory, "default_client_id", lambda cfg: "main")
    monkeypatch.setattr(check.client_factory, "from_secrets", lambda cfg, secrets, client_id=None: client)
    monkeypatch.setattr(check, "reconcile_topic", lambda *args, **kwargs: {"events": [], "summary": {}})
    return tracker


def test_pending_add_retry_is_recorded_as_recovered_add(monkeypatch):
    save_state(
        {
            "topics": [
                {
                    "id": "pending-topic",
                    "title": "Show",
                    "url": "https://tracker/1",
                    "save_path": r"M:\TV",
                    "hash": None,
                    "last_error": "qBit ownership was not confirmed after add",
                }
            ]
        }
    )
    save_download_history({"schema_version": 1, "topics": {}})

    class PendingClient(FakeClient):
        def __init__(self):
            super().__init__()
            self.present = True
            self.pending = True

        def configure_torrent_selection(self, content, infohash, selected_indices, *, ensure_started=False):
            self.configure_calls += 1
            self.ensure_started = ensure_started
            self.pending = False
            return self.inspect_torrent(infohash)

        def inspect_torrent(self, infohash):
            info = super().inspect_torrent(infohash)
            if info is not None and self.pending:
                info["tags"] = ["tow", "tow-pending"]
            return info

    client = PendingClient()
    _wire_fake_check(monkeypatch, client)
    events = []
    monkeypatch.setattr(check, "log_event", lambda kind, **fields: events.append((kind, fields)))

    result = check.run_check(apply=True, notify=False, how="test")

    row = result["results"][0]
    assert row["ok"] is True
    assert row["added"] is True
    assert row["pending_add_recovered"] is True
    assert client.add_calls == 0
    assert client.configure_calls == 1
    assert client.ensure_started is True
    assert load_state()["topics"][0]["hash"] == "HASH-NEW"
    recovered = [fields for kind, fields in events if kind == "client_added"]
    assert recovered
    assert recovered[-1]["recovered"] is True
    assert not any(kind == "client_selection_updated" for kind, _fields in events)


def test_owned_add_retry_without_pending_tag_is_still_recorded_as_recovered_add(
    monkeypatch,
):
    save_state(
        {
            "topics": [
                {
                    "id": "owned-topic",
                    "title": "Show",
                    "url": "https://tracker/1",
                    "save_path": r"M:\TV",
                    "hash": None,
                    "last_error": "qBit ownership was not confirmed after add",
                }
            ]
        }
    )
    save_download_history({"schema_version": 1, "topics": {}})
    client = FakeClient()
    client.present = True
    _wire_fake_check(monkeypatch, client)
    events = []
    monkeypatch.setattr(check, "log_event", lambda kind, **fields: events.append((kind, fields)))

    result = check.run_check(apply=True, notify=False, how="test")

    row = result["results"][0]
    assert row["ok"] is True
    assert row["added"] is True
    assert row["pending_add_recovered"] is False
    assert client.add_calls == 0
    assert client.configure_calls == 1
    assert client.ensure_started is True
    recovered = [fields for kind, fields in events if kind == "client_added"]
    assert recovered
    assert recovered[-1]["recovered"] is True


def test_pending_retry_refuses_save_path_drift_before_reconfiguration(monkeypatch):
    save_state(
        {
            "topics": [
                {
                    "id": "path-drift-topic",
                    "title": "Show",
                    "url": "https://tracker/1",
                    "save_path": r"M:\TV",
                    "hash": None,
                }
            ]
        }
    )
    save_download_history({"schema_version": 1, "topics": {}})

    class WrongPathClient(FakeClient):
        def inspect_torrent(self, infohash):
            info = super().inspect_torrent(infohash)
            if info is not None:
                info["save_path"] = r"D:\wrong"
                info["tags"] = ["tow", "tow-pending"]
            return info

    client = WrongPathClient()
    client.present = True
    _wire_fake_check(monkeypatch, client)

    result = check.run_check(apply=True, notify=False, how="test")

    row = result["results"][0]
    assert row["ok"] is False
    assert row["error_record"]["code"] == "check.save_path_unconfirmed"
    assert client.configure_calls == 0
    assert load_state()["topics"][0]["hash"] is None


def test_tracker_auth_falls_back_to_magnet_metadata_and_normal_add_flow(monkeypatch):
    save_state(
        {
            "topics": [
                {
                    "id": "magnet-topic",
                    "title": "Show [1 из 2]",
                    "url": "https://tracker/1",
                    "save_path": r"M:\TV",
                    "hash": None,
                }
            ]
        }
    )
    save_download_history({"schema_version": 1, "topics": {}})
    client = FakeClient()
    client.capabilities = {**FakeClient.capabilities, "magnet_metadata": True}
    tracker = _wire_fake_check(monkeypatch, client)
    monkeypatch.setattr(
        tracker,
        "fetch_torrent",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("fake: no download link on page")),
    )
    monkeypatch.setattr(
        tracker,
        "fetch_magnet",
        lambda *_args, **_kwargs: ("magnet:?xt=urn:btih:HASH-NEW", "HASH-NEW"),
        raising=False,
    )
    materialized = []

    def materialize(url, path, infohash):
        materialized.append((url, path, infohash))
        client.present = True
        return b"torrent"

    client.materialize_magnet = materialize

    result = check.run_check(apply=True, notify=False, ids=["magnet-topic"], how="manual")

    row = result["results"][0]
    assert row["ok"] is True
    assert row["source"] == "magnet"
    assert row["fallback_reason"] == "tracker_auth"
    assert row["added"] is True
    assert row["selection_updated"] is False
    assert materialized == [("magnet:?xt=urn:btih:HASH-NEW", r"M:\TV", "HASH-NEW")]
    assert load_state()["topics"][0]["hash"] == "HASH-NEW"


@pytest.mark.parametrize("apply", [False, True])
def test_existing_hash_uses_matching_page_magnet_when_torrent_link_disappears(monkeypatch, apply):
    saved_hash = "A" * 40
    topic = {
        "id": "existing-magnet",
        "title": "Show",
        "url": "https://tracker/1",
        "save_path": r"M:\TV",
        "hash": saved_hash,
    }
    save_state({"topics": [topic]})
    save_download_history({"schema_version": 1, "topics": {}})
    client = FakeClient()
    client.present = True
    tracker = _wire_fake_check(monkeypatch, client)
    monkeypatch.setattr(
        tracker,
        "fetch_torrent",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("fake: no download link on page")),
    )
    magnet_calls = []

    def fetch_magnet(*_args, **kwargs):
        magnet_calls.append(kwargs["persist"])
        return f"magnet:?xt=urn:btih:{saved_hash}", saved_hash

    monkeypatch.setattr(tracker, "fetch_magnet", fetch_magnet, raising=False)
    logged_events = []
    monkeypatch.setattr(check, "log_event", lambda kind, **fields: logged_events.append((kind, fields)))
    result = check.run_check(apply=apply, notify=False, ids=["existing-magnet"], how="manual")
    row = result["results"][0]
    assert row["ok"] is True
    assert row["source"] == "matching_magnet"
    assert row["hash"] == saved_hash
    assert row["fallback_reason"] == "tracker_auth"
    assert client.add_calls == 0
    assert client.configure_calls == 0
    assert magnet_calls == [apply]
    assert load_state()["topics"][0]["hash"] == saved_hash
    fallback_events = [
        fields
        for kind, fields in logged_events
        if kind == "tracker_checked" and fields.get("source") == "matching_magnet"
    ]
    assert [event["fallback_reason"] for event in fallback_events] == (["tracker_auth"] if apply else [])


def test_different_page_magnet_cannot_hide_missing_torrent_link(monkeypatch):
    saved_hash = "A" * 40
    save_state(
        {
            "topics": [
                {
                    "id": "changed-magnet",
                    "title": "Show",
                    "url": "https://tracker/1",
                    "save_path": r"M:\TV",
                    "hash": saved_hash,
                }
            ]
        }
    )
    save_download_history({"schema_version": 1, "topics": {}})
    client = FakeClient()
    client.present = True
    tracker = _wire_fake_check(monkeypatch, client)
    monkeypatch.setattr(
        tracker,
        "fetch_torrent",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("fake: no download link on page")),
    )
    monkeypatch.setattr(
        tracker,
        "fetch_magnet",
        lambda *_args, **_kwargs: ("magnet:?xt=urn:btih:" + "B" * 40, "B" * 40),
        raising=False,
    )
    result = check.run_check(apply=False, notify=False, ids=["changed-magnet"], how="manual")
    assert result["results"][0]["ok"] is False
    assert "no download link" in result["results"][0]["error"]
    assert client.add_calls == 0


@pytest.mark.parametrize(("verified", "dirty"), [(False, False), (True, True)])
@pytest.mark.parametrize("apply", [False, True])
def test_matching_magnet_cannot_skip_unverified_partial_selection(monkeypatch, verified, dirty, apply):
    saved_hash = "A" * 40
    save_state(
        {
            "topics": [
                {
                    "id": "partial-magnet",
                    "title": "Show",
                    "url": "https://tracker/1",
                    "save_path": r"M:\TV",
                    "hash": saved_hash,
                    "selection": {"mode": "episodes", "value": "S01E01"},
                    "selection_hash": saved_hash,
                    "selection_verified": verified,
                    "selection_dirty": dirty,
                }
            ]
        }
    )
    save_download_history({"schema_version": 1, "topics": {}})
    client = FakeClient()
    client.present = True
    client.capabilities = {**FakeClient.capabilities, "magnet_metadata": True}
    materialize_calls = []
    client.materialize_magnet = lambda *_args: materialize_calls.append(True)
    tracker = _wire_fake_check(monkeypatch, client)
    monkeypatch.setattr(
        tracker,
        "fetch_torrent",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("fake: no download link on page")),
    )
    monkeypatch.setattr(
        tracker,
        "fetch_magnet",
        lambda *_args, **_kwargs: (f"magnet:?xt=urn:btih:{saved_hash}", saved_hash),
        raising=False,
    )
    result = check.run_check(apply=apply, notify=False, ids=["partial-magnet"], how="manual")
    assert result["results"][0]["ok"] is False
    assert result["results"][0]["error_record"]["code"] == "check.link_unavailable_selection"
    assert materialize_calls == []
    assert client.add_calls == 0
    assert client.configure_calls == 0


@pytest.mark.parametrize("saved_hash", [("B" * 64)[:40], "B" * 64])
def test_existing_v2_magnet_matches_full_or_client_hash(monkeypatch, saved_hash):
    v2 = "B" * 64
    save_state(
        {
            "topics": [
                {
                    "id": "v2-magnet",
                    "title": "Show",
                    "url": "https://tracker/1",
                    "save_path": r"M:\TV",
                    "hash": saved_hash,
                }
            ]
        }
    )
    save_download_history({"schema_version": 1, "topics": {}})
    client = FakeClient()
    client.present = True
    tracker = _wire_fake_check(monkeypatch, client)
    monkeypatch.setattr(
        tracker,
        "fetch_torrent",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("fake: no download link on page")),
    )
    magnet_url = f"magnet:?xt=urn:btmh:1220{v2}"
    monkeypatch.setattr(tracker, "fetch_magnet", lambda *_args, **_kwargs: (magnet_url, v2), raising=False)
    result = check.run_check(apply=False, notify=False, ids=["v2-magnet"], how="manual")
    assert result["results"][0]["ok"] is True
    assert result["results"][0]["source"] == "matching_magnet"


def test_legacy_v1_hybrid_magnet_requires_client_v2_confirmation(monkeypatch):
    v1, v2 = "A" * 40, "B" * 64
    save_state(
        {
            "topics": [
                {
                    "id": "hybrid-magnet",
                    "title": "Show",
                    "url": "https://tracker/1",
                    "save_path": r"M:\TV",
                    "hash": v1,
                }
            ]
        }
    )
    save_download_history({"schema_version": 1, "topics": {}})

    class HybridClient(FakeClient):
        current_v2 = v2

        def inspect_torrent(self, infohash):
            return {**super().inspect_torrent(infohash), "infohash_v1": v1, "infohash_v2": self.current_v2}

    client = HybridClient()
    client.present = True
    tracker = _wire_fake_check(monkeypatch, client)
    monkeypatch.setattr(
        tracker,
        "fetch_torrent",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("fake: no download link on page")),
    )
    magnet_url = f"magnet:?xt=urn:btih:{v1}&xt=urn:btmh:1220{v2}"
    monkeypatch.setattr(tracker, "fetch_magnet", lambda *_args, **_kwargs: (magnet_url, v2), raising=False)
    matched = check.run_check(apply=False, notify=False, ids=["hybrid-magnet"], how="manual")
    assert matched["results"][0]["ok"] is True
    client.current_v2 = "C" * 64
    mismatched = check.run_check(apply=False, notify=False, ids=["hybrid-magnet"], how="manual")
    assert mismatched["results"][0]["ok"] is False


def test_reconciliation_failure_isolated_per_topic(monkeypatch):
    save_state(
        {
            "topics": [
                {
                    "id": "topic-1",
                    "title": "Show 1",
                    "url": "https://tracker/1",
                    "save_path": r"M:\\TV",
                    "hash": "OLD1",
                },
                {
                    "id": "topic-2",
                    "title": "Show 2",
                    "url": "https://tracker/2",
                    "save_path": r"M:\\TV",
                    "hash": "OLD2",
                },
            ]
        }
    )
    save_download_history({"schema_version": 1, "topics": {}})
    client = FakeClient()
    _wire_fake_check(monkeypatch, client)
    calls = 0

    def reconcile(*args, **kwargs):
        nonlocal calls
        calls += 1
        if calls == 1:
            raise RuntimeError("inspect failed")
        return {"events": [], "summary": {}}

    monkeypatch.setattr(check, "reconcile_topic", reconcile)

    out = check.run_check(apply=True, notify=False, how="test")

    assert len(out["results"]) == 2
    assert out["results"][0]["ok"] is False
    assert "inspect failed" in out["results"][0]["error"]
    assert out["results"][1]["ok"] is True
    assert load_state()["health"]["at"]


def test_event_text_is_short_series_tracker_episode():
    title = "Сериал Г / Show G [04x01-07 из 24] (2026) WEBRip"
    assert event_text(title=title, kind="new_file", tracker="rutor") == (
        "Сериал Г — Rutor — S04E01–07 из 24 — новые серии найдены"
    )
    assert "S01E12" not in event_text(title="Show [Серии 1-12 из 24]", kind="new_file", tracker="rutor")


def test_check_aggregates_file_notifications_per_topic(monkeypatch):
    save_state(
        {
            "topics": [
                {
                    "id": "topic-1",
                    "title": "Show [01x01-05 из 5]",
                    "url": "https://tracker/1",
                    "save_path": r"M:\\TV",
                    "hash": "OLD",
                }
            ],
        }
    )
    save_download_history({"schema_version": 1, "topics": {}})
    client = FakeClient()
    _wire_fake_check(monkeypatch, client)
    monkeypatch.setattr(
        check,
        "reconcile_topic",
        lambda *args, **kwargs: {"events": ["new_file"] * 5, "summary": {}},
    )
    sent = []
    monkeypatch.setattr(
        check,
        "_audited_send",
        lambda secrets, *, text, operation_id, topic=None, how="auto": sent.append(text.split("\n")[0]) or True,
    )

    out = check.run_check(apply=True, notify=True, how="test")

    assert out["results"][0]["ok"] is True
    assert sent == ["Show — Fake — S01E01–05 из 5 — новые серии найдены"]
    assert load_state()["topics"][0]["tracker_title"] == "Show [01x01-05 из 5]"


def test_dry_run_is_preview_without_authoritative_writes(monkeypatch, tmp_path: Path):
    save_state(
        {
            "topics": [
                {
                    "id": "topic-1",
                    "title": "Show",
                    "url": "https://tracker/1",
                    "save_path": r"M:\\TV",
                    "hash": None,
                }
            ],
            "mirrors": {"fake": {"active": "https://mirror", "fail": {}}},
        }
    )
    save_download_history({"schema_version": 1, "topics": {}})
    state_before = Path(load_state.__globals__["state_path"]()).read_bytes()
    history_before = Path(load_download_history.__globals__["download_history_path"]()).read_bytes()
    client = FakeClient()
    tracker = _wire_fake_check(monkeypatch, client)
    log_calls = []
    monkeypatch.setattr(check, "log_event", lambda *args, **kwargs: log_calls.append((args, kwargs)))
    monkeypatch.setattr(check, "save_state", lambda state: (_ for _ in ()).throw(AssertionError("dry-run saved state")))
    monkeypatch.setattr(
        check,
        "save_download_history",
        lambda history: (_ for _ in ()).throw(AssertionError("dry-run saved history")),
    )

    out = check.run_check(apply=False, notify=True, how="test")

    assert out["results"][0]["ok"] is True
    assert out["results"][0]["hash"] == "HASH-NEW"
    assert tracker.persist is False
    assert client.add_calls == 0
    assert log_calls == []
    assert Path(load_state.__globals__["state_path"]()).read_bytes() == state_before
    assert Path(load_download_history.__globals__["download_history_path"]()).read_bytes() == history_before


def test_apply_add_failure_is_reported_as_failure(monkeypatch):
    save_state(
        {
            "topics": [
                {
                    "id": "topic-1",
                    "title": "Show",
                    "url": "https://tracker/1",
                    "save_path": r"M:\\TV",
                    "hash": "OLD",
                }
            ],
        }
    )
    save_download_history({"schema_version": 1, "topics": {}})
    client = FakeClient(add_error="qbit rejected add")
    _wire_fake_check(monkeypatch, client)
    out = check.run_check(apply=True, notify=False, how="test")

    row = out["results"][0]
    topic = load_state()["topics"][0]
    assert row["ok"] is False
    assert row["status"] == "failed"
    assert "qbit rejected add" in row["error"]
    assert topic["last_ok"] is False
    assert topic["hash"] == "OLD"


def test_apply_add_requires_client_readback(monkeypatch):
    save_state(
        {
            "topics": [
                {
                    "id": "topic-1",
                    "title": "Show",
                    "url": "https://tracker/1",
                    "save_path": r"M:\\TV",
                    "hash": None,
                }
            ],
        }
    )
    save_download_history({"schema_version": 1, "topics": {}})
    client = FakeClient()
    _wire_fake_check(monkeypatch, client)
    out = check.run_check(apply=True, notify=False, how="test")

    row = out["results"][0]
    assert row["ok"] is True
    assert row["added"] is True
    assert client.add_calls == 1


def test_apply_store_failure_restores_history_and_state(monkeypatch):
    save_state(
        {
            "topics": [
                {
                    "id": "topic-1",
                    "title": "Show",
                    "url": "https://tracker/1",
                    "save_path": r"M:\\TV",
                    "hash": None,
                }
            ],
        }
    )
    save_download_history({"schema_version": 1, "topics": {}})
    client = FakeClient()
    _wire_fake_check(monkeypatch, client)

    def reconcile(_topic, _client, history, **_kwargs):
        history.setdefault("topics", {})["topic-1"] = {"items": {"new": {"completed": True}}}
        return {"events": [], "summary": {}}

    monkeypatch.setattr(check, "reconcile_topic", reconcile)
    state_file = Path(load_state.__globals__["state_path"]())
    history_file = Path(load_download_history.__globals__["download_history_path"]())
    state_before = state_file.read_bytes()
    history_before = history_file.read_bytes()
    monkeypatch.setattr(check, "save_state", lambda _state: (_ for _ in ()).throw(OSError("state store unavailable")))

    with pytest.raises(OSError, match="state store unavailable"):
        check.run_check(apply=True, notify=False, how="test")

    assert state_file.read_bytes() == state_before
    assert history_file.read_bytes() == history_before
    assert not (state_file.parent / ".tow-check-transaction").exists()


def test_recover_check_transaction_restores_history_committed_marker():
    save_state({"topics": [{"id": "topic-1", "hash": "OLD"}]})
    save_download_history({"schema_version": 1, "topics": {}})
    state_file = Path(load_state.__globals__["state_path"]())
    history_file = Path(load_download_history.__globals__["download_history_path"]())
    state_before = state_file.read_bytes()
    history_before = history_file.read_bytes()

    with persistence_lock():  # the check runs under the lock, then its process dies
        transaction = check_transaction._begin_locked()
        save_download_history({"schema_version": 1, "topics": {"topic-1": {"items": {"new": {}}}}})
        transaction.mark_history_committed()

    check_transaction.recover_check_transaction()

    assert state_file.read_bytes() == state_before
    assert history_file.read_bytes() == history_before
    assert not (state_file.parent / ".tow-check-transaction").exists()


def test_blocked_check_records_attempt_for_home_timer(monkeypatch):
    save_state({"topics": []})
    events = []
    monkeypatch.setattr(check, "log_event", lambda *args, **kwargs: events.append((args, kwargs)))

    health = check.record_check_failure("legacy plaintext TOW secrets require explicit migrate")

    saved = load_state()["health"]
    assert health["check_ok"] is False
    assert saved["check_ok"] is False
    assert saved["check_error"] == "secrets_migration_required"
    assert saved["at_ts"] > 0
    assert events == [
        (
            ("check_blocked",),
            {"reason": "secrets_migration_required", "how": "auto"},
        )
    ]


def test_cli_check_secret_failure_records_apply_attempt_but_not_dry_run(monkeypatch, capsys):
    from tow import cli
    from tow.store import SecretStoreError

    monkeypatch.setattr(
        check,
        "run_check",
        lambda **kwargs: (_ for _ in ()).throw(
            SecretStoreError("legacy plaintext TOW secrets require explicit migrate")
        ),
    )
    attempts = []
    monkeypatch.setattr(check, "record_check_failure", lambda error, **_kw: attempts.append(str(error)))

    assert cli.main(["check", "--apply", "--json"]) == 3
    assert attempts == ["legacy plaintext TOW secrets require explicit migrate"]
    assert '"blocked": true' in capsys.readouterr().out

    assert cli.main(["check", "--dry-run", "--json"]) == 3
    assert attempts == ["legacy plaintext TOW secrets require explicit migrate"]


def test_dry_run_does_not_quarantine_corrupt_state():
    state_file = Path(load_state.__globals__["state_path"]())
    state_file.write_bytes(b"{broken")
    before_names = {path.name for path in state_file.parent.iterdir()}

    with pytest.raises(StoreCorruptionError):
        check.run_check(apply=False, notify=False, how="test")

    assert state_file.read_bytes() == b"{broken"
    assert {path.name for path in state_file.parent.iterdir()} == before_names


def test_notification_is_not_sent_before_state_history_commit(monkeypatch):
    save_state(
        {
            "topics": [
                {
                    "id": "topic-1",
                    "title": "Show",
                    "url": "https://tracker/1",
                    "save_path": r"M:\TV",
                    "hash": "OLD",
                }
            ]
        }
    )
    save_download_history({"schema_version": 1, "topics": {}})
    _wire_fake_check(monkeypatch, FakeClient())
    monkeypatch.setattr(
        check,
        "reconcile_topic",
        lambda *args, **kwargs: {"events": ["new_file"], "summary": {}},
    )
    deliveries = []
    logged = []
    monkeypatch.setattr(check, "_audited_send", lambda *args, **kwargs: deliveries.append(kwargs) or True)
    monkeypatch.setattr(check, "log_event", lambda kind, **kwargs: logged.append(kind))
    monkeypatch.setattr(
        check,
        "save_state",
        lambda _state: (_ for _ in ()).throw(OSError("state store unavailable")),
    )

    with pytest.raises(OSError, match="state store unavailable"):
        check.run_check(apply=True, notify=True, how="test")

    assert deliveries == []
    assert "new_file" not in logged


def test_an_added_message_lost_with_a_failed_commit_is_sent_by_the_next_run(monkeypatch):
    """The add was confirmed, then the commit (and the message staged with it) failed: the next
    run finds TOW's own torrent that no topic records and finishes the add - "added" goes out."""
    save_state({"topics": [{"id": "t", "title": "Show", "url": "https://tracker/1", "save_path": r"M:\\TV"}]})
    save_download_history({"schema_version": 1, "topics": {}})
    client = FakeClient()
    _wire_fake_check(monkeypatch, client)
    real_save = check.save_state
    monkeypatch.setattr(check, "save_state", lambda _state: (_ for _ in ()).throw(OSError("disk full")))
    with pytest.raises(OSError, match="disk full"):
        check.run_check(apply=True, notify=True, how="test")
    assert client.present is True  # the client has it...
    assert load_state()["topics"][0].get("hash") is None  # ...TOW does not know

    monkeypatch.setattr(check, "save_state", real_save)
    sent = []
    monkeypatch.setattr(check, "_audited_send", lambda _s, *, text, **_k: sent.append(text.split("\n")[0]) or True)
    out = check.run_check(apply=True, notify=True, how="test")
    assert out["results"][0]["added"] is True
    assert len(sent) == 1
    assert "добавлено в торрент-клиент" in sent[0]
    assert load_state()["topics"][0]["hash"] == "HASH-NEW"


def test_once_mode_stops_tracker_polling_but_keeps_client_reconciliation(monkeypatch):
    save_state(
        {
            "topics": [
                {
                    "id": "topic-once",
                    "title": "Show",
                    "url": "https://tracker/1",
                    "save_path": r"M:\TV",
                    "hash": None,
                    "tracking_mode": "once",
                    "selection": {"mode": "all", "value": ""},
                }
            ]
        }
    )
    save_download_history({"schema_version": 1, "topics": {}})
    client = FakeClient()
    tracker = _wire_fake_check(monkeypatch, client)
    reconciliations = []
    monkeypatch.setattr(
        check,
        "reconcile_topic",
        lambda *args, **kwargs: reconciliations.append(args[0]["id"]) or {"events": [], "summary": {}},
    )

    first = check.run_check(apply=True, notify=False, how="test")
    second = check.run_check(apply=True, notify=False, how="test")

    assert first["results"][0]["added"] is True
    assert second["results"][0]["skip"] == "once_done"
    assert tracker.fetch_calls == 1
    assert reconciliations == ["topic-once", "topic-once"]
    assert load_state()["topics"][0]["once_done"] is True


def test_paused_topic_never_fetches_tracker_even_for_manual_id(monkeypatch):
    save_state(
        {
            "topics": [
                {
                    "id": "topic-paused",
                    "title": "Show",
                    "url": "https://tracker/1",
                    "save_path": r"M:\TV",
                    "hash": "HASH-NEW",
                    "paused": True,
                }
            ]
        }
    )
    save_download_history({"schema_version": 1, "topics": {}})
    client = FakeClient()
    client.present = True
    tracker = _wire_fake_check(monkeypatch, client)
    reconciliations = []
    monkeypatch.setattr(
        check,
        "reconcile_topic",
        lambda *args, **kwargs: reconciliations.append(args[0]["id"]) or {"events": [], "summary": {}},
    )

    result = check.run_check(apply=True, notify=False, ids=["topic-paused"], ignore_cool=True, how="manual")

    assert result["results"][0]["skip"] == "paused"
    assert result["results"][0]["skipped"] == "на паузе"
    assert tracker.fetch_calls == 0
    assert reconciliations == ["topic-paused"]


def test_partial_episode_selection_reaches_client_as_exact_indices(monkeypatch):
    save_state(
        {
            "topics": [
                {
                    "id": "topic-partial",
                    "title": "Show",
                    "url": "https://tracker/1",
                    "save_path": r"M:\TV",
                    "hash": None,
                    "selection": {"mode": "episodes", "value": "S01E02"},
                }
            ]
        }
    )
    save_download_history({"schema_version": 1, "topics": {}})
    client = FakeClient()
    _wire_fake_check(monkeypatch, client)
    monkeypatch.setattr(
        check,
        "parse_torrent_metadata",
        lambda blob: SimpleNamespace(
            infohash="HASH-NEW",
            client_hash="HASH-NEW",
            name="Show",
            files=(
                TorrentFile(0, "Show.S01E01.mkv", 1),
                TorrentFile(1, "Show.S01E02.mkv", 1),
            ),
        ),
    )

    result = check.run_check(apply=True, notify=False, how="test")

    assert result["results"][0]["ok"] is True
    assert client.selected_indices == (1,)
    topic = load_state()["topics"][0]
    assert topic["selected_file_count"] == 1
    assert topic["selected_episode_keys"] == ["episode:s01e02"]


def test_partial_selection_refuses_to_mutate_foreign_existing_torrent(monkeypatch):
    save_state(
        {
            "topics": [
                {
                    "id": "topic-foreign",
                    "title": "Show",
                    "url": "https://tracker/1",
                    "save_path": r"M:\TV",
                    "hash": "OLD",
                    "selection": {"mode": "episodes", "value": "S01E01"},
                }
            ]
        }
    )
    save_download_history({"schema_version": 1, "topics": {}})

    class ForeignClient(FakeClient):
        def inspect_torrent(self, infohash):
            row = super().inspect_torrent(infohash)
            if row:
                row["tags"] = ["manual"]
            return row

    client = ForeignClient()
    client.present = True
    _wire_fake_check(monkeypatch, client)
    monkeypatch.setattr(
        check,
        "parse_torrent_metadata",
        lambda blob: SimpleNamespace(
            infohash="HASH-NEW",
            client_hash="HASH-NEW",
            name="Show",
            files=(TorrentFile(0, "Show.S01E01.mkv", 1),),
        ),
    )

    result = check.run_check(apply=True, notify=False, how="test")

    assert result["results"][0]["ok"] is False
    assert result["results"][0]["error_record"]["code"].startswith("check.not_owned_")
    assert load_state()["topics"][0]["hash"] == "OLD"


def test_successful_selection_update_clears_dirty_flag_on_disk(monkeypatch):
    save_state(
        {
            "topics": [
                {
                    "id": "topic-dirty",
                    "title": "Show",
                    "url": "https://tracker/1",
                    "save_path": r"M:\TV",
                    "hash": "HASH-NEW",
                    "selection": {"mode": "all", "value": ""},
                    "selection_dirty": True,
                }
            ]
        }
    )
    save_download_history({"schema_version": 1, "topics": {}})
    client = FakeClient()
    client.present = True
    _wire_fake_check(monkeypatch, client)

    check.run_check(apply=True, notify=False, how="test")
    check.run_check(apply=True, notify=False, how="test")

    assert client.configure_calls == 1
    assert load_state()["topics"][0]["selection_dirty"] is False


@pytest.mark.parametrize(("old_state", "blocked"), [("downloading", True), ("stoppedDL", False)])
def test_revision_overlap_guard_includes_untagged_old_torrents(monkeypatch, old_state, blocked):
    save_state(
        {
            "topics": [
                {
                    "id": "topic-revision",
                    "title": "Show",
                    "url": "https://tracker/1",
                    "save_path": r"M:\TV",
                    "hash": "OLD",
                    "selection": {"mode": "all", "value": ""},
                }
            ]
        }
    )
    save_download_history({"schema_version": 1, "topics": {}})

    class RevisionClient(FakeClient):
        def __init__(self):
            super().__init__()
            self.hashes = {"OLD"}

        def has_hash(self, infohash):
            return infohash in self.hashes

        def inspect_torrent(self, infohash):
            if infohash not in self.hashes:
                return None
            return {
                "hash": infohash,
                "save_path": r"M:\TV",
                "tags": ["manual"] if infohash == "OLD" else ["tow"],
                "state": old_state if infohash == "OLD" else "downloading",
                "files": [{"index": 0, "name": "Show.mkv", "size": 1, "priority": 1}],
            }

        def add_torrent_selected(self, content, save_path, infohash, selected_indices):
            self.add_calls += 1
            self.hashes.add(infohash)
            return self.inspect_torrent(infohash)

    client = RevisionClient()
    _wire_fake_check(monkeypatch, client)

    result = check.run_check(apply=True, notify=False, how="test")

    assert result["results"][0]["ok"] is (not blocked)
    assert client.add_calls == (0 if blocked else 1)
    if blocked:
        assert result["results"][0]["error_record"]["code"] == "check.previous_revision_active"


@pytest.mark.parametrize("new_size", [10, 11])
def test_revision_overlap_guard_fails_closed_for_sanitized_non_utf_path(monkeypatch, new_size):
    save_state(
        {
            "topics": [
                {
                    "id": "topic-non-utf-revision",
                    "title": "Show",
                    "url": "https://tracker/1",
                    "save_path": r"M:\TV",
                    "hash": "OLD",
                    "selection": {"mode": "all", "value": ""},
                }
            ]
        }
    )
    save_download_history({"schema_version": 1, "topics": {}})

    class SanitizedRevisionClient(FakeClient):
        def has_hash(self, infohash):
            return infohash == "OLD"

        def inspect_torrent(self, infohash):
            if infohash != "OLD":
                return None
            return {
                "hash": "OLD",
                "save_path": r"M:\TV",
                "tags": ["manual"],
                "state": "downloading",
                "files": [
                    {
                        "index": 0,
                        "name": "Show/_____.avi",
                        "size": 10,
                        "priority": 1,
                    }
                ],
            }

    client = SanitizedRevisionClient()
    _wire_fake_check(monkeypatch, client)
    monkeypatch.setattr(
        check,
        "parse_torrent_metadata",
        lambda blob: SimpleNamespace(
            infohash="HASH-NEW",
            client_hash="HASH-NEW",
            name="Show",
            files=(TorrentFile(0, "Серия.avi", new_size),),
        ),
    )

    result = check.run_check(apply=True, notify=False, how="test")

    assert result["results"][0]["ok"] is False
    assert result["results"][0]["error_record"]["code"] == "check.previous_revision_active"


def test_incompatible_topics_cannot_mutate_the_same_client_hash(monkeypatch):
    save_state(
        {
            "topics": [
                {
                    "id": "first",
                    "title": "Show",
                    "url": "https://tracker/1",
                    "save_path": r"M:\TV",
                    "hash": "HASH-NEW",
                    "selected_files": ["Show.mkv"],
                },
                {
                    "id": "second",
                    "title": "Show mirror",
                    "url": "https://tracker/2",
                    "save_path": r"M:\other",
                    "hash": None,
                },
            ]
        }
    )
    save_download_history({"schema_version": 1, "topics": {}})
    client = FakeClient()
    _wire_fake_check(monkeypatch, client)

    result = check.run_check(apply=True, notify=False, ids=["second"], how="test")

    assert result["results"][0]["ok"] is False
    assert result["results"][0]["error_record"]["code"] == "check.hash_claimed"
    assert client.add_calls == 0


def test_overlap_guard_checks_all_previous_revision_hashes(monkeypatch):
    save_state(
        {
            "topics": [
                {
                    "id": "history-overlap",
                    "title": "Show",
                    "url": "https://tracker/1",
                    "save_path": r"M:\TV",
                    "hash": "OLD",
                    "previous_hashes": ["OLDER"],
                }
            ]
        }
    )
    save_download_history({"schema_version": 1, "topics": {}})

    class HistoricalClient(FakeClient):
        def has_hash(self, infohash):
            return infohash in {"OLD", "OLDER"}

        def inspect_torrent(self, infohash):
            if infohash not in {"OLD", "OLDER"}:
                return None
            return {
                "hash": infohash,
                "save_path": r"M:\TV",
                "tags": ["manual"],
                "state": "stoppedDL" if infohash == "OLD" else "downloading",
                "files": [{"index": 0, "name": "Show.mkv", "size": 1, "priority": 1}],
            }

    client = HistoricalClient()
    _wire_fake_check(monkeypatch, client)

    result = check.run_check(apply=True, notify=False, how="test")

    assert result["results"][0]["ok"] is False
    assert result["results"][0]["error_record"]["code"] == "check.previous_revision_active"
    assert client.add_calls == 0


def test_qbit_down_notification_is_sent_only_after_state_commit(monkeypatch):
    save_state({"topics": []})
    save_download_history({"schema_version": 1, "topics": {}})
    cfg = {"trackers": {}, "client": {"id": "main", "kind": "fake"}}
    monkeypatch.setattr(check, "load_config", lambda: cfg)
    monkeypatch.setattr(check, "load_trackers", lambda _cfg: {})
    monkeypatch.setattr(check.client_factory, "default_client_id", lambda _cfg: "main")
    monkeypatch.setattr(
        check.client_factory,
        "from_secrets",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("offline")),
    )
    order = []

    def committed(state):
        save_state(state)
        order.append("commit")

    monkeypatch.setattr(check, "save_state", committed)
    monkeypatch.setattr(
        check,
        "_audited_send",
        lambda *_args, **_kwargs: order.append("send") or True,
    )

    check.run_check(apply=True, notify=True, how="test")

    assert order[-2:] == ["commit", "send"]


def test_repeated_qbit_down_notification_is_suppressed_until_recovery(monkeypatch):
    save_state({"topics": [], "health": {"qbit_ok": False}})
    save_download_history({"schema_version": 1, "topics": {}})
    cfg = {"trackers": {}, "client": {"id": "main", "kind": "fake"}}
    monkeypatch.setattr(check, "load_config", lambda: cfg)
    monkeypatch.setattr(check, "load_trackers", lambda _cfg: {})
    monkeypatch.setattr(check.client_factory, "default_client_id", lambda _cfg: "main")
    monkeypatch.setattr(
        check.client_factory,
        "from_secrets",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("offline")),
    )
    sent = []
    monkeypatch.setattr(
        check,
        "_audited_send",
        lambda *_args, **_kwargs: sent.append(True) or True,
    )

    check.run_check(apply=True, notify=True, how="test")

    assert sent == []


def test_tracker_and_reconcile_failures_are_both_persisted(monkeypatch):
    save_state(
        {
            "topics": [
                {
                    "id": "dual-failure",
                    "title": "Show",
                    "url": "https://tracker/1",
                    "save_path": r"M:\TV",
                    "hash": "OLD",
                }
            ]
        }
    )
    save_download_history({"schema_version": 1, "topics": {}})
    client = FakeClient()
    client.present = True
    tracker = _wire_fake_check(monkeypatch, client)
    monkeypatch.setattr(
        tracker,
        "fetch_torrent",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("tracker failed")),
    )
    monkeypatch.setattr(
        check,
        "reconcile_topic",
        lambda *_args, **_kwargs: (_ for _ in ()).throw(RuntimeError("inspect failed")),
    )

    result = check.run_check(apply=True, notify=False, how="test")

    error = result["results"][0]["error"]
    assert "tracker failed" in error
    assert "inspect failed" in error
    persisted = load_state()["topics"][0]
    assert persisted["last_error"] == error
    assert persisted["last_error_class"] == "error"


def test_hybrid_v1_state_hash_migrates_to_modern_qbit_id_without_readd(monkeypatch):
    old_v1 = "1" * 40
    new_v2_id = "2" * 40
    save_state(
        {
            "topics": [
                {
                    "id": "hybrid-migration",
                    "title": "Show",
                    "url": "https://tracker/1",
                    "save_path": r"M:\\TV",
                    "hash": old_v1,
                }
            ]
        }
    )
    save_download_history({"schema_version": 1, "topics": {}})
    client = FakeClient()
    client.present = True
    _wire_fake_check(monkeypatch, client)
    monkeypatch.setattr(
        check,
        "parse_torrent_metadata",
        lambda _blob: SimpleNamespace(
            infohash=new_v2_id + "3" * 24,
            client_hash=new_v2_id,
            hash_v1=old_v1,
            hash_v2=new_v2_id + "3" * 24,
            name="Show",
            files=(TorrentFile(0, "Show.mkv", 1),),
        ),
    )

    result = check.run_check(apply=True, notify=False, how="test")

    row = result["results"][0]
    topic = load_state()["topics"][0]
    assert row["ok"] is True
    assert row["changed"] is False
    assert row["hash_identity_migrated"] is True
    assert client.add_calls == 0
    assert client.configure_calls == 0
    assert topic["hash"] == new_v2_id
    assert "previous_hashes" not in topic


def test_client_confirmation_accepts_hybrid_v1_alias():
    v1 = "A" * 40
    v2 = "B" * 64

    class HybridClient:
        def has_hash(self, _infohash):
            return True

        def inspect_torrent(self, _infohash):
            return {
                "hash": v2,
                "infohash_v1": v1,
                "infohash_v2": v2,
                "save_path": r"M:\TV",
                "tags": ["tow"],
            }

    client = HybridClient()

    assert check.client_owned_by_tow(client, v1) is True
    assert check._confirm_client_add(client, v1, r"M:\TV", require_tow_ownership=True) is True


@pytest.mark.parametrize(
    ("old_file", "blocked"),
    [("Сериал/Серия 01.avi", False), ("Сериал/Серия 02.avi", True)],
)
def test_revision_overlap_guard_compares_readable_non_ascii_names_exactly(monkeypatch, old_file, blocked):
    save_state(
        {
            "topics": [
                {
                    "id": "topic-cyrillic-revision",
                    "title": "Сериал",
                    "url": "https://tracker/1",
                    "save_path": r"M:\TV",
                    "hash": "OLD",
                    "selection": {"mode": "all", "value": ""},
                }
            ]
        }
    )
    save_download_history({"schema_version": 1, "topics": {}})

    class ReadableRevisionClient(FakeClient):
        def __init__(self):
            super().__init__()
            self.hashes = {"OLD"}

        def has_hash(self, infohash):
            return infohash in self.hashes

        def inspect_torrent(self, infohash):
            if infohash not in self.hashes:
                return None
            return {
                "hash": infohash,
                "save_path": r"M:\TV",
                "tags": ["tow"],
                "state": "uploading" if infohash == "OLD" else "downloading",
                "files": [
                    {
                        "index": 0,
                        "name": old_file if infohash == "OLD" else "Сериал/Серия 02.avi",
                        "size": 10,
                        "priority": 1,
                    }
                ],
            }

        def add_torrent_selected(self, content, save_path, infohash, selected_indices):
            self.add_calls += 1
            self.hashes.add(infohash)
            return self.inspect_torrent(infohash)

    client = ReadableRevisionClient()
    _wire_fake_check(monkeypatch, client)
    monkeypatch.setattr(
        check,
        "parse_torrent_metadata",
        lambda blob: SimpleNamespace(
            infohash="HASH-NEW",
            client_hash="HASH-NEW",
            name="Сериал",
            files=(TorrentFile(0, "Серия 02.avi", 10),),
        ),
    )

    result = check.run_check(apply=True, notify=False, how="test")

    error = result["results"][0].get("error") or ""
    assert (
        (result["results"][0]["error_record"] or {}).get("code") == "check.previous_revision_active"
        if blocked
        else not error
    )
    assert client.add_calls == (0 if blocked else 1)
    if blocked:
        assert "остановите прежний торрент" in error


def test_committed_transaction_with_failed_cleanup_does_not_block_later_checks():
    save_state({"topics": [{"id": "topic-1", "hash": "OLD"}]})
    save_download_history({"schema_version": 1, "topics": {}})
    with persistence_lock():
        transaction = check_transaction._begin_locked()
        save_state({"topics": [{"id": "topic-1", "hash": "NEW"}]})
        transaction.mark_committed()
    # cleanup was interrupted (e.g. antivirus held a file); the user then edits a topic
    save_state({"topics": [{"id": "topic-1", "hash": "NEW", "title": "edited later"}]})
    root = transaction.root

    check_transaction.recover_check_transaction()

    assert load_state()["topics"][0]["title"] == "edited later"
    assert not root.exists()


def test_leftover_atomic_write_temp_file_is_cleaned_not_fatal():
    save_state({"topics": []})
    save_download_history({"schema_version": 1, "topics": {}})
    with persistence_lock():
        transaction = check_transaction._begin_locked()
        root = transaction.root
        (root / ".TRANSACTION.json.abc123.tmp").write_bytes(b"{")

    check_transaction.recover_check_transaction()

    assert not root.exists()


def test_foreign_entry_in_transaction_directory_still_fails_closed():
    save_state({"topics": []})
    save_download_history({"schema_version": 1, "topics": {}})
    with persistence_lock():
        transaction = check_transaction._begin_locked()
        (transaction.root / "unexpected.bin").write_bytes(b"x")

    with pytest.raises(check_transaction.CheckTransactionError, match="unexpected entry"):
        check_transaction.recover_check_transaction()


def test_interrupted_check_is_recovered_before_the_next_writer_not_after_it():
    """A crashed check must not later overwrite an edit made after the crash."""
    save_state({"topics": [{"id": "topic-1", "hash": "OLD", "paused": False}]})
    save_download_history({"schema_version": 1, "topics": {}})
    with persistence_lock():
        transaction = check_transaction._begin_locked()
        save_state({"topics": [{"id": "topic-1", "hash": "HALF-WRITTEN", "paused": False}]})
        root = transaction.root  # the check process dies here, transaction "prepared"

    # The next writer (e.g. the web UI pausing the topic) takes the lock first ...
    with persistence_lock():
        state = load_state()
        state["topics"][0]["paused"] = True
        save_state(state)

    # ... and whatever runs recovery afterwards must not undo that edit.
    check_transaction.recover_check_transaction()

    topic = load_state()["topics"][0]
    assert topic == {"id": "topic-1", "hash": "OLD", "paused": True}
    assert not root.exists()


def test_cookies_saved_by_one_topics_login_are_used_by_the_next_topic(monkeypatch):
    import base64

    from tow.store import load_secrets, save_secrets

    monkeypatch.setenv("TOW_MASTER_KEY", base64.urlsafe_b64encode(b"k" * 32).decode())
    save_secrets({"trackers": {"fake": {"username": "u", "password": "p"}}})
    save_state(
        {
            "topics": [
                {
                    "id": f"t{i}",
                    "title": "Show",
                    "url": f"https://tracker/{i}",
                    "save_path": r"M:\s",
                    "hash": "HASH-NEW",
                }
                for i in (1, 2)
            ]
        }
    )
    save_download_history({"schema_version": 1, "topics": {}})
    tracker = _wire_fake_check(monkeypatch, FakeClient())
    seen_cookies = []

    def fetch_torrent(url, secrets, ua, *, ignore_cool=False, persist=True):
        jar = ((secrets.get("trackers") or {}).get("fake") or {}).get("cookies_by_origin")
        seen_cookies.append(jar)
        if not jar and persist:  # first topic logs in and persists the session
            stored = load_secrets()
            stored["trackers"]["fake"]["cookies_by_origin"] = {"https://tracker:443": {"sid": "fresh"}}
            save_secrets(stored)
        return b"torrent"

    tracker.fetch_torrent = fetch_torrent
    check.run_check(apply=True, notify=False, how="test")

    assert seen_cookies == [None, {"https://tracker:443": {"sid": "fresh"}}]


def test_a_check_decrypts_the_secrets_again_only_after_they_changed(monkeypatch):
    from tow.store import save_secrets

    save_secrets({"trackers": {"fake": {"username": "u", "password": "p"}}})
    save_state(
        {
            "topics": [
                {"id": f"t{i}", "title": "Show", "url": f"https://tracker/{i}", "save_path": r"M:\s", "hash": "H"}
                for i in range(4)
            ]
        }
    )
    save_download_history({"schema_version": 1, "topics": {}})
    tracker = _wire_fake_check(monkeypatch, FakeClient())
    decrypted = []
    original = check.load_secrets
    monkeypatch.setattr(check, "load_secrets", lambda: decrypted.append(1) or original())

    check.run_check(apply=True, notify=False, how="test")
    assert len(decrypted) == 1  # four topics, a fetch and a title each: nothing changed

    def login_on_the_second_topic(url, secrets, ua, *, ignore_cool=False, persist=True):
        if url.endswith("/1"):
            save_secrets({**original(), "fake": {"cookies": {"session": "fresh"}}})
        return b"torrent"

    tracker.fetch_torrent = login_on_the_second_topic
    decrypted.clear()
    check.run_check(apply=True, notify=False, how="test")
    assert len(decrypted) == 2  # at the start, and once after the login saved its cookies


def test_manual_check_does_not_move_the_scheduled_countdown(monkeypatch):
    from fastapi.testclient import TestClient

    from tow.web import app

    save_state({"topics": []})
    save_download_history({"schema_version": 1, "topics": {}})
    _wire_fake_check(monkeypatch, FakeClient())
    clock = {"now": 1000}
    monkeypatch.setattr(check, "machine_now", lambda: SimpleNamespace(timestamp=lambda: clock["now"]))
    monkeypatch.setattr(check_rows, "now", lambda: "t")

    check.run_check(apply=True, notify=False, how="auto")
    clock["now"] = 5000
    check.run_check(apply=True, notify=False, how="manual")

    health = load_state()["health"]
    assert health["at_ts"] == 5000
    assert health["auto_at_ts"] == 1000
    assert TestClient(app).get("/health.json").json()["next_from_ts"] == 1000


def test_blocked_scheduled_check_still_counts_as_the_scheduled_attempt():
    check.record_check_failure(RuntimeError("secrets"), how="auto")
    first = load_state()["health"]
    assert first["auto_at_ts"] == first["at_ts"]
    check.record_check_failure(RuntimeError("secrets"), how="manual")
    assert load_state()["health"]["auto_at_ts"] == first["auto_at_ts"]


def test_cleared_move_pending_is_persisted_by_the_check(monkeypatch):
    save_state(
        {
            "topics": [
                {
                    "id": "moving-topic",
                    "title": "Show",
                    "url": "https://tracker/1",
                    "save_path": r"M:\new",
                    "hash": "HASH-NEW",
                    "move_pending": {"from": r"M:\old", "to": r"M:\new", "since": "2026-10-01T00:00:00+03:00"},
                }
            ]
        }
    )
    save_download_history({"schema_version": 1, "topics": {}})
    _wire_fake_check(monkeypatch, FakeClient())

    def reconcile(topic, *_args, **_kwargs):
        topic.pop("move_pending", None)  # the client finished moving
        return {"events": [], "summary": {}}

    monkeypatch.setattr(check, "reconcile_topic", reconcile)
    check.run_check(apply=True, notify=False, how="test")

    assert "move_pending" not in load_state()["topics"][0]


def _capture_sends(monkeypatch):
    sent: list[str] = []
    # First line only: the topic link (G4) follows on its own line.
    monkeypatch.setattr(
        check, "_audited_send", lambda _secrets, *, text, **_kwargs: sent.append(text.split("\n")[0]) or True
    )
    return sent


def _seed_watched_topic(**fields):
    topic = {"id": "t1", "title": "Show", "url": "https://tracker/1", "save_path": r"M:\TV", "hash": "HASH-NEW"}
    save_state({"topics": [{**topic, **fields}], "health": {"qbit_ok": True}})
    save_download_history({"schema_version": 1, "topics": {}})


class FailingTracker(FakeTracker):
    error = "fake: all hosts failed"

    def fetch_torrent(self, url, secrets, ua, *, ignore_cool=False, persist=True):
        raise RuntimeError(self.error)


def test_a_lasting_error_is_reported_once_and_its_end_is_reported(monkeypatch):
    # B3: every failing topic used to send "Сбой" on every run.
    client = FakeClient()
    client.present = True
    _wire_fake_check(monkeypatch, client)
    failing = FailingTracker()
    monkeypatch.setattr(check, "match_tracker", lambda trackers, url: failing)
    sent = _capture_sends(monkeypatch)
    _seed_watched_topic()

    # A site that does not answer (amber) is reported once it lasted three checks in a row.
    for _ in range(4):
        check.run_check(apply=True, notify=True, how="test")
    assert [text for text in sent if text.startswith("Сбой")] == ["Сбой — Show: fake: all hosts failed"]
    assert load_state()["topics"][0]["error_notified"] is True

    failing.error = "fake: лимит скачиваний на сегодня"  # a different class is news again
    check.run_check(apply=True, notify=True, how="test")
    assert len([text for text in sent if text.startswith("Сбой")]) == 2

    monkeypatch.setattr(check, "match_tracker", lambda trackers, url: FakeTracker())
    check.run_check(apply=True, notify=True, how="test")
    assert sent[-1] == "Show — снова работает"
    assert "error_notified" not in load_state()["topics"][0]
    check.run_check(apply=True, notify=True, how="test")
    assert sent[-1] == "Show — снова работает"
    assert len(sent) == 3


def test_dead_client_sends_one_message_and_reddens_its_topics(monkeypatch):
    # B3/B4: the client was stored before ping(), so every topic failed and notified.
    class DeadClient(FakeClient):
        def ping(self):
            raise ConnectionError("qBittorrent refused the connection")

        def inspect_torrent(self, infohash):
            raise AssertionError("a client that failed ping must not be used")

    _wire_fake_check(monkeypatch, DeadClient())
    sent = _capture_sends(monkeypatch)
    _seed_watched_topic()

    check.run_check(apply=True, notify=True, how="test")

    assert sent == ["Торрент-клиент недоступен"]
    topic = load_state()["topics"][0]
    assert topic["last_ok"] is False
    assert topic["last_error"].startswith("клиент недоступен")
    assert topic["last_error_class"] == "qbit"


def test_client_coming_back_is_reported(monkeypatch):
    client = FakeClient()
    client.present = True
    _wire_fake_check(monkeypatch, client)
    sent = _capture_sends(monkeypatch)
    _seed_watched_topic()
    state = load_state()
    state["health"] = {"qbit_ok": False}
    save_state(state)

    check.run_check(apply=True, notify=True, how="test")

    assert sent == ["Торрент-клиент снова доступен"]


def test_torrent_removed_from_client_makes_the_topic_red(monkeypatch):
    client = FakeClient()
    _wire_fake_check(monkeypatch, client)

    def removed(topic, adapter, history, now):
        history.setdefault("topics", {})[str(topic["id"])] = {"items": {}, "client_present": False}
        return {"events": [], "summary": {}}

    monkeypatch.setattr(check, "reconcile_topic", removed)
    _seed_watched_topic()

    check.run_check(apply=True, notify=False, how="test")

    topic = load_state()["topics"][0]
    assert topic["last_ok"] is False
    assert topic["last_error"] == "торрент удалён из клиента"
    assert topic["last_error_class"] == "qbit"


def test_blocked_check_keeps_the_last_known_client_and_bot_status():
    # B9: a blocked check wrote qbit_ok=False ("связи нет") and so swallowed the next
    # real "торрент-клиент недоступен"; it knows nothing new about either.
    save_state({"topics": [], "health": {"qbit": "4.6 webapi 2.9", "qbit_ok": True, "clients_ok": {"main": True}}})

    check.record_check_failure(RuntimeError("secrets"), how="auto")

    health = load_state()["health"]
    assert (health["qbit_ok"], health["clients_ok"], health["qbit"]) == (True, {"main": True}, "4.6 webapi 2.9")
    assert health["check_ok"] is False


@pytest.mark.parametrize(
    ("argv", "how"), [(["check", "--apply"], "auto"), (["check", "--apply", "--manual"], "manual")]
)
def test_cli_check_says_whether_a_person_ran_it(monkeypatch, argv, how):
    from tow import cli

    seen = {}
    monkeypatch.setattr(check, "run_check", lambda **kwargs: seen.update(kwargs) or {"results": []})

    assert cli.main([*argv, "--json"]) == 0
    assert seen["how"] == how


def test_title_request_uses_cookies_saved_by_a_relogin_during_the_fetch(monkeypatch):
    from tow.store import load_secrets as real_load_secrets

    client = FakeClient()
    tracker = _wire_fake_check(monkeypatch, client)
    secrets_seen_by_title = []

    def relogin_fetch(url, secrets, ua, *, ignore_cool=False, persist=True):
        save_secrets({**real_load_secrets(), "fake": {"cookies": {"session": "fresh"}}})
        return b"torrent"

    def title(url, secrets, ua, *, ignore_cool=False, persist=True):
        secrets_seen_by_title.append(((secrets.get("fake") or {}).get("cookies") or {}).get("session"))
        return "Show"

    monkeypatch.setattr(tracker, "fetch_torrent", relogin_fetch)
    monkeypatch.setattr(tracker, "fetch_title", title)
    save_secrets({"fake": {"cookies": {"session": "stale"}}})
    _seed_watched_topic()

    check.run_check(apply=True, notify=False, how="test")

    assert secrets_seen_by_title == ["fresh"]


def test_hash_label_migration_relabels_history_instead_of_a_new_revision(monkeypatch):
    # B7: items kept the old label, were marked superseded and came back as a false
    # "версия файлов обновлена".
    old_v1 = "1" * 40
    new_v2_id = "2" * 40
    _seed_watched_topic(hash=old_v1)
    save_download_history(
        {
            "schema_version": 1,
            "topics": {"t1": {"items": {"ep1": {"source_hash": old_v1.lower(), "status": "completed"}}}},
        }
    )
    client = FakeClient()
    client.present = True
    _wire_fake_check(monkeypatch, client)
    monkeypatch.setattr(
        check,
        "parse_torrent_metadata",
        lambda _blob: SimpleNamespace(
            infohash=new_v2_id + "3" * 24,
            client_hash=new_v2_id,
            hash_v1=old_v1,
            hash_v2=new_v2_id + "3" * 24,
            name="Show",
            files=(TorrentFile(0, "Show.mkv", 1),),
        ),
    )
    seen_by_reconcile = []

    def reconcile(topic, adapter, history, now):
        seen_by_reconcile.append(history["topics"]["t1"]["items"]["ep1"]["source_hash"])
        return {"events": [], "summary": {}}

    monkeypatch.setattr(check, "reconcile_topic", reconcile)

    result = check.run_check(apply=True, notify=False, how="test")

    assert result["results"][0]["hash_identity_migrated"] is True
    assert seen_by_reconcile == [new_v2_id]
    assert load_download_history()["topics"]["t1"]["items"]["ep1"]["source_hash"] == new_v2_id


@pytest.mark.parametrize(("tracking_mode", "ok"), [("watch", True), ("once", False)])
def test_watched_range_in_the_future_waits_instead_of_failing(monkeypatch, tracking_mode, ok):
    # B8: "S01E11-20" on a torrent with E01-10 errored on every run.
    client = FakeClient()
    _wire_fake_check(monkeypatch, client)
    _seed_watched_topic(hash="", selection={"mode": "episodes", "value": "S01E11-20"}, tracking_mode=tracking_mode)
    monkeypatch.setattr(
        check,
        "parse_torrent_metadata",
        lambda _blob: SimpleNamespace(
            infohash="HASH-NEW",
            client_hash="HASH-NEW",
            name="Show",
            files=tuple(TorrentFile(i, f"Show.S01E{i + 1:02d}.mkv", 1) for i in range(10)),
        ),
    )

    row = check.run_check(apply=True, notify=False, how="test")["results"][0]

    assert row["ok"] is ok
    assert client.add_calls == 0
    topic = load_state()["topics"][0]
    if ok:
        assert row["skipped"].startswith("ожидает серий")
        assert not topic.get("last_error")
        assert not topic.get("hash")
    else:
        assert topic["last_error_code"] == "selection.not_out_yet"


def test_an_add_that_cannot_fit_is_refused_before_the_client(monkeypatch, tmp_path):
    # G6: a torrent larger than the free space was handed to qBittorrent anyway.
    import shutil

    client = FakeClient()
    _wire_fake_check(monkeypatch, client)
    big = TorrentFile(0, "Show.mkv", 50 * 1024**3)
    monkeypatch.setattr(
        check,
        "parse_torrent_metadata",
        lambda _blob: SimpleNamespace(infohash="HASH-NEW", client_hash="HASH-NEW", name="Show", files=(big,)),
    )
    monkeypatch.setattr(shutil, "disk_usage", lambda _p: SimpleNamespace(total=0, used=0, free=10 * 1024**3))
    _seed_watched_topic(hash="", save_path=str(tmp_path))

    row = check.run_check(apply=True, notify=False, how="test")["results"][0]

    assert client.add_calls == 0
    assert row["ok"] is False
    assert row["error"].startswith("мало места на диске: нужно 50,0 ГБ, свободно 10,0 ГБ")
    assert row["error_record"]["params"]["needed"] == 50.0
    assert load_state()["topics"][0]["last_error_class"] == "disk"


def test_a_daily_limit_is_kept_until_the_next_local_day(monkeypatch):
    save_state({"topics": [{"id": "k", "title": "Show", "url": "https://tracker/1", "save_path": r"M:\s"}]})
    save_download_history({"schema_version": 1, "topics": {}})
    tracker = _wire_fake_check(monkeypatch, FakeClient())
    day = {"today": "2026-10-01"}
    monkeypatch.setattr(check, "_today", lambda: day["today"])
    fetches = []

    def limited(*_args, **_kwargs):
        fetches.append(1)
        raise RuntimeError("fake: лимит скачиваний на сегодня")

    tracker.fetch_torrent = limited
    tracker.name = "fake"
    check.run_check(apply=True, notify=False, how="auto")
    assert load_state()["daily_limit"] == {"fake": "2026-10-01"}
    assert len(fetches) == 1

    out = check.run_check(apply=True, notify=False, how="auto")  # the next scheduled run, same day
    assert len(fetches) == 1  # the site is not asked again
    assert out["results"][0]["error"] == "дневной лимит скачиваний исчерпан"

    day["today"] = "2026-10-02"  # after local midnight
    tracker.fetch_torrent = lambda *_a, **_k: b"torrent"
    check.run_check(apply=True, notify=False, how="auto")
    assert "daily_limit" not in load_state()


def test_free_space_counts_only_the_files_not_already_in_the_folder(monkeypatch, tmp_path):
    """M5: a new revision of a season whose first episodes are on disk needs only the rest."""
    import shutil

    (tmp_path / "Show").mkdir()
    (tmp_path / "Show" / "E01.mkv").write_bytes(b"x" * 100)  # already downloaded
    (tmp_path / "E02.mkv").write_bytes(b"x" * 50)  # half of it, in a client layout without a subfolder
    files = (TorrentFile(0, "E01.mkv", 100), TorrentFile(1, "E02.mkv", 100), TorrentFile(2, "E03.mkv", 100))
    free = check.FREE_SPACE_MARGIN + 160
    monkeypatch.setattr(shutil, "disk_usage", lambda _p: SimpleNamespace(total=0, used=0, free=free))

    # 300 bytes selected, 150 of them already there: 150 new bytes fit in 160.
    assert check.free_space_problem(str(tmp_path), files, [0, 1, 2], name="Show", is_multi=True) is None
    # Without the files on disk the same torrent does not fit.
    assert check.free_space_problem(str(tmp_path / "empty"), files, [0, 1, 2], name="Show", is_multi=True)
    # A single-file torrent is the file itself.
    (tmp_path / "Movie.mkv").write_bytes(b"x" * 300)
    single = (TorrentFile(0, "Movie.mkv", 300),)
    assert check.free_space_problem(str(tmp_path), single, [0], name="Movie.mkv") is None


def test_free_space_is_not_checked_when_it_cannot_be_known(monkeypatch):
    import shutil

    monkeypatch.setattr(shutil, "disk_usage", lambda _p: (_ for _ in ()).throw(OSError("unreachable")))
    # The share "exists" without asking the network: a real \\nas lookup takes seconds.
    monkeypatch.setattr("tow.folders.seen_from_here", lambda _p: True)
    monkeypatch.setattr(Path, "exists", lambda _self, **_kw: True)

    assert check.free_space_problem(r"\\nas\media", (TorrentFile(0, "a.mkv", 10**12),), [0]) is None
    assert check.free_space_problem(r"M:\x", (TorrentFile(0, "a.mkv", 0),), [0]) is None


def _season_reconcile(monkeypatch, *, complete, events):
    def reconcile(topic, adapter, history, now):
        history.setdefault("topics", {}).setdefault(str(topic["id"]), {"items": {}})
        summary = {"completed": 12 if complete else 11, "expected": 12, "is_complete": complete}
        return {"events": events, "summary": summary}

    monkeypatch.setattr(check, "reconcile_topic", reconcile)


def test_season_complete_is_announced_once_when_the_last_episode_lands(monkeypatch):
    # G5: "сезон собран 12/12" exactly once, then re-armed if the season grows again.
    client = FakeClient()
    client.present = True
    _wire_fake_check(monkeypatch, client)
    monkeypatch.setattr(check, "match_tracker", lambda trackers, url: FakeTracker())
    sent = _capture_sends(monkeypatch)
    _seed_watched_topic()

    _season_reconcile(monkeypatch, complete=True, events=["episode_completed"])
    check.run_check(apply=True, notify=True, how="test")
    check.run_check(apply=True, notify=True, how="test")
    assert [text for text in sent if "сезон собран" in text] == ["Show — сезон собран 12/12"]

    _season_reconcile(monkeypatch, complete=False, events=[])
    check.run_check(apply=True, notify=True, how="test")
    _season_reconcile(monkeypatch, complete=True, events=["episode_completed"])
    check.run_check(apply=True, notify=True, how="test")
    assert len([text for text in sent if "сезон собран" in text]) == 2


def test_an_already_complete_season_is_marked_silently(monkeypatch):
    client = FakeClient()
    client.present = True
    _wire_fake_check(monkeypatch, client)
    sent = _capture_sends(monkeypatch)
    _seed_watched_topic()
    _season_reconcile(monkeypatch, complete=True, events=[])

    check.run_check(apply=True, notify=True, how="test")

    assert sent == []
    assert load_download_history()["topics"]["t1"]["season_complete_at"]


@pytest.mark.parametrize("stuck", ["TRANSACTION.json", "state.before"])
def test_an_interrupted_cleanup_never_blocks_later_writes(monkeypatch, stuck):
    # Found while raising coverage: an antivirus/sharing violation on TRANSACTION.json
    # after a committed check left a marker without backups, and every later write in
    # every process failed with "check transaction backup is missing".
    from pathlib import Path

    save_state({"topics": [], "mirrors": {}})
    save_download_history({"schema_version": 1, "topics": {}})
    real_unlink = Path.unlink

    def flaky_unlink(self, *args, **kwargs):
        if self.name == stuck:
            raise PermissionError("held by another process")
        return real_unlink(self, *args, **kwargs)

    monkeypatch.setattr(Path, "unlink", flaky_unlink)
    with check_transaction.check_store_transaction() as transaction:
        save_download_history({"schema_version": 1, "topics": {"t": {"items": {}}}})
        transaction.mark_history_committed()
        save_state({"topics": [{"id": "after"}], "mirrors": {}})
        transaction.mark_committed()
    assert transaction.cleanup_pending is True
    monkeypatch.setattr(Path, "unlink", real_unlink)

    save_state({"topics": [{"id": "later"}], "mirrors": {}})  # must not raise

    assert load_state()["topics"] == [{"id": "later"}]


def test_progress_only_pass_never_asks_the_tracker(monkeypatch):
    # G2: the 30-minute pass reads completions from qBittorrent only.
    client = FakeClient()
    client.present = True
    tracker = _wire_fake_check(monkeypatch, client)
    reconciled = []
    monkeypatch.setattr(
        check, "reconcile_topic", lambda topic, *a, **k: reconciled.append(topic["id"]) or {"events": [], "summary": {}}
    )
    _seed_watched_topic(last_check="earlier")

    row = check.run_check(apply=True, notify=False, how="progress", progress_only=True)["results"][0]

    assert tracker.fetch_calls == 0
    assert reconciled == ["t1"]
    assert row["skipped"] == "только прогресс в клиенте"
    assert load_state()["topics"][0]["last_check"] == "earlier"


def test_cli_progress_only_is_its_own_kind_of_run(monkeypatch):
    from tow import cli

    seen = {}
    monkeypatch.setattr(check, "run_check", lambda **kw: seen.update(kw) or {"results": []})

    assert cli.main(["check", "--apply", "--progress-only", "--json"]) == 0
    assert (seen["progress_only"], seen["how"]) == (True, "progress")


def test_preview_does_not_spend_a_daily_download_limit():
    from types import SimpleNamespace

    from tow.check import _download_limited, _skip_row

    kinozal = SimpleNamespace(name="kinozal", spec={})
    row: dict = {}
    assert _skip_row({"url": "u"}, row, kinozal, old="H", quota=set(), state={}, how="manual", apply=False)
    assert row["skipped"] == "предпросмотр не скачивает торрент-файл с сайта с дневным лимитом"
    assert not _skip_row({"url": "u"}, {}, kinozal, old="H", quota=set(), state={}, how="auto", apply=True)
    assert _download_limited(SimpleNamespace(name="rutor", spec={"download_limit": True}))
    assert not _download_limited(SimpleNamespace(name="kinozal", spec={"download_limit": False}))
