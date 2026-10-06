from __future__ import annotations

import base64
import json
from types import SimpleNamespace

import pytest
from bs4 import BeautifulSoup
from fastapi.testclient import TestClient
from test_content import URL, blob

from tow import releases, torrent_cache
from tow.config import ConfigError, load_config, save_config, validated
from tow.errors import TowError
from tow.store import load_state, master_fernet, save_state
from tow.torrent import parse_torrent_metadata
from tow.web import app, services


def watch(url=URL, tid="test-topic"):
    state = load_state()
    state["topics"] = [{"id": tid, "url": url, "title": "Test", "client_id": "main"}]
    save_state(state)


def test_one_state_snapshot_per_cache_operation(monkeypatch):
    watch()
    reads = []
    original = torrent_cache.load_state
    monkeypatch.setattr(torrent_cache, "load_state", lambda **kwargs: reads.append(1) or original(**kwargs))
    torrent_cache.remember(blob(), URL)
    assert len(reads) == 1
    torrent_cache.read(URL)
    assert len(reads) == 2
    torrent_cache.remember(blob(), URL)
    assert len(reads) == 3


def test_drafts_do_not_create_durable_metadata():
    torrent_cache.remember(blob(), URL)
    assert torrent_cache.read(URL) is None
    assert not torrent_cache._folder().exists()


def test_live_metadata_is_encrypted_bound_and_reusable_after_snapshot_expiry(monkeypatch):
    watch()
    torrent_cache.remember(blob(), URL)
    encrypted = torrent_cache._path(URL).read_bytes()
    assert URL.encode() not in encrypted
    assert b"Season" not in encrypted
    assert len(list(torrent_cache._folder().iterdir())) == 1
    monkeypatch.setattr(torrent_cache, "MAX_BYTES", len(encrypted) + 1)
    torrent_cache.remember(blob(), URL)  # replacement does not count the old record twice
    assert torrent_cache.read(URL) == blob()
    assert torrent_cache.read(URL + "2") is None


def test_durable_metadata_outlives_form_tokens(monkeypatch):
    from tow import content

    watch()
    torrent_cache.remember(blob(), URL)
    prepared = content.prepare(blob(), URL, "main")
    monkeypatch.setattr(content, "TTL", -1)
    with pytest.raises(TowError):
        content.read(prepared["token"], URL, "main")
    assert torrent_cache.read(URL) == blob()


def test_unchanged_metadata_does_not_rewrite_the_file(monkeypatch):
    watch()
    torrent_cache.remember(blob(), URL)
    monkeypatch.setattr(
        torrent_cache, "atomic_write_bytes", lambda *_args: pytest.fail("unchanged metadata was rewritten")
    )
    torrent_cache.remember(blob(), URL)


def test_corrupt_regular_record_can_be_repaired_by_an_explicit_fresh_file():
    watch()
    torrent_cache.remember(blob(), URL)
    torrent_cache._path(URL).write_bytes(b"damaged")
    torrent_cache.remember(blob(), URL)
    assert torrent_cache.read(URL) == blob()


def test_delete_route_removes_metadata_only_and_undo_remains_available():
    watch()
    torrent_cache.remember(blob(), URL)
    client = TestClient(app, headers={"Origin": "http://127.0.0.1"})
    result = client.post("/topics/test-topic/delete", follow_redirects=False)
    assert result.status_code == 303
    assert load_state()["topics"] == []
    assert torrent_cache.read(URL) is None
    assert not torrent_cache._path(URL).exists()
    result = client.post("/undo", follow_redirects=False)
    assert result.status_code == 303
    assert load_state()["topics"][0]["url"] == URL


def test_delete_route_keeps_cleanup_failure_visible_without_undoing_deletion(monkeypatch):
    watch()
    torrent_cache.remember(blob(), URL)
    monkeypatch.setattr(services, "forget_cached_content", lambda *_args: (_ for _ in ()).throw(OSError("private")))
    client = TestClient(app, headers={"Origin": "http://127.0.0.1"})
    result = client.post("/topics/test-topic/delete")
    assert result.status_code == 200
    assert load_state()["topics"] == []
    assert TowError("content.cache_delete_failed").text() in result.text
    assert "private" not in result.text


def test_cancel_added_topic_also_removes_its_metadata():
    from tow import undo

    watch()
    torrent_cache.remember(blob(), URL)
    state = load_state()
    undo.stamp(state, "topic_add", id="test-topic")
    save_state(state)
    assert undo.apply().kind == "ok"
    assert load_state()["topics"] == []
    assert not torrent_cache._path(URL).exists()


def test_picker_receives_translated_cache_and_quota_messages():
    page = BeautifulSoup(TestClient(app).get("/").text, "html.parser")
    texts = json.loads(page.select_one("#tow-content-i18n").text)
    for key in ("content.cached_hint", "content.cache_failed", "content.limited_confirm"):
        assert key in texts
        assert texts[key] != key


@pytest.mark.parametrize("value", ["maybe", {}, [], 2])
def test_update_check_preference_rejects_invalid_configuration(value):
    with pytest.raises(ConfigError):
        validated({"check_updates": value})


def test_fresh_metadata_route_has_an_explicit_source_and_no_upload_ambiguity(monkeypatch):
    calls = []
    monkeypatch.setattr(services, "prepare_fresh_content", lambda *args: calls.append(args) or {"cached": False})
    client = TestClient(app, headers={"Origin": "http://127.0.0.1"})
    result = client.post(
        "/content/prepare", data={"url": URL, "client_id": "main", "source": "fresh", "allow_limited": "true"}
    )
    assert result.status_code == 200
    assert calls == [(URL, "main", True)]
    result = client.post(
        "/content/prepare",
        data={"url": URL, "client_id": "main", "source": "fresh"},
        files={"torrent": ("test.torrent", blob())},
    )
    assert result.status_code == 400
    assert calls == [(URL, "main", True)]


def test_cache_directory_links_are_refused(monkeypatch):
    from pathlib import Path

    folder = torrent_cache._folder()
    original = Path.is_junction
    monkeypatch.setattr(Path, "is_junction", lambda path: True if path == folder else original(path))
    with pytest.raises(TowError):
        torrent_cache.read(URL)


def test_cache_replaced_between_stat_and_open_is_refused(monkeypatch):
    watch()
    torrent_cache.remember(blob(), URL)
    original = torrent_cache.os.fstat

    def swapped(fd):
        info = original(fd)
        return SimpleNamespace(st_mode=info.st_mode, st_dev=info.st_dev, st_ino=info.st_ino + 1)

    monkeypatch.setattr(torrent_cache.os, "fstat", swapped)
    with pytest.raises(TowError):
        torrent_cache.read(URL)


def test_validated_tracker_metadata_is_retained_even_if_client_identity_lookup_fails(monkeypatch):
    from tow import check

    watch()
    work = SimpleNamespace(topic={"id": "test-topic"}, run=SimpleNamespace(apply=True), row={}, old="", url=URL)
    monkeypatch.setattr(check, "_fetch_revision", lambda *_args: SimpleNamespace(blob=blob(), magnet_hash=None))
    monkeypatch.setattr(
        check, "resolve_client_hash", lambda *_args: (_ for _ in ()).throw(TowError("content.unavailable"))
    )
    work.client = None
    with pytest.raises(TowError):
        check._check_revision(work)
    assert torrent_cache.read(URL) == blob()


def test_delete_retains_shared_metadata_until_last_watch_is_gone():
    watch()
    torrent_cache.remember(blob(), URL)
    torrent_cache.forget_if_unused(URL)
    assert torrent_cache.read(URL) == blob()
    state = load_state()
    state["topics"] = []
    save_state(state)
    torrent_cache.forget_if_unused(URL)
    assert torrent_cache.read(URL) is None
    torrent_cache.forget_if_unused(URL)


@pytest.mark.parametrize("fault", ["changed_url", "corrupt", "oversize"])
def test_damaged_metadata_is_never_silently_used(fault, monkeypatch):
    watch()
    torrent_cache.remember(blob(), URL)
    path = torrent_cache._path(URL)
    if fault == "corrupt":
        path.write_bytes(b"invalid encrypted data")
    elif fault == "changed_url":
        path.write_bytes(
            master_fernet().encrypt(json.dumps({"url": URL + "2", "blob": base64.b64encode(blob()).decode()}).encode())
        )
    else:
        monkeypatch.setattr(torrent_cache, "MAX_RECORD_BYTES", 1)
    with pytest.raises(TowError):
        torrent_cache.read(URL)


@pytest.mark.parametrize("budget", ["MAX_BYTES", "MAX_RECORDS"])
def test_cache_budget_preserves_old_record(budget, monkeypatch):
    watch()
    torrent_cache.remember(blob(), URL)
    old = torrent_cache._path(URL).read_bytes()
    monkeypatch.setattr(torrent_cache, budget, 0)
    with pytest.raises(TowError) as error:
        torrent_cache.remember(blob().replace(b"4:Show", b"4:Else"), URL)
    assert error.value.code == "content.full"
    assert torrent_cache._path(URL).read_bytes() == old
    with pytest.raises(TowError) as error:
        torrent_cache.read(URL)
    assert error.value.code == "content.cache_invalid"


def test_failed_changed_revision_cannot_reuse_old_contents_even_after_restart(monkeypatch):
    watch()
    torrent_cache.remember(blob(), URL)
    changed = blob().replace(b"4:Show", b"4:Else")
    writer = torrent_cache.atomic_write_bytes
    monkeypatch.setattr(
        torrent_cache, "atomic_write_bytes", lambda *_args: (_ for _ in ()).throw(OSError("cache folder refused"))
    )
    with pytest.raises(OSError, match="cache folder refused"):
        torrent_cache.remember(changed, URL)
    assert load_state()["topics"][0]["metadata_cache_unavailable"] is True
    with pytest.raises(TowError):
        torrent_cache.read(URL)
    monkeypatch.setattr(torrent_cache, "atomic_write_bytes", writer)
    torrent_cache.remember(changed, URL)
    assert "metadata_cache_unavailable" not in load_state()["topics"][0]
    assert torrent_cache.read(URL) == changed


def test_unknown_entries_are_not_deleted():
    watch()
    torrent_cache.remember(blob(), URL)
    foreign = torrent_cache._folder() / "owner.txt"
    foreign.write_bytes(b"owner data")
    with pytest.raises(TowError):
        torrent_cache.remember(blob().replace(b"4:Show", b"4:Else"), URL)
    assert foreign.read_bytes() == b"owner data"


@pytest.mark.parametrize("action", ["torrent", "magnet"])
@pytest.mark.parametrize("fault", ["read_permission", "oversize"])
def test_unreadable_old_copy_stays_refused_after_storage_recovers(action, fault, monkeypatch):
    from pathlib import Path

    watch()
    torrent_cache.remember(blob(), URL)
    path = torrent_cache._path(URL)
    changed = blob().replace(b"4:Show", b"4:Else")
    original = Path.open
    with monkeypatch.context() as blocked:
        if fault == "read_permission":

            def refused(item, *args, **kwargs):
                if item == path:
                    raise PermissionError("metadata temporarily unreadable")
                return original(item, *args, **kwargs)

            blocked.setattr(Path, "open", refused)
        else:
            blocked.setattr(torrent_cache, "MAX_RECORD_BYTES", 1)
        operation, args = (
            (torrent_cache.remember, (changed, URL))
            if action == "torrent"
            else (
                torrent_cache.observe_magnet,
                (URL, "magnet:?xt=urn:btih:" + parse_torrent_metadata(changed).infohash),
            )
        )
        with pytest.raises((TowError, PermissionError)):
            operation(*args)
    assert load_state()["topics"][0]["metadata_cache_unavailable"] is True
    with pytest.raises(TowError) as error:
        torrent_cache.read(URL)
    assert error.value.code == "content.cache_invalid"
    torrent_cache.remember(changed, URL)
    assert torrent_cache.read(URL) == changed


def test_failed_repair_cannot_allow_a_later_restored_old_record(monkeypatch):
    watch()
    torrent_cache.remember(blob(), URL)
    path = torrent_cache._path(URL)
    old = path.read_bytes()
    path.write_bytes(b"damaged")
    changed = blob().replace(b"4:Show", b"4:Else")
    with monkeypatch.context() as blocked:
        blocked.setattr(torrent_cache, "MAX_BYTES", 0)
        with pytest.raises(TowError):
            torrent_cache.remember(changed, URL)
    path.write_bytes(old)
    with pytest.raises(TowError):
        torrent_cache.read(URL)
    torrent_cache.remember(changed, URL)
    assert torrent_cache.read(URL) == changed


def test_changed_magnet_invalidates_old_contents_but_matching_one_keeps_them():
    watch()
    torrent_cache.remember(blob(), URL)
    infohash = parse_torrent_metadata(blob()).infohash
    torrent_cache.observe_magnet(URL, "magnet:?xt=urn:btih:" + infohash)
    assert torrent_cache.read(URL) == blob()
    torrent_cache.observe_magnet(URL, "not a magnet")
    assert torrent_cache.read(URL) == blob()
    torrent_cache.observe_magnet(URL, "magnet:?xt=urn:btih:" + "A" * 40)
    assert torrent_cache.read(URL) is None


def test_matching_magnet_can_revalidate_a_copy_after_a_failed_replacement():
    watch()
    torrent_cache.remember(blob(), URL)
    state = load_state()
    state["topics"][0]["metadata_cache_unavailable"] = True
    save_state(state)
    torrent_cache.observe_magnet(URL, "magnet:?xt=urn:btih:" + parse_torrent_metadata(blob()).infohash)
    assert torrent_cache.read(URL) == blob()
    assert "metadata_cache_unavailable" not in load_state()["topics"][0]


def test_changed_magnet_cannot_reuse_a_locked_old_copy(monkeypatch):
    from pathlib import Path

    watch()
    torrent_cache.remember(blob(), URL)
    path = torrent_cache._path(URL)
    original = Path.unlink

    def refused(item, **kwargs):
        if item == path:
            raise OSError("locked metadata")
        return original(item, **kwargs)

    monkeypatch.setattr(Path, "unlink", refused)
    with pytest.raises(OSError, match="locked metadata"):
        torrent_cache.observe_magnet(URL, "magnet:?xt=urn:btih:" + "A" * 40)
    assert path.exists()
    with pytest.raises(TowError):
        torrent_cache.read(URL)


def wire_tracker(monkeypatch):
    cfg = load_config()
    cfg["clients"] = [{"id": "main", "kind": "qbittorrent", "enabled": True}]
    cfg["default_client_id"] = "main"
    save_config(cfg)
    calls = []
    tracker = SimpleNamespace(
        name="kinozal",
        spec={"download_limit": True},
        fetch_torrent=lambda *_args, **kwargs: calls.append(kwargs) or blob(),
    )
    monkeypatch.setattr("tow.trackers.load_trackers", lambda _cfg: [tracker])
    monkeypatch.setattr("tow.trackers.match_tracker", lambda *_args: tracker)
    return calls


def test_saved_contents_use_no_tracker_quota_and_fresh_request_requires_consent(monkeypatch):
    calls = wire_tracker(monkeypatch)
    watch()
    torrent_cache.remember(blob(), URL)
    result = services.prepare_content(URL, "main", None, False)
    assert result["cached"] is True
    assert services.read_content(result["token"], URL, "main") == blob()
    assert calls == []
    with pytest.raises(TowError) as error:
        services.prepare_fresh_content(URL, "main", False)
    assert error.value.code == "content.limited"
    assert calls == []
    assert services.prepare_fresh_content(URL, "main", True)["cached"] is False
    assert calls == [{"persist": True}]


def test_cache_write_failure_does_not_lose_prepared_selection(monkeypatch):
    wire_tracker(monkeypatch)
    watch()
    monkeypatch.setattr(torrent_cache, "remember", lambda *_args: (_ for _ in ()).throw(OSError("private path")))
    result = services.prepare_fresh_content(URL, "main", True)
    assert result["cache_failed"] is True
    assert services.read_content(result["token"], URL, "main") == blob()


def test_automatic_update_checks_can_be_disabled_and_manual_checks_still_work(monkeypatch):
    calls = []
    monkeypatch.setattr(releases, "_fetch_latest", lambda: calls.append(1) or "99.0.0")
    monkeypatch.setattr(releases, "checker", releases.ReleaseChecker())
    client = TestClient(app, headers={"Origin": "http://127.0.0.1"})
    response = client.post("/settings/updates", data={"enabled": "0"}, follow_redirects=False)
    assert response.status_code == 303
    assert response.headers["location"].startswith("/settings?open=updates")
    assert load_config()["check_updates"] is False
    for _ in range(3):
        assert client.get("/updates.json").json()["checks_enabled"] is False
    assert calls == []
    result = client.post("/updates/check").json()
    assert result["latest"] == "99.0.0"
    assert result["checks_enabled"] is False
    assert calls == [1]
    assert load_config()["check_updates"] is False


def test_update_links_open_only_compact_updates_panel():
    client = TestClient(app)
    home = BeautifulSoup(client.get("/").text, "html.parser")
    assert home.select_one("[data-release-notice]")["hidden"] == ""
    assert "open=updates" in home.select_one(".app-version a")["href"]
    page = BeautifulSoup(client.get("/settings").text, "html.parser")
    updates = page.select_one("#acc-updates")
    assert updates.select_one("[data-release-check]") is not None
    assert page.select_one("#acc-service [data-release-check]") is None
    assert "open" not in updates.attrs
    assert "open" not in updates.select_one("[data-update-details]").attrs
    assert updates.select_one("#check-updates")["checked"] == ""
    cfg = load_config()
    cfg["check_updates"] = False
    save_config(cfg)
    assert (
        "checked" not in BeautifulSoup(client.get("/settings").text, "html.parser").select_one("#check-updates").attrs
    )
