"""An inherited Windows job never reaches installation without an independent worker."""

from __future__ import annotations

import json
import subprocess
from types import SimpleNamespace

import pytest
from test_update import _load

from tow import update_worker


def machine(tmp_path):
    module = _load()
    result = object.__new__(module.System)
    result.windows = True
    result.root = tmp_path / "Unicode-путь"
    result.env = {"TOW_ROOT": str(result.root), "TEST_SECRET": "private-значение"}
    return module, result


def test_local_broker_transports_environment_only_in_stdin(tmp_path, monkeypatch):
    module, system = machine(tmp_path)
    calls = []
    monkeypatch.setattr(
        module.subprocess,
        "run",
        lambda args, **kwargs: (
            calls.append((args, kwargs)) or SimpleNamespace(returncode=0, stdout='{"ReturnValue":0,"ProcessId":42}')
        ),
    )
    argv = [str(tmp_path / "with space" / "python.exe"), "-I", "worker.py", 'quoted"value']
    system.spawn_broker(argv)
    args, options = calls[0]
    assert args[:4] == ["powershell", "-NoProfile", "-NonInteractive", "-Command"]
    assert "Win32_ProcessStartup" in args[-1]
    assert "0x09000400" in args[-1]
    assert "-ComputerName" not in args[-1]
    assert "private" not in str(args)
    payload = json.loads(options["input"])
    assert payload["command"] == subprocess.list2cmdline(argv)
    assert payload["cwd"] == str(system.root)
    assert "TEST_SECRET=private-значение" in payload["environment"]
    assert options["timeout"] == 15
    assert options["creationflags"] == module.NO_WINDOW
    assert not options.get("shell", False)


@pytest.mark.parametrize(
    "answer",
    [
        "malformed",
        "[]",
        "null",
        '{"ReturnValue":false,"ProcessId":42}',
        '{"ReturnValue":2,"ProcessId":42}',
        '{"ReturnValue":0,"ProcessId":true}',
        '{"ReturnValue":0,"ProcessId":0}',
        '{"ReturnValue":0,"ProcessId":4294967296}',
    ],
)
def test_broker_requires_confirmed_process_creation(tmp_path, monkeypatch, answer):
    module, system = machine(tmp_path)
    monkeypatch.setattr(module.subprocess, "run", lambda *a, **k: SimpleNamespace(returncode=0, stdout=answer))
    with pytest.raises(OSError, match="Windows broker"):
        system.spawn_broker(["python", "worker.py"])


@pytest.mark.parametrize("failure", [OSError("private-value"), subprocess.TimeoutExpired("private-value", 15)])
def test_broker_timeout_and_os_failure_have_a_safe_message(tmp_path, monkeypatch, failure):
    module, system = machine(tmp_path)

    def fail(*_args, **_kwargs):
        raise failure

    monkeypatch.setattr(module.subprocess, "run", fail)
    with pytest.raises(OSError, match=r"^Windows broker launch failed$"):
        system.spawn_broker(["python", "worker.py"])


def test_broker_is_windows_only(tmp_path):
    _module, system = machine(tmp_path)
    system.windows = False
    with pytest.raises(OSError, match="unavailable"):
        system.spawn_broker(["python", "worker.py"])


@pytest.mark.parametrize(
    ("mode", "independent", "waited", "failure", "reason"),
    [
        ("--handoff", False, True, False, ""),
        ("--handoff", False, True, True, "releases.broker_failed"),
        ("--broker-child", False, True, False, "releases.inherited_job"),
        ("--broker-child", True, False, False, "releases.parent_wait_failed"),
        ("--broker-child", True, True, False, ""),
    ],
)
def test_broker_worker_checks_independence_and_parent_before_running(
    tmp_path, monkeypatch, mode, independent, waited, failure, reason
):
    path = tmp_path / "job.json"
    path.write_text(json.dumps({"id": "owned", "status": "queued"}))
    (tmp_path / "owned").mkdir()
    monkeypatch.setattr(update_worker, "__file__", str(tmp_path / "owned" / "worker.py"))
    calls = []

    def broker(argv):
        calls.append(("broker", argv))
        if failure:
            raise OSError("private error")

    system = SimpleNamespace(
        updater_independent=lambda: independent,
        spawn_broker=broker,
        wait_process_exit=lambda *_: calls.append(("wait",)) or waited,
    )
    module = SimpleNamespace(System=lambda _: system)
    spec = SimpleNamespace(name="isolated_broker", loader=SimpleNamespace(exec_module=lambda _: None))
    monkeypatch.setattr(update_worker.importlib.util, "spec_from_file_location", lambda *a: spec)
    monkeypatch.setattr(update_worker.importlib.util, "module_from_spec", lambda _: module)
    monkeypatch.setattr(update_worker, "run", lambda *a: calls.append(("run",)) or 0)
    monkeypatch.setattr(
        update_worker.sys,
        "argv",
        [
            "worker.py",
            mode,
            *(["42"] if mode == "--broker-child" else []),
            str(tmp_path),
            str(path),
            "owned",
            "1.22.23",
        ],
    )
    assert update_worker.main() == (2 if reason else 0)
    if mode == "--handoff":
        assert calls[0][0] == "broker"
        assert calls[0][1][1:4] == ["-I", "-S", "-u"]
        assert "--broker-child" in calls[0][1]
        assert all(call[0] != "run" for call in calls)
    else:
        assert all(call[0] != "broker" for call in calls)
        assert any(call[0] == "run" for call in calls) is (not reason)
    if reason:
        assert json.loads(path.read_text())["error"] == reason
        if mode == "--broker-child":
            assert reason in (tmp_path / "owned" / "update.log").read_text()


@pytest.mark.parametrize("started", [True, "yesterday", 0, 160])
def test_expired_or_invalid_reservations_cannot_start_late(tmp_path, monkeypatch, started):
    monkeypatch.setattr(update_worker.time, "time", lambda: 100)
    path = tmp_path / "job.json"
    path.write_text(json.dumps({"id": "owned", "status": "queued", "started_at": started}))
    assert update_worker.run(tmp_path, path, "owned", "1.22.23", None) == 2
    assert json.loads(path.read_text())["error"] == "releases.interrupted"


@pytest.mark.parametrize("foreign", ["path", "id"])
def test_broker_child_refuses_foreign_record_before_any_file_access(tmp_path, monkeypatch, foreign):
    folder = tmp_path / "owned"
    monkeypatch.setattr(update_worker, "__file__", str(folder / "worker.py"))
    record = tmp_path / "job.json" if foreign == "id" else tmp_path / "foreign.json"
    identifier = "other" if foreign == "id" else "owned"
    monkeypatch.setattr(
        update_worker.sys,
        "argv",
        ["worker.py", "--broker-child", "42", str(tmp_path), str(record), identifier, "1.22.23"],
    )
    assert update_worker.main() == 2
    assert not folder.exists()
    assert not record.exists()
