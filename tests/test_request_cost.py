"""What one request costs: the config, the state and the secrets are each read at most once.

Before 1.19 Home read the config 7 times, the state 4 times and decrypted the secrets once; the
polled /health.json 5/2/1 times; every static file read the config twice and parsed the state.
The per-request context (tow.web._context) reads each once; these tests fail when a page starts
reading them again and again.
"""

from __future__ import annotations

import contextvars
import sys

import pytest
from fastapi.testclient import TestClient

import tow.config
import tow.store
from tow.auth import lan_password_record
from tow.store import save_secrets, save_state
from tow.web import app

_LOADERS = {
    "config": tow.config.load_config,
    "state": tow.store.load_state,
    "secrets": tow.store.load_secrets,
}


@pytest.fixture
def reads(monkeypatch):
    """How many times each store was read, wherever TOW imported its loader."""
    counts = dict.fromkeys(_LOADERS, 0)
    for name, original in _LOADERS.items():

        def counting(*args, _name=name, _original=original, **kwargs):
            counts[_name] += 1
            return _original(*args, **kwargs)

        for module in list(sys.modules.values()):
            if getattr(module, "__name__", "").startswith("tow") and module is not None:
                for attribute, value in list(vars(module).items()):
                    if value is original:
                        monkeypatch.setattr(module, attribute, counting)
    return counts


@pytest.fixture
def stores():
    topics = [
        {"id": f"t{i}", "title": f"Show {i}", "url": f"http://rutor.info/torrent/{i}", "save_path": "M:\\s"}
        for i in range(20)
    ]
    save_state({"topics": topics, "mirrors": {}, "health": {"qbit_ok": True}})
    save_secrets({"lan_auth": lan_password_record("a-long-password"), "telegram": {"token": "x" * 40, "chat_id": "1"}})


def _get(client: TestClient, path: str, reads: dict[str, int]) -> dict[str, int]:
    client.get(path, headers={"Accept": "text/html"})  # warm up (imports, caches)
    for name in reads:
        reads[name] = 0
    response = client.get(path, headers={"Accept": "text/html"})
    assert response.status_code == 200, path
    return dict(reads)


@pytest.mark.parametrize(
    ("path", "config", "state", "secrets"),
    [
        ("/", 1, 1, 1),
        ("/health.json", 1, 1, 1),
        ("/history", 1, 1, 1),
        ("/sites", 1, 1, 1),
        ("/login", 1, 1, 0),
        # The night-copy and restore-point folders come from the config in their own modules.
        ("/settings", 3, 1, 1),
        # A static file needs the config (who may ask) and nothing else.
        ("/static/app.css", 1, 0, 0),
    ],
)
def test_a_page_reads_each_store_once(monkeypatch, stores, reads, path, config, state, secrets):
    monkeypatch.setattr(
        "tow.web.services.service_status", lambda: {"pid": 1, "mode": "run", "task": {}, "restart": None}
    )
    client = TestClient(app)

    assert _get(client, path, reads) == {"config": config, "state": state, "secrets": secrets}


def test_a_static_file_runs_no_recovery(monkeypatch):
    calls = []
    monkeypatch.setattr("tow.web.services.recover_store_transaction", lambda: calls.append("recovery") or False)
    monkeypatch.setattr("tow.web.site_store.secret_undo_cleanup_pending", lambda: calls.append("pending") or False)
    client = TestClient(app)

    assert client.get("/static/app.css").status_code == 200
    assert calls == []
    assert client.get("/healthz").status_code == 200
    assert calls == ["recovery"]  # the liveness answer reads no state for the undo pre-check
    assert client.get("/health.json").status_code == 200
    assert calls == ["recovery", "recovery", "pending"]


def _in_a_request(function):
    """Run ``function`` inside a request context of its own (as the middleware opens one)."""
    from tow.web import _context

    def run():
        _context.begin()
        return function()

    return contextvars.copy_context().run(run)


def test_a_save_in_the_same_request_is_seen_by_the_page():
    """The context drops what it read when this process writes a file: a re-rendered page shows
    the saved data, not the snapshot from before the save."""
    from tow.web import _context

    save_state({"topics": [{"id": "old"}]})

    def request():
        before = _context.state()["topics"]
        save_state({"topics": [{"id": "new"}]})
        return before, _context.state()["topics"]

    assert _in_a_request(request) == ([{"id": "old"}], [{"id": "new"}])


def test_outside_a_request_every_call_reads_afresh(reads):
    from tow.web import _context

    _context.config()
    _context.config()

    assert reads["config"] == 2


def test_a_broken_secret_store_gives_the_same_answer_for_the_whole_request(monkeypatch):
    from tow.store import SecretStoreError
    from tow.web import _context

    calls = []

    def broken():
        calls.append(1)
        raise SecretStoreError("cannot decrypt TOW secrets")

    monkeypatch.setattr("tow.web.services.load_secrets", broken)

    def request():
        assert _context.secrets_or_none() is None
        with pytest.raises(SecretStoreError):
            _context.secrets()

    _in_a_request(request)
    assert calls == [1]


def _sites(*names_and_patterns):
    return {"trackers": {name: {"url_regex": pattern} for name, pattern in names_and_patterns}}


def test_the_site_of_a_link_is_the_one_match_tracker_finds_and_is_kept_for_the_same_sites(monkeypatch):
    """Home and the header name every topic's site: the answer of match_tracker (the first site in
    config order whose pattern takes the link), remembered across requests while the sites stay
    the same, and asked again as soon as a site or a pattern changes."""
    from tow.trackers import generic, load_trackers, match_tracker
    from tow.web import _context

    cfg = {
        "value": _sites(
            ("wide", r"^https?://(?:www\.)?example\.org/.*?[?&]t=(\d+)"),
            ("narrow", r"^https?://example\.org/topic\?t=(\d+)"),
        )
    }
    monkeypatch.setattr("tow.web.services.load_config", lambda: cfg["value"])
    runs = []
    original = generic.GenericHttpTracker.parse_id

    def parse_id(self, url):
        runs.append(self.name)
        return original(self, url)

    monkeypatch.setattr(generic.GenericHttpTracker, "parse_id", parse_id)
    links = [
        "https://example.org/topic?t=1",
        "https://www.example.org/x?a=1&t=2",
        "https://other.example/topic?t=3",
        "  https://example.org/topic?t=4  ",
    ]

    def names():
        return [_context.site_name(link) for link in links]

    def expected():
        found = [match_tracker(load_trackers(cfg["value"]), link) for link in links]
        return [tracker.name if tracker else None for tracker in found]

    assert _in_a_request(names) == expected() == ["wide", "wide", None, "wide"]
    runs.clear()
    assert _in_a_request(names) == ["wide", "wide", None, "wide"]
    assert runs == []  # the same sites: nothing is matched again
    assert _in_a_request(lambda: _context.tracker_of(links[0]).name) == "wide"

    # Another order of the same sites: the first one that takes the link wins again.
    cfg["value"] = _sites(
        ("narrow", r"^https?://example\.org/topic\?t=(\d+)"),
        ("wide", r"^https?://(?:www\.)?example\.org/.*?[?&]t=(\d+)"),
    )
    assert _in_a_request(names) == expected() == ["narrow", "wide", None, "narrow"]
    # A new pattern for a site, and a new site: shown at once.
    cfg["value"] = _sites(
        ("narrow", r"^https?://example\.org/topic\?t=(\d+)"),
        ("wide", r"^https?://www\.example\.org/.*?[?&]t=(\d+)"),
        ("other", r"^https?://other\.example/topic\?t=(\d+)"),
    )
    assert _in_a_request(names) == expected() == ["narrow", "wide", "other", "narrow"]
    # A link nobody asked about yet is matched when it is first asked.
    runs.clear()
    assert _in_a_request(lambda: _context.site_name("https://other.example/topic?t=9")) == "other"
    assert runs == ["narrow", "wide", "other"]


def test_home_and_the_header_name_the_same_sites_after_the_lookup_is_kept(monkeypatch, stores):
    """The header's site icons and Home's site column come from the kept lookup and do not change
    between the first and the second request."""
    from tow.web.templating import header_health

    client = TestClient(app)
    first = client.get("/", headers={"Accept": "text/html"}).text
    second = client.get("/", headers={"Accept": "text/html"}).text
    assert first.count('class="topic-tracker"') == second.count('class="topic-tracker"') == 20
    assert _in_a_request(header_health)["sites"] == {"rutor": "mut"}
