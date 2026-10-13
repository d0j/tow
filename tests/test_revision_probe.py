"""A fresh site hash cannot skip selection, client presence or ownership."""

from __future__ import annotations

from typing import Any

import httpx
import pytest
from helpers import make_torrent
from test_check_pipeline import OLD, Client, Tracker, _topic, _wire

from tow import check
from tow import http as thttp
from tow.check import topic as topic_step
from tow.config import load_config, save_config
from tow.mirrors import MirrorFetchError
from tow.store import load_state, save_download_history, save_state
from tow.torrent import parse_torrent_metadata
from tow.trackers.generic import GenericHttpTracker


class ProbeTracker(Tracker):
    def __init__(self, value: str | None = OLD):
        super().__init__()
        self.spec = {"infohash_path": "/revision?id={id}"}
        self.value = value
        self.probes = 0
        self.downloads = 0

    def fetch_revision_hash(self, url, secrets, ua, **kwargs):
        self.probes += 1
        return self.value

    def fetch_torrent(self, *args, **kwargs):
        self.downloads += 1
        return super().fetch_torrent(*args, **kwargs)


def wire(monkeypatch, *, value=OLD, tags=None, present=True, **topic_fields: Any):
    tracker = ProbeTracker(value)
    client = Client()
    if present:
        client.put(OLD, "/media/tv", tags=["tow"] if tags is None else tags)
    save_state({"topics": [_topic(hash=OLD, **topic_fields)]})
    save_download_history({"schema_version": 1, "topics": {}})
    _wire(monkeypatch, {"main": client}, tracker)
    return tracker, client


@pytest.mark.parametrize("prior_error", [False, True])
def test_fresh_same_hash_and_owned_client_avoid_download_and_clear_prior_tracker_error(monkeypatch, prior_error):
    extra = {"last_error": "previous site outage", "last_error_class": "tracker"} if prior_error else {}
    tracker, client = wire(monkeypatch, **extra)
    row = check.run_check(apply=True, notify=False)["results"][0]
    assert tracker.probes == 1
    assert tracker.downloads == 0
    assert row["source"] == "site_infohash"
    assert row["status"] == "succeeded"
    assert row["ok"] is True
    assert row["hash"] == OLD
    assert client.adds == []
    assert not load_state()["topics"][0].get("last_error")


@pytest.mark.parametrize("value", [None, "B" * 40, ""])
def test_changed_or_unknown_hash_uses_normal_metadata_flow(monkeypatch, value):
    tracker, _client = wire(monkeypatch, value=value)
    check.run_check(apply=True, notify=False)
    assert tracker.probes == 1
    assert tracker.downloads == 1


@pytest.mark.parametrize(("present", "tags"), [(False, ["tow"]), (True, []), (True, ["foreign"])])
def test_same_site_hash_without_owned_present_torrent_cannot_confirm_shortcut(monkeypatch, present, tags):
    tracker, _client = wire(monkeypatch, present=present, tags=tags)
    row = check.run_check(apply=True, notify=False, ids=["t1"], how="manual")["results"][0]
    assert tracker.downloads == 1
    assert row.get("source") != "site_infohash"


def test_selection_dirty_cannot_use_hash_only_confirmation(monkeypatch):
    tracker, _client = wire(monkeypatch, selection_dirty=True)
    check.run_check(apply=True, notify=False)
    assert tracker.probes == 0
    assert tracker.downloads == 1


@pytest.mark.parametrize("verified", [False, True])
def test_partial_selection_needs_the_same_verified_revision(monkeypatch, verified):
    tracker, _client = wire(
        monkeypatch,
        selection={"mode": "files", "value": "*.mkv"},
        selection_verified=verified,
        selection_hash=OLD,
    )
    row = check.run_check(apply=True, notify=False)["results"][0]
    assert tracker.downloads == (0 if verified else 1)
    assert (row.get("source") == "site_infohash") is verified


def test_preview_never_uses_applying_revision_probe(monkeypatch):
    tracker, _client = wire(monkeypatch)
    before = load_state()
    check.run_check(apply=False, notify=False)
    assert tracker.probes == 0
    assert load_state() == before


def test_once_hash_confirmation_still_requires_ownership(monkeypatch):
    tracker, _client = wire(monkeypatch, tracking_mode="once")
    row = check.run_check(apply=True, notify=False)["results"][0]
    assert row["source"] == "site_infohash"
    assert tracker.downloads == 0
    assert load_state()["topics"][0]["once_done"] is True


@pytest.mark.parametrize("failure", ["transport", "http", "cloudflare", "interactive", "auth", "quota", "paused"])
def test_probe_host_failures_are_not_followed_by_another_download_attempt(monkeypatch, failure):
    tracker, _client = wire(monkeypatch)

    def refused(*_args, **_kwargs):
        tracker.probes += 1
        raise MirrorFetchError("mirrors.all_failed", failure=failure, tracker="fake")

    monkeypatch.setattr(tracker, "fetch_revision_hash", refused)
    row = check.run_check(apply=True, notify=False)["results"][0]
    assert row["ok"] is False
    assert row["error_record"]["code"] == "mirrors.all_failed"
    assert tracker.probes == 1
    assert tracker.downloads == 0


@pytest.mark.parametrize(
    "failure", ["http_topic", "gone", "unexpected", "local", "too_large", "redirect_policy", "redirect_loop"]
)
def test_an_optional_revision_endpoint_failure_still_uses_normal_download(monkeypatch, failure):
    tracker, _client = wire(monkeypatch)

    def refused(*_args, **_kwargs):
        tracker.probes += 1
        raise MirrorFetchError("mirrors.all_failed", failure=failure, tracker="fake", status=404)

    monkeypatch.setattr(tracker, "fetch_revision_hash", refused)
    check.run_check(apply=True, notify=False)
    assert tracker.probes == 1
    assert tracker.downloads == 1


@pytest.mark.parametrize("failure", ["cloudflare", "maintenance", "interactive"])
@pytest.mark.parametrize("host_failure", [False, True, None])
def test_page_scoped_challenges_are_optional_but_unknown_or_host_wide_ones_stop(monkeypatch, failure, host_failure):
    tracker, _client = wire(monkeypatch)

    def refused(*_args, **_kwargs):
        tracker.probes += 1
        error = MirrorFetchError("mirrors.all_failed", failure=failure, tracker="fake")
        error.host_failure = host_failure
        raise error

    monkeypatch.setattr(tracker, "fetch_revision_hash", refused)
    row = check.run_check(apply=True, notify=False)["results"][0]
    assert tracker.downloads == (1 if host_failure is False else 0)
    assert row["ok"] is (host_failure is False)


@pytest.mark.parametrize("host_wide", [False, True])
def test_real_http_probe_challenge_checks_host_scope_before_downloading(monkeypatch, host_wide):
    content = make_torrent({b"name": b"Show", b"length": 1, b"piece length": 16384, b"pieces": b"x" * 20})
    metadata = parse_torrent_metadata(content)
    calls = []

    def response(request):
        calls.append(request.url.path)
        if request.url.path == "/revision" or (host_wide and request.url.path == "/"):
            return httpx.Response(403, text="<title>Just a moment...</title>", headers={"cf-mitigated": "challenge"})
        if request.url.path == "/download/1":
            return httpx.Response(200, content=content)
        return httpx.Response(200, text="<title>Show</title>")

    transport = httpx.MockTransport(response)
    monkeypatch.setattr(thttp, "client", lambda **kw: httpx.Client(transport=transport, follow_redirects=False))
    tracker = GenericHttpTracker(
        "fake",
        {
            "url_regex": r"^https://tracker\.example/(\d+)$",
            "fetch_hosts": ["https://tracker.example"],
            "infohash_path": "/revision?id={id}",
            "download_path": "/download/{id}",
        },
    )
    configuration = load_config()
    configuration["trackers"] = {"fake": tracker.spec}
    save_config(configuration)
    client = Client()
    client.put(metadata.client_hash, "/media/tv", tags=["tow"])
    save_state({"topics": [_topic(hash=metadata.client_hash)]})
    save_download_history({"schema_version": 1, "topics": {}})
    _wire(monkeypatch, {"main": client}, tracker)
    monkeypatch.setattr(topic_step, "parse_torrent_metadata", parse_torrent_metadata)
    row = check.run_check(apply=True, notify=False)["results"][0]
    assert calls.count("/revision") == 1
    assert calls.count("/") == 1
    assert calls.count("/download/1") == (0 if host_wide else 1)
    assert row["ok"] is not host_wide
    failures = load_state()["mirrors"]["fake"]["fail"]
    assert failures.get("https://tracker.example", 0) == (1 if host_wide else 0)


def test_a_pending_same_revision_takes_metadata_and_finishes_its_confirmed_add(monkeypatch):
    tracker, client = wire(monkeypatch, tags=["tow", "tow-pending"])
    parse_metadata = topic_step.parse_torrent_metadata
    monkeypatch.setattr(
        topic_step,
        "parse_torrent_metadata",
        lambda blob: type(parse_metadata(blob))(**{**vars(parse_metadata(blob)), "infohash": OLD, "client_hash": OLD}),
    )
    configured = []

    def finish(content, h, selected, *, ensure_started=False):
        configured.append(h)
        client.torrents[h]["tags"] = ["tow"]
        client.torrents[h]["state"] = "downloading"
        return client.inspect_torrent(h)

    monkeypatch.setattr(client, "configure_torrent_selection", finish)
    row = check.run_check(apply=True, notify=False)["results"][0]
    assert tracker.downloads == 1
    assert row.get("source") != "site_infohash"
    assert configured == [OLD]
    assert client.inspect_torrent(OLD)["tags"] == ["tow"]
    assert row["ok"] is True
