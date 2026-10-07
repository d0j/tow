"""The web UI must stay responsive while another worker holds the persistence lock.

A scheduled `tow check --apply` holds the cross-process persistence lock for its
whole network-bound run; the web process must not wait for it on plain reads.
"""

import threading

import pytest
from fastapi.testclient import TestClient
from helpers import shown

from tow.store import load_state, persistence_lock, save_state
from tow.web import app


def _hold_persistence_lock(release: threading.Event, held: threading.Event) -> threading.Thread:
    """Hold the lock like a running check: until ``release`` (or a 10 s safety cap)."""

    def hold() -> None:
        with persistence_lock():
            held.set()
            release.wait(10)
            held.clear()

    thread = threading.Thread(target=hold, daemon=True)
    thread.start()
    assert held.wait(5)
    return thread


def test_reads_do_not_wait_for_a_running_check():
    # No timing threshold (it was slow and could flake): a read that waited for the lock
    # could only finish after the holder let go, so each must finish while it still holds.
    client = TestClient(app)
    client.get("/healthz")  # warm up app startup
    release, held = threading.Event(), threading.Event()
    holder = _hold_persistence_lock(release, held)
    try:
        for path in ("/healthz", "/health.json", "/log.json", "/"):
            response = client.get(path, headers={"Accept": "text/html"})
            assert response.status_code == 200, path
            assert held.is_set(), f"{path} only answered after the persistence lock was released"
    finally:
        release.set()
        holder.join()


def _hold_check_lock(release: threading.Event, held: threading.Event) -> threading.Thread:
    """Another applying check is running (scheduled, or "check all"): it holds the check lock."""
    from tow.store import check_run_lock

    def hold() -> None:
        with check_run_lock():
            held.set()
            release.wait(10)
            held.clear()

    thread = threading.Thread(target=hold, daemon=True)
    thread.start()
    assert held.wait(5)
    return thread


def test_a_manual_check_does_not_wait_for_a_running_check():
    """M3: the request waited for the check lock while holding the HTTP lock - every page froze."""

    save_state({"topics": [{"id": "t", "title": "Show", "url": "http://rutor.info/torrent/1", "save_path": "M:\\s"}]})
    client = TestClient(app, headers={"Origin": "http://127.0.0.1"})
    release, held = threading.Event(), threading.Event()
    holder = _hold_check_lock(release, held)
    try:
        response = client.post("/topics/t/check", follow_redirects=False)
        assert held.is_set(), "the request only answered after the other check ended"
        assert response.status_code == 303
        assert "сейчас идёт другая проверка" in shown(response.headers["location"])
        added = client.post(
            "/topics/add",
            data={"url": "http://rutor.info/torrent/2/x", "title": "Другой сериал", "save_path": "M:\\s"},
            follow_redirects=False,
        )
        assert held.is_set()
        assert "наблюдение TOW сохранено; сейчас идёт другая проверка" in shown(added.headers["location"])
        assert len(load_state()["topics"]) == 2  # the new watch is saved all the same
    finally:
        release.set()
        holder.join()


def test_check_lock_without_waiting_refuses_a_held_lock_across_handles():
    import pytest

    from tow.paths import data_dir
    from tow.platform import locks
    from tow.store import _CHECK_RUN_LOCK_NAME, CheckBusyError, check_run_lock, init_lock_file

    with check_run_lock(wait=False):  # free: taken at once
        # this process (another thread or the same) holds it
        with pytest.raises(CheckBusyError), check_run_lock(wait=False):
            pass
        # Another process holds the file lock: a second handle on the same file cannot take it.
        with (data_dir() / _CHECK_RUN_LOCK_NAME).open("a+b") as other:
            init_lock_file(other)
            assert locks.lock(other, wait=False) is False
    with check_run_lock(wait=False):  # released again
        pass


def test_pending_secret_undo_cleanup_still_runs():
    state = load_state()
    state["secret_undo_cleanup_pending"] = {"reference": "settings-v1", "attempts": 0}
    save_state(state)

    assert TestClient(app).get("/health.json").status_code == 200

    assert "secret_undo_cleanup_pending" not in load_state()


def test_the_liveness_answer_reads_no_state(monkeypatch):
    """QA 1.24.1: every /healthz (a monitor polls it) copied the whole state for the pending
    undo cleanup's pre-check. It still rolls back an interrupted transaction (a journal look),
    and the next page does the cleanup."""
    calls = []
    monkeypatch.setattr("tow.web.services.recover_store_transaction", lambda: calls.append("recovery"))
    monkeypatch.setattr("tow.web.site_store.secret_undo_cleanup_pending", lambda: pytest.fail("state read"))
    client = TestClient(app)
    assert client.get("/healthz").json()["ok"] is True
    assert calls == ["recovery"]
    monkeypatch.setattr("tow.web.site_store.secret_undo_cleanup_pending", lambda: calls.append("cleanup") and False)
    assert client.get("/health.json").status_code == 200
    assert calls == ["recovery", "recovery", "cleanup"]


def test_a_slow_write_does_not_hold_up_pages(monkeypatch):
    """1.19: every request used to queue for the site lock - a site probe of half a minute froze
    Home. Reads no longer take it, and a probe no longer holds up a save either."""
    import asyncio

    monkeypatch.setattr("tow.web.middleware._SITE_HTTP_LOCK", asyncio.Lock())  # bound to this test's loop
    entered, release = threading.Event(), threading.Event()

    def slow_probe(**_kwargs):
        entered.set()
        release.wait(10)
        return {"ok": True, "probes": []}

    monkeypatch.setattr("tow.web.services.doctor_report", slow_probe)
    save_state({"topics": [{"id": "t", "title": "Show", "url": "http://rutor.info/torrent/1", "save_path": "M:\\s"}]})
    with TestClient(app, headers={"Origin": "http://127.0.0.1"}) as client:
        probe = threading.Thread(target=lambda: client.post("/doctor/run", follow_redirects=False), daemon=True)
        probe.start()
        done = threading.Event()
        pause = threading.Thread(
            target=lambda: (client.post("/topics/t/pause", follow_redirects=False), done.set()), daemon=True
        )
        try:
            assert entered.wait(5)
            for path in ("/", "/settings", "/history", "/sites", "/health.json"):
                assert client.get(path, headers={"Accept": "text/html"}).status_code == 200, path
                assert probe.is_alive(), f"{path} only answered after the probe ended"
            pause.start()
            # 1.24.1: a probe of half a minute held every save behind it.
            assert done.wait(5), "a save waited for a site probe"
            assert probe.is_alive()
        finally:
            release.set()
            probe.join(10)
        pause.join(10)
    assert load_state()["topics"][0]["paused"] is True


def test_ordinary_saves_still_go_one_at_a_time(monkeypatch):
    """A write that is not a network action keeps the site lock (one that takes no persistence
    lock of its own here, so only the site lock can make the second one wait)."""
    import asyncio

    monkeypatch.setattr("tow.web.middleware._SITE_HTTP_LOCK", asyncio.Lock())
    entered, release = threading.Event(), threading.Event()

    def slow_restart():
        entered.set()
        release.wait(10)
        return {"ok": False}

    monkeypatch.setattr("tow.web.services.request_restart", slow_restart)
    save_state({"topics": [{"id": "t", "title": "Show", "url": "http://rutor.info/torrent/1", "save_path": "M:\\s"}]})
    with TestClient(app, headers={"Origin": "http://127.0.0.1"}) as client:
        first = threading.Thread(target=lambda: client.post("/settings/service/restart"), daemon=True)
        first.start()
        done = threading.Event()
        second = threading.Thread(
            target=lambda: (client.post("/topics/t/pause", follow_redirects=False), done.set()), daemon=True
        )
        try:
            assert entered.wait(5)
            second.start()
            assert not done.wait(0.3), "a second write did not wait for the first"
        finally:
            release.set()
            first.join(10)
        second.join(10)
        assert done.is_set()


def _slow_network(monkeypatch, name: str, result, save=None):
    """``services.<name>`` waits like a client or a site that does not answer; ``save`` then
    writes what it learned the way the real action does (under the persistence lock)."""
    entered, release = threading.Event(), threading.Event()

    def slow(*_args, **_kwargs):
        entered.set()
        release.wait(10)
        if save is not None:
            with persistence_lock():
                state = load_state()
                save(state)
                save_state(state)
        return result

    monkeypatch.setattr(f"tow.web.services.{name}", slow)
    return entered, release


def _while_running(client, slow_request, entered, release, save_request) -> None:
    """``save_request`` finishes while ``slow_request`` still waits on the network."""
    slow = threading.Thread(target=slow_request, daemon=True)
    slow.start()
    try:
        assert entered.wait(5)
        response = save_request()
        assert response.status_code in (200, 303), response.text
        assert slow.is_alive(), "the save only answered after the network action ended"
    finally:
        release.set()
        slow.join(10)


def test_a_save_does_not_wait_for_a_client_check(monkeypatch):
    """QA 1.24.1: "Check" on a client that does not answer took 24 s; a language save made
    meanwhile waited 23.6 s for it."""
    import asyncio

    from tow.config import load_config

    monkeypatch.setattr("tow.web.middleware._SITE_HTTP_LOCK", asyncio.Lock())

    class Silent:
        def ping(self):
            entered.set()
            release.wait(10)
            raise ConnectionError("no answer")

    entered, release = threading.Event(), threading.Event()
    monkeypatch.setattr("tow.clients.factory.from_secrets", lambda *_a, **_k: Silent())
    monkeypatch.setattr("tow.web.services.client_answers", lambda *_a, **_k: True)
    with TestClient(app, headers={"Origin": "http://127.0.0.1"}) as client:
        _while_running(
            client,
            lambda: client.post("/settings/client/ping", follow_redirects=False),
            entered,
            release,
            lambda: client.post("/settings/language", data={"language": "en"}, follow_redirects=False),
        )
    assert load_config()["language"] == "en"


def test_a_topic_check_and_a_save_keep_both_changes(monkeypatch):
    """A check against a tracker that does not answer (45 s) no longer holds up a pause; the
    check's result, saved under the persistence lock, keeps the pause and the pause keeps it."""
    import asyncio

    monkeypatch.setattr("tow.web.middleware._SITE_HTTP_LOCK", asyncio.Lock())
    save_state({"topics": [{"id": "t", "title": "Show", "url": "http://rutor.info/torrent/1", "save_path": "M:\\s"}]})

    def checked(state):
        state["topics"][0]["last_ok"] = True

    entered, release = _slow_network(monkeypatch, "run_check", {"results": [{"id": "t", "ok": True}]}, checked)
    with TestClient(app, headers={"Origin": "http://127.0.0.1"}) as client:
        _while_running(
            client,
            lambda: client.post("/topics/t/check", follow_redirects=False),
            entered,
            release,
            lambda: client.post("/topics/t/pause", follow_redirects=False),
        )
    topic = load_state()["topics"][0]
    assert topic["paused"] is True
    assert topic["last_ok"] is True


def test_adding_a_topic_does_not_hold_up_saves(monkeypatch):
    """Adding a topic on a tracker that does not answer kept every save waiting for ~53 s."""
    import asyncio

    from tow.config import load_config

    monkeypatch.setattr("tow.web.middleware._SITE_HTTP_LOCK", asyncio.Lock())
    monkeypatch.setattr("tow.title.guess_topic_title", lambda *_a, **_k: "")
    entered, release = _slow_network(monkeypatch, "run_check", {"results": []})
    with TestClient(app, headers={"Origin": "http://127.0.0.1"}) as client:
        _while_running(
            client,
            lambda: client.post(
                "/topics/add",
                data={"url": "http://rutor.info/torrent/2/x", "title": "Show", "save_path": "M:\\s"},
                follow_redirects=False,
            ),
            entered,
            release,
            lambda: client.post("/settings/language", data={"language": "en"}, follow_redirects=False),
        )
    assert load_config()["language"] == "en"
    assert [topic["title"] for topic in load_state()["topics"]] == ["Show"]


def test_a_site_deleted_while_a_topic_is_added_is_not_added_to(monkeypatch):
    """The add no longer holds the site lock while it fetches the title: the site is looked up
    again under the persistence lock before the topic is saved."""
    import tow.web.routes_topics
    from tow.config import load_config, save_config

    real = tow.web.routes_topics.match_tracker
    calls = []

    def site_goes_away(trackers, url):
        calls.append(url)
        if len(calls) == 2:  # the second look, under the lock: the owner deleted the site meanwhile
            cfg = load_config()
            cfg["trackers"].pop("rutor", None)
            save_config(cfg)
            return None
        return real(trackers, url)

    monkeypatch.setattr("tow.web.routes_topics.match_tracker", site_goes_away)
    monkeypatch.setattr("tow.title.guess_topic_title", lambda *_a, **_k: "")
    monkeypatch.setattr("tow.web.services.run_check", lambda **_kw: pytest.fail("no check for a refused add"))
    client = TestClient(app, headers={"Origin": "http://127.0.0.1"})
    response = client.post(
        "/topics/add",
        data={"url": "http://rutor.info/torrent/2/x", "title": "Show", "save_path": "M:\\s"},
        follow_redirects=False,
    )
    assert response.status_code == 303
    assert load_state().get("topics", []) == []


def test_network_actions_are_the_only_writes_outside_the_site_lock():
    from starlette.requests import Request

    from tow.web.middleware import _serialized

    def serialized(path: str) -> bool:
        return _serialized(Request({"type": "http", "method": "POST", "path": path, "headers": []}))

    for path in (
        "/check",
        "/doctor/run",
        "/settings/client/ping",
        "/settings/notifier/telegram/test",
        "/sites/rutor/probe",
        "/topics/add",
        "/topics/guess-title",
        "/topics/t1/check",
        "/topics/t1/replace-revision",
        "/topics/t1/tracker-login",
        "/topics/t1/tracker-browser-auth",
    ):
        assert not serialized(path), path
    for path in (
        "/settings/language",
        "/settings/client",
        "/settings/notifier/telegram",
        "/sites/new",
        "/sites/rutor",
        "/sites/rutor/delete",
        "/topics/t1/edit",
        "/topics/t1/pause",
        "/topics/t1/check/extra",
        "/settings/restore-points",
    ):
        assert serialized(path), path


def test_the_middleware_reads_no_file_on_the_event_loop(monkeypatch):
    """Config, secrets, sessions, recovery and the remembered language are read in worker
    threads: one slow disk read must not stall every other request."""
    import asyncio

    import tow.auth
    import tow.i18n
    import tow.web
    from tow.auth import issue_session, lan_password_record, lan_password_session_key
    from tow.config import load_config, save_config
    from tow.store import save_secrets
    from tow.web import services

    record = lan_password_record("a-long-password")
    save_secrets({"lan_auth": record})
    cfg = load_config()
    cfg.update(allow_lan=True, bind="0.0.0.0", language="auto")
    save_config(cfg)
    cookie = issue_session(lan_password_session_key(record))
    on_loop: list[str] = []

    def watch(name: str, original):
        def wrapper(*args, **kwargs):
            try:
                asyncio.get_running_loop()
            except RuntimeError:
                pass  # a worker thread: fine
            else:
                on_loop.append(name)
            return original(*args, **kwargs)

        return wrapper

    for name in ("load_config", "load_secrets", "load_state"):
        monkeypatch.setattr(services, name, watch(name, getattr(services, name)))
    monkeypatch.setattr(services, "recover_store_transaction", watch("recovery", services.recover_store_transaction))
    monkeypatch.setattr(tow.auth, "session_is_valid", watch("session", tow.auth.session_is_valid))
    monkeypatch.setattr(tow.i18n, "remember_browser_language", watch("language", tow.i18n.remember_browser_language))
    client = TestClient(app, client=("192.168.1.9", 50000), base_url="http://192.168.1.2:8787")
    client.cookies.set("tow_session", cookie)

    response = client.get("/", headers={"Accept": "text/html", "Accept-Language": "ru"})

    assert response.status_code == 200
    assert on_loop == []
