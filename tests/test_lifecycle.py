import json


def test_the_supervisor_reports_only_on_the_operation_the_marker_names(tmp_path, monkeypatch):
    from tow import lifecycle

    monkeypatch.setattr(lifecycle, "data_dir", lambda: tmp_path)
    assert lifecycle.update_restart_marker("restart-a", {"status": "ready"}) is False  # no marker yet
    lifecycle._write_marker({"operation_id": "restart-a", "status": "queued", "reason": "settings"})
    assert lifecycle.update_restart_marker("restart-b", {"status": "failed"}) is False
    assert lifecycle.update_restart_marker("restart-a", {"status": "ready", "process_pid": 9}) is True
    marker = json.loads((tmp_path / "service-restart.json").read_text(encoding="utf-8"))
    assert (marker["status"], marker["process_pid"], marker["reason"]) == ("ready", 9, "settings")


def test_autostart_goes_to_this_oses_backend(monkeypatch):
    from tow import lifecycle

    calls = []

    class Fake:
        supports_without_login = True

        def status(self):
            return {"on": bool(calls), "without_login": True}

        def enable(self, *, without_login=False):
            calls.append(("on", without_login))
            return {"ok": True}

        def disable(self):
            calls.append(("off",))
            return {"ok": True}

    monkeypatch.setattr("tow.autostart.backend", lambda *a, **k: Fake())
    assert lifecycle.service_status()["autostart"] is False
    assert lifecycle.set_autostart(True, without_login=True) == {"ok": True}
    status = lifecycle.service_status()
    assert (status["autostart"], status["without_login"]) == (True, True)
    assert status["supports_without_login"] is True
    assert "mode" not in status  # 1.21: one process only, no five-task mode
    lifecycle.set_autostart(False)
    assert calls == [("on", True), ("off",)]


def test_a_broken_autostart_read_back_is_off_not_a_broken_page(monkeypatch, caplog):
    from tow import lifecycle

    def broken(*_a, **_k):
        raise OSError("secret=must-not-be-displayed")

    lifecycle._forget_cached()
    monkeypatch.setattr("tow.autostart.backend", broken)
    status = lifecycle.service_status()
    assert status["autostart"] is False
    assert status["autostart_detail"]["error"] == "не удалось узнать состояние автозапуска"
    assert "must-not-be-displayed" not in str(status)
    assert "must-not-be-displayed" not in caplog.text


def test_settings_renders_reuse_the_autostart_read_back_for_a_minute(monkeypatch):
    # F2: every /settings render asked the OS (schtasks ~30 ms).
    from tow import lifecycle

    queries = []

    class Fake:
        supports_without_login = False

        def status(self):
            queries.append(1)
            return {"on": True}

    monkeypatch.setattr("tow.autostart.backend", lambda *a, **k: Fake())
    lifecycle.service_status()
    lifecycle.service_status()
    assert queries == [1]

    monkeypatch.setattr(lifecycle, "_AUTOSTART_CACHE_SECONDS", 0.0)
    lifecycle.service_status()
    assert queries == [1, 1]


def test_restart_goes_through_the_supervisor_when_it_runs(monkeypatch, tmp_path):
    from tow import lifecycle
    from tow.supervisor import layout

    held = layout.InstanceLock()
    assert held.acquire()
    try:
        result = lifecycle.request_restart(reason="settings")
    finally:
        held.release()
    assert result["ok"] is True
    request = layout.read_json(layout.control_dir() / "restart")
    assert request["operation_id"] == result["operation_id"]
    marker = json.loads(lifecycle.service_restart_path().read_text(encoding="utf-8"))
    assert (marker["status"], marker["via"]) == ("queued", "supervisor")


def test_without_tow_run_a_restart_is_refused_not_improvised(monkeypatch):
    # 1.21: no restart worker any more (scripts/tow-restart.py and the TOW-serve task are gone).
    from tow import lifecycle
    from tow.supervisor import layout

    assert layout.running() is None
    result = lifecycle.request_restart(reason="settings")
    assert result["ok"] is False
    assert not lifecycle.service_restart_path().exists()
    assert not (layout.control_dir() / "restart").exists()
