from tow.browser_auth import BrowserAuthManager


def _record(operation_id: str, *, finished_at: float, status: str = "succeeded") -> dict:
    return {
        "operation_id": operation_id,
        "topic_id": operation_id,
        "tracker": "tracker",
        "status": status,
        "message": status,
        "started_at": finished_at - 1,
        "finished_at": finished_at,
    }


def test_status_expires_completed_sessions_but_preserves_active(monkeypatch):
    manager = BrowserAuthManager(completed_ttl_sec=10, max_completed_sessions=4)
    manager._sessions["expired"] = _record("expired", finished_at=80)
    manager._sessions["active"] = _record("active", finished_at=80, status="waiting")
    manager._active_by_topic["active"] = "active"
    monkeypatch.setattr("tow.browser_auth.time.time", lambda: 100)

    assert manager.status("expired", "expired")["status"] == "idle"
    assert manager.status("active", "active")["status"] == "waiting"
    assert set(manager._sessions) == {"active"}


def test_status_keeps_only_newest_completed_sessions_and_all_active(monkeypatch):
    manager = BrowserAuthManager(completed_ttl_sec=1_000, max_completed_sessions=3)
    for index in range(6):
        operation_id = f"done-{index}"
        manager._sessions[operation_id] = _record(operation_id, finished_at=90 + index)
    manager._sessions["active"] = _record("active", finished_at=1, status="waiting")
    manager._active_by_topic["active"] = "active"
    monkeypatch.setattr("tow.browser_auth.time.time", lambda: 100)

    manager.status("missing")

    assert set(manager._sessions) == {"done-3", "done-4", "done-5", "active"}


# --- N5-N7 -------------------------------------------------------------------------------------
import asyncio  # noqa: E402
import json  # noqa: E402
from pathlib import Path  # noqa: E402
from types import SimpleNamespace  # noqa: E402

import pytest  # noqa: E402

from tow import browser_auth as ba  # noqa: E402
from tow.log import read_events  # noqa: E402
from tow.paths import data_dir  # noqa: E402


def _run_session(monkeypatch, *, wait):
    """Drive BrowserAuthManager._run synchronously with a fake browser process."""
    from tow.platform import posix

    monkeypatch.setattr(ba, "find_browser_executable", lambda: "msedge.exe")
    monkeypatch.setattr(ba, "_show_browser_window", lambda _pid: None)
    monkeypatch.setattr(posix, "_run", lambda *_a, **_k: "")  # Linux asks systemd for the display
    monkeypatch.setattr(ba.subprocess, "Popen", lambda *a, **k: SimpleNamespace(pid=4242, poll=lambda: None))
    killed = []
    monkeypatch.setattr(ba, "_terminate_process", lambda process: killed.append(process.pid))
    monkeypatch.setattr(ba, "_wait_for_authenticated_topic", wait)
    manager = ba.BrowserAuthManager()
    record = {
        "operation_id": "op-1",
        "topic_id": "t1",
        "tracker": "nnmclub",
        "status": "starting",
        "profile": str(data_dir() / "browser-auth" / "op-1" / "profile"),
    }
    manager._run(record, "https://nnmclub.to/forum/viewtopic.php?t=1", "https://nnmclub.to/login", lambda *_: {}, 30)
    return record, killed


def test_a_failed_login_is_in_the_event_log_with_its_phase(monkeypatch):
    async def closed(**_kw):
        raise ba.BrowserAuthError("окно браузера закрыто до входа")

    record, killed = _run_session(monkeypatch, wait=closed)

    assert record["status"] == "failed"
    assert killed == [4242]
    assert not Path(record["profile"]).parent.exists()
    event = next(e for e in read_events(limit=5) if e["kind"] == "browser_auth_failed")
    assert (event["phase"], event["tracker"], event["error_type"]) == ("wait_login", "nnmclub", "BrowserAuthError")


def test_a_successful_login_is_logged_too(monkeypatch):
    async def ok(**_kw):
        return {"uid": "1"}, "UA"

    record, _ = _run_session(monkeypatch, wait=ok)

    assert record["status"] == "succeeded"
    assert any(e["kind"] == "browser_auth_succeeded" for e in read_events(limit=5))


class _FakeCdp:
    """A CDP websocket: Runtime.evaluate fails ``errors`` times, then shows the logged-in page."""

    def __init__(self, errors):
        self.errors = errors
        self.outbox = []

    async def __aenter__(self):
        return self

    async def __aexit__(self, *exc):
        return False

    async def send(self, raw):
        request = json.loads(raw)
        method, ident = request["method"], request["id"]
        if method == "Runtime.evaluate" and self.errors > 0:
            self.errors -= 1
            self.outbox.append({"id": ident, "error": {"message": "Execution context was destroyed."}})
        elif method == "Runtime.evaluate":
            value = {"href": "https://nnmclub.to/forum/viewtopic.php?t=1", "host": "nnmclub.to", "hasDownload": True}
            self.outbox.append({"id": ident, "result": {"result": {"value": value}}})
        elif method == "Network.getAllCookies":
            self.outbox.append(
                {"id": ident, "result": {"cookies": [{"domain": ".nnmclub.to", "name": "phpbb", "value": "x"}]}}
            )
        else:
            self.outbox.append({"id": ident, "result": {"userAgent": "UA"}})

    async def recv(self):
        return json.dumps(self.outbox.pop(0))


@pytest.mark.parametrize(("errors", "ok"), [(3, True), (ba.MAX_TRANSIENT_CDP_ERRORS, False)])
def test_page_navigation_errors_are_retried_not_fatal(monkeypatch, errors, ok):
    # N6: one "context destroyed" while the login page navigated ended the whole session.
    import websockets.asyncio.client as ws_client

    fake = _FakeCdp(errors)
    monkeypatch.setattr(ws_client, "connect", lambda *a, **k: fake)

    async def target(*_a):
        return "ws://fake"

    monkeypatch.setattr(ba, "_wait_target", target)
    real_sleep = asyncio.sleep
    monkeypatch.setattr(ba.asyncio, "sleep", lambda _s: real_sleep(0))
    process = SimpleNamespace(poll=lambda: None)
    run = ba._wait_for_authenticated_topic(
        port=1, topic_url="https://nnmclub.to/forum/viewtopic.php?t=1", timeout_sec=60, process=process
    )

    if ok:
        cookies, user_agent = asyncio.run(run)
        assert cookies == {"phpbb": "x"}
        assert user_agent == "UA"
    else:
        with pytest.raises(ba.BrowserAuthError, match="перестало отвечать"):
            asyncio.run(run)


def test_browser_is_stopped_with_its_whole_process_tree():
    # N7: terminate() stopped only the main Edge process; its children kept the profile.
    from tow import platform

    stopped: list[tuple[int, float]] = []
    backend = SimpleNamespace(
        name="windows", terminate=lambda pid, timeout=10.0: stopped.append((pid, timeout)) or True
    )
    process = SimpleNamespace(pid=77, poll=lambda: 0)

    with platform.use(backend):  # type: ignore[arg-type]
        ba._terminate_process(process)

    assert stopped == [(77, 5)]


def test_profile_removal_retries_while_files_are_still_held(monkeypatch, tmp_path):
    session = tmp_path / "op" / "profile"
    session.mkdir(parents=True)
    real_rmtree = ba.shutil.rmtree
    attempts = []

    def flaky(path, ignore_errors=False):
        attempts.append(1)
        if len(attempts) >= 3:
            real_rmtree(path, ignore_errors=ignore_errors)

    monkeypatch.setattr(ba.shutil, "rmtree", flaky)
    monkeypatch.setattr(ba.time, "sleep", lambda _s: None)

    assert ba._remove_profile(session) is True
    assert len(attempts) == 3


def test_stale_profiles_are_swept_before_the_first_session(monkeypatch):
    leftover = data_dir() / "browser-auth" / "browser-auth-dead" / "profile"
    leftover.mkdir(parents=True)
    (leftover / "Cookies").write_text("secret cookie db", encoding="utf-8")
    monkeypatch.setattr(ba.threading, "Thread", lambda **_k: SimpleNamespace(start=lambda: None))
    manager = ba.BrowserAuthManager()

    manager.start(
        topic_id="t1", tracker_name="nnmclub", topic_url="https://x/1", start_url="https://x/", on_success=lambda *_: {}
    )

    assert not leftover.parent.exists()
