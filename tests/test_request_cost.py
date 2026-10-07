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
