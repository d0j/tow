"""scripts/update.py on a throwaway install: real git on a temp clone, everything else faked.

The port of scripts/test-deploy.ps1: success, a new version that does not answer (rollback with
the data put back), a rollback step that fails, local edits, a second update at the same time,
and an install that still runs the five Windows tasks of 1.17 (refused).
"""

from __future__ import annotations

import importlib.util
import io
import json
import os
import subprocess
import sys
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

pytestmark = pytest.mark.allow_git

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "update.py"


def _load():
    spec = importlib.util.spec_from_file_location("tow_update_under_test", SCRIPT)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = module
    spec.loader.exec_module(module)
    return module


updater = _load()


def _git_env(config: Path) -> dict[str, str]:
    env = {key: value for key, value in os.environ.items() if not key.startswith("GIT_")}
    return {**env, "GIT_CONFIG_GLOBAL": str(config), "GIT_CONFIG_NOSYSTEM": "1", "GIT_TERMINAL_PROMPT": "0"}


_ENV: dict[str, str] = {}


def git(cwd: Path, *args: str) -> str:
    done = subprocess.run(
        ["git", "-c", "user.name=t", "-c", "user.email=t@example.invalid", "-c", "commit.gpgsign=false", *args],
        cwd=str(cwd),
        env=_ENV or None,
        capture_output=True,
        text=True,
        check=True,
    )
    return done.stdout.strip()


@pytest.fixture(scope="module")
def origin(tmp_path_factory) -> Path:
    """A repository with two tagged releases, made once for the module."""
    base = tmp_path_factory.mktemp("update-origin")
    (base / "gitconfig").write_text("", encoding="utf-8")
    _ENV.update(_git_env(base / "gitconfig"))
    origin = base / "origin"
    origin.mkdir()
    git(origin, "init", "-q", "-b", "main")
    (origin / "pyproject.toml").write_text('[project]\nname = "tow"\nversion = "1.20.0"\n', encoding="utf-8")
    (origin / "README.md").write_text("one\n", encoding="utf-8")
    git(origin, "add", "-A")
    git(origin, "commit", "-q", "-m", "one")
    git(origin, "tag", "v1.20.0")
    (origin / "pyproject.toml").write_text('[project]\nname = "tow"\nversion = "1.21.0"\n', encoding="utf-8")
    git(origin, "commit", "-q", "-am", "two")
    git(origin, "tag", "v1.21.0")
    (origin / "pyproject.toml").write_text('[project]\nname = "tow"\nversion = "1.17.1"\n', encoding="utf-8")
    git(origin, "commit", "-q", "-am", "the five tasks")
    git(origin, "tag", "v1.17.1")
    return origin


@pytest.fixture
def install(tmp_path, origin, monkeypatch) -> dict[str, Any]:
    """<tmp>/TOW: app (a clone of the origin at v1.0.0), data, config.yaml, backup/."""
    for key in [key for key in os.environ if key.startswith("GIT_")]:
        monkeypatch.delenv(key)
    for key, value in _ENV.items():
        if key.startswith("GIT_"):
            monkeypatch.setenv(key, value)  # the updater's own git calls see the same isolation
    root = tmp_path / "TOW"
    root.mkdir()
    git(root, "clone", "-q", str(origin), "app")
    git(root / "app", "checkout", "-q", "--detach", "v1.20.0")
    (root / "config.yaml").write_text("port: 18999\nlanguage: en\n", encoding="utf-8")
    data = root / "data"
    (data / "browser-auth" / "op").mkdir(parents=True)
    (data / "browser-auth" / "op" / "Cookies").write_bytes(b"secret cookies")
    (data / "state.json").write_text('{"topics": []}', encoding="utf-8")
    (data / "master.key").write_text("not a real key", encoding="utf-8")
    (data / "sessions.json").write_text("{}", encoding="utf-8")
    (data / "lan-auth.token").write_text("token", encoding="utf-8")
    backup = root / "backup"
    for name in (
        "update-20260901-000000-before-v0.9.0",
        "update-20260902-000000-before-v0.9.1",
        "update-20260903-000000-before-v0.9.2",
        "data-20260801-000000-before-v0.8.0",
        "data-20260802-000000-before-v0.8.1",
        "update-20260904-000000-before-v0.9.3",
        "data-20250101-000000-pre-runtime",
        "night",
        "key-copy",
    ):
        (backup / name).mkdir(parents=True)
    return {"origin": origin, "root": root, "app": root / "app", "data": data, "backup": backup}


class Fake(updater.System):
    """The machine: tow run, uv and the web server (and, refused, the five tasks of 1.17)."""

    def __init__(self, app: Path, *, windows: bool = False, **scenario: Any):
        super().__init__(app)
        self.windows = windows
        self.home = app.parent / "home"
        self.scenario = scenario
        self.calls: list[str] = []
        self.supervisor = scenario.get("supervisor", True)
        self.up = self.supervisor
        self.syncs = 0
        self.tasks: dict[str, dict[str, Any]] = scenario.get("tasks", {})
        self.t = 0.0

    def head_version(self) -> str:
        text = (self.app / "pyproject.toml").read_text(encoding="utf-8")
        return text.split('version = "', 1)[1].split('"', 1)[0]

    def uv_sync(self) -> None:
        self.syncs += 1
        self.calls.append(f"uv sync {self.head_version()}")
        if self.syncs in self.scenario.get("uv_fails_on", ()):
            raise updater.UpdateError("uv: network unreachable")

    def sleep(self, seconds: float) -> None:
        self.t += seconds

    def monotonic(self) -> float:
        return self.t

    def http_json(self, url: str, timeout: float = 3.0):
        if not self.up:
            return None
        if self.scenario.get("bad_target") and self.head_version() == "1.21.0":
            return None
        if url.endswith("/healthz"):
            return {"ok": True, "version": self.head_version()}
        return {"ok": True}

    def port_open(self, port: int) -> bool:
        return self.up and not self.scenario.get("port_stuck")

    def kill_tree(self, pid: int) -> bool:
        self.calls.append(f"kill {pid}")
        if pid == 4242:
            self.supervisor = False
        if pid in self.scenario.get("server_pids", ()) or pid == 4242:
            self.up = False
        return True

    def status_pids(self) -> list[int]:
        return list(self.scenario.get("status_pids", ()))

    def command_line(self, pid: int) -> str:
        return self.scenario.get("commands", {}).get(pid, "")

    def stop_autostart(self, kind: str) -> bool:
        self.calls.append(f"manager stop {kind}")
        return True

    def supervisor_running(self):
        return {"pid": 4242} if self.supervisor else None

    def request_stop(self) -> None:
        self.calls.append("stop request")
        self.supervisor = False
        self.up = False

    def autostart_kind(self):
        return self.scenario.get("autostart")

    def start_autostart(self, kind: str) -> bool:
        self.calls.append(f"autostart {kind}")
        self._started()
        return True

    def spawn_supervisor(self) -> None:
        self.calls.append(f"spawn tow run {self.head_version()}")
        self._started()

    def _started(self) -> None:
        self.supervisor = True
        self.up = True
        logs = self.app.parent / "data" / "logs"
        logs.mkdir(parents=True, exist_ok=True)
        with (logs / "run.log").open("a", encoding="utf-8") as handle:
            handle.write(f"TOW {self.head_version()} started\n")
        if self.scenario.get("new_version_migrates") and self.head_version() == "1.21.0":
            (self.app.parent / "data" / "state.json").write_text('{"topics": [], "schema": 2}', encoding="utf-8")
            (self.app.parent / "data" / "new-index.json").write_text("{}", encoding="utf-8")
            (self.app.parent / "config.yaml").write_text("port: 18999\nnew_key: 1\n", encoding="utf-8")

    # the five Windows tasks of 1.17 (only to be refused)
    def task_xml(self, name: str):
        if name not in self.tasks:
            return None
        command = self.app / "scripts" / self.tasks[name]
        return f"<Task><Actions><Exec><Command>{command}</Command></Exec></Actions></Task>"


def run(fake: Fake, ref: str = "v1.21.0", **options) -> tuple[int, list[str]]:
    lines: list[str] = []
    work_options = {"health_timeout": 5, "wait_minutes": 1, **options}
    code = updater.update(ref, system=fake, app=fake.app, say=lines.append, **work_options)
    return code, lines


def head(install) -> str:
    return git(install["app"], "rev-parse", "HEAD")


def state(install) -> dict[str, Any]:
    return json.loads((install["root"] / "update-state.json").read_text(encoding="utf-8"))


def snapshots(install) -> list[str]:
    return sorted(path.name for path in install["backup"].iterdir())


# --- supervised service (tow run) --------------------------------------------------------------


def test_a_successful_update(install):
    fake = Fake(install["app"])
    target = git(install["origin"], "rev-parse", "v1.21.0")

    code, lines = run(fake)

    assert code == 0, lines
    assert head(install) == target
    assert fake.calls == ["stop request", "uv sync 1.21.0", "spawn tow run 1.21.0"]
    record = state(install)
    assert (record["status"], record["target_version"], record["service_running"]) == ("ok", "1.21.0", True)
    assert lines[-1] == "TOW 1.21.0 is running and answers on 127.0.0.1:18999"
    snapshot = Path(record["snapshot"])
    kept = sorted(p.relative_to(snapshot).as_posix() for p in snapshot.rglob("*") if p.is_file())
    assert kept == ["SNAPSHOT.json", "config.yaml", "data/state.json"]  # no key, token, session, profile
    # five newest update/deploy snapshots stay; night copies, key copies and pre-runtime are never touched
    names = snapshots(install)
    assert snapshot.name in names
    assert [n for n in names if "-before-" in n] == sorted(
        [snapshot.name, *[f"update-2026090{i}-000000-before-v0.9.{i - 1}" for i in (1, 2, 3, 4)]]
    )
    assert {"night", "key-copy", "data-20250101-000000-pre-runtime"} <= set(names)


def test_autostart_starts_the_new_version_when_it_is_on(install):
    fake = Fake(install["app"], autostart="systemd")
    code, _ = run(fake)
    assert code == 0
    assert fake.calls[-1] == "autostart systemd"


def test_a_new_version_that_does_not_answer_is_rolled_back_with_its_data(install):
    fake = Fake(install["app"], bad_target=True, new_version_migrates=True)
    before = head(install)

    code, lines = run(fake)

    assert code == 1
    assert head(install) == before
    record = state(install)
    assert record["status"] == "rolled_back"
    assert record["data_restored"] is True
    assert [step["ok"] for step in record["rollback"]] == [True, True, True, True, True]
    assert (install["data"] / "state.json").read_text(encoding="utf-8") == '{"topics": []}'
    assert not (install["data"] / "new-index.json").exists()
    assert "new_key" not in (install["root"] / "config.yaml").read_text(encoding="utf-8")
    assert (install["data"] / "master.key").exists()  # what the snapshot leaves out is never touched
    assert (install["data"] / "browser-auth" / "op" / "Cookies").exists()
    assert "the previous version is back and answers" in lines
    assert fake.calls[-1] == "spawn tow run 1.20.0"
    # 1.21: the failed version's log stays (it says why it failed); the snapshot never had it
    assert (install["data"] / "logs" / "run.log").read_text(encoding="utf-8").splitlines() == [
        "TOW 1.21.0 started",
        "TOW 1.20.0 started",
    ]
    assert not list(Path(record["snapshot"]).rglob("run.log"))
    assert len([n for n in snapshots(install) if n.startswith("update-")]) == 5  # a failure prunes nothing


def test_new_logs_alone_do_not_count_as_changed_data(install):
    fake = Fake(install["app"], bad_target=True)  # writes run.log, changes nothing else
    code, _ = run(fake)
    assert code == 1
    record = state(install)
    assert record["status"] == "rolled_back"
    assert record["data_restored"] is False  # nothing to put back: logs are not data
    assert (install["data"] / "logs" / "run.log").is_file()


def test_a_failing_rollback_step_does_not_stop_the_others(install):
    fake = Fake(install["app"], bad_target=True, uv_fails_on=(2,))
    before = head(install)

    code, lines = run(fake)

    assert code == 1
    assert head(install) == before  # the checkout still went back
    record = state(install)
    steps = {step["step"]: step for step in record["rollback"]}
    assert steps["uv sync"]["ok"] is False
    assert "network unreachable" in steps["uv sync"]["error"]
    assert steps["start the previous version"]["ok"] is True
    assert any(line.startswith("rollback - uv sync: ") for line in lines)
    assert record["status"] == "failed"  # a failed environment rebuild cannot prove rollback complete


def test_a_previous_version_that_does_not_come_back_is_said_plainly(install):
    fake = Fake(install["app"], bad_target=True)
    fake.http_json = lambda url, timeout=3.0: None  # nothing answers any more
    code, lines = run(fake)
    assert code == 1
    assert state(install)["status"] == "failed"
    assert any("did not come back" in line for line in lines)


def test_local_edits_are_refused_before_anything_stops(install):
    (install["app"] / "README.md").write_text("edited by hand\n", encoding="utf-8")
    fake = Fake(install["app"])
    code, lines = run(fake)
    assert code == 2
    assert fake.calls == []
    assert "local changes" in lines[-1]
    assert not (install["root"] / "update-state.json").exists()


def test_a_second_update_at_the_same_time_is_refused(install):
    lock = (install["root"] / ".update.lock").open("a+b")
    try:
        assert updater._try_lock(lock)
        fake = Fake(install["app"])
        code, lines = run(fake)
    finally:
        updater._unlock(lock)
        lock.close()
    assert code == 2
    assert fake.calls == []
    assert lines[-1].startswith("another update is running")


def test_a_development_checkout_is_not_updated(install):
    (install["root"] / "config.yaml").unlink()
    code, lines = run(Fake(install["app"]))
    assert code == 2
    assert "not a runtime install" in lines[-1]


def test_a_supervisor_that_does_not_stop_is_stopped_forcibly(install):
    fake = Fake(install["app"])
    fake.request_stop = lambda: fake.calls.append("stop request")  # it ignores the request
    code, lines = run(fake)
    assert code == 0
    assert "kill 4242" in fake.calls
    assert any("stopped forcibly" in line for line in lines)


def test_a_failed_snapshot_changes_nothing_and_starts_tow_again(install, monkeypatch):
    fake = Fake(install["app"])
    before = head(install)

    def broken(*_a, **_k):
        raise OSError("disk full")

    monkeypatch.setattr(updater.shutil, "copy2", broken)
    code, lines = run(fake)
    assert code == 1
    assert head(install) == before
    assert state(install)["status"] == "failed"
    assert fake.calls[-1] == "spawn tow run 1.20.0"
    assert any("the snapshot failed" in line for line in lines)


def test_a_target_older_than_one_process_is_refused(install):
    fake = Fake(install["app"])
    before = head(install)
    code, lines = run(fake, ref="v1.17.1")
    assert code == 2
    assert head(install) == before
    assert fake.calls == []  # nothing stopped
    assert "v1.18.0" in lines[-1]


def _ours(install, *args: str) -> str:
    return f'"{install["app"] / ".venv" / "bin" / "python"}" -m tow {" ".join(args)}'


def test_a_forced_stop_on_linux_also_stops_the_web_server_and_the_job(install):
    # 1.21: killing the supervisor's process group never reached its children (own sessions),
    # and systemd (Restart=on-failure) started a killed TOW again in the middle of the update.
    commands = {
        4300: _ours(install, "serve", "--log-file", str(install["data"] / "logs" / "serve.log")),
        4400: _ours(install, "check", "--apply"),
        4500: "/usr/bin/vim notes.txt",  # the job's pid, reused by now: never touched
    }
    fake = Fake(
        install["app"], autostart="systemd", status_pids=(4300, 4400, 4500), commands=commands, server_pids=(4300,)
    )
    fake.request_stop = lambda: fake.calls.append("stop request")  # it ignores the request

    code, lines = run(fake)

    assert code == 0, lines
    stop = fake.calls[: fake.calls.index("uv sync 1.21.0")]
    assert stop[:4] == ["stop request", "manager stop systemd", "kill 4242", "kill 4300"]
    assert "kill 4400" in stop
    assert "kill 4500" not in fake.calls
    assert fake.calls[-1] == "autostart systemd"


def test_a_web_server_left_by_a_dead_supervisor_is_stopped_before_the_update(install):
    commands = {4300: _ours(install, "serve", "--log-file", "x")}
    fake = Fake(install["app"], supervisor=False, status_pids=(4300,), commands=commands, server_pids=(4300,))
    fake.up = True  # the port is held, nobody holds the lock
    code, lines = run(fake)
    assert code == 0, lines
    assert fake.calls[0] == "kill 4300"


def test_another_programs_port_is_never_freed_by_force(install):
    fake = Fake(install["app"], supervisor=False, status_pids=(4300,), commands={4300: "python -m http.server"})
    fake.up = True
    code, lines = run(fake)
    assert code == 1
    assert "kill 4300" not in fake.calls
    assert any("port 18999 is still in use" in line for line in lines)


# --- the machine (System) without effects -------------------------------------------------------


class Recorder(updater.System):
    def __init__(self, app: Path, results: dict[str, int] | None = None):
        super().__init__(app)
        self.argv: list[list[str]] = []
        self.results = results or {}

    def _run(self, argv, *, cwd=None, env=None, timeout=600):
        self.argv.append(list(argv))
        return self.results.get(argv[1], 0), ""


def test_the_os_manager_stops_and_starts_tow(install, monkeypatch):
    monkeypatch.setenv("XDG_CONFIG_HOME", str(install["root"] / "xdg"))
    system = Recorder(install["app"])
    system.home = install["root"] / "home"
    system.windows = False
    unit = install["root"] / "xdg" / "systemd" / "user" / "tow.service"
    unit.parent.mkdir(parents=True)
    unit.write_text(f'ExecStart="{install["app"] / ".venv" / "bin" / "tow"}" run\n', encoding="utf-8")
    assert system.autostart_kind() == "systemd"  # found where XDG_CONFIG_HOME says
    assert system.stop_autostart("systemd") is True
    assert system.start_autostart("systemd") is True
    assert system.argv == [
        ["systemctl", "--user", "stop", "tow.service"],
        ["systemctl", "--user", "start", "tow.service"],
    ]


def test_launchd_is_bootstrapped_when_the_agent_is_not_loaded(install):
    system = Recorder(install["app"], results={"print": 113})
    system.uid = 501
    assert system.stop_autostart("launchd") is True
    assert system.start_autostart("launchd") is True
    assert system.argv[0] == ["launchctl", "bootout", "gui/501/io.tow"]
    assert system.argv[-1] == ["launchctl", "bootstrap", "gui/501", str(system.agent_path())]
    loaded = Recorder(install["app"])
    loaded.uid = 501
    assert loaded.start_autostart("launchd") is True
    assert loaded.argv[-1] == ["launchctl", "kickstart", "gui/501/io.tow"]


def test_status_pids_are_the_server_and_the_job(install):
    run_dir = install["data"] / "run"
    run_dir.mkdir()
    (run_dir / "status.json").write_text(
        json.dumps({"server": {"pid": 11}, "job": {"name": "check", "pid": 12}}), encoding="utf-8"
    )
    assert updater.System(install["app"]).status_pids() == [11, 12]
    (run_dir / "status.json").write_text("{broken", encoding="utf-8")
    assert updater.System(install["app"]).status_pids() == []


def test_the_launchers_environment_is_the_updaters(install):
    # One environment for uv, Python and uv's cache: what scripts/tow and tow-env.cmd set.
    scripts = SCRIPT.parent
    posix = (scripts / "tow").read_text(encoding="utf-8")
    windows = (scripts / "tow-env.cmd").read_text(encoding="utf-8")
    uv, env = updater.launcher_env(install["app"], {"PATH": "x"})
    names = {name for name in env if name.startswith("UV_")}
    assert names == {
        "UV_PYTHON_INSTALL_DIR",
        "UV_PYTHON_BIN_DIR",
        "UV_CACHE_DIR",
        "UV_PROJECT_ENVIRONMENT",
        "UV_MANAGED_PYTHON",
    }
    for name in names:
        assert f"export {name}=" in posix
        assert f'set "{name}=' in windows
    root, app = install["root"], install["app"]
    assert env["UV_PYTHON_INSTALL_DIR"] == str(root / "runtime" / "python")
    assert env["UV_PYTHON_BIN_DIR"] == str(root / "runtime" / "bin")
    assert env["UV_CACHE_DIR"] == str(root / "runtime" / "cache")
    assert env["UV_PROJECT_ENVIRONMENT"] == str(app / ".venv")
    assert (env["UV_MANAGED_PYTHON"], env["TOW_ROOT"], env["PATH"]) == ("1", str(root), "x")
    assert uv == "uv"
    own = root / "runtime" / "bin" / ("uv.exe" if os.name == "nt" else "uv")
    own.parent.mkdir(parents=True)
    own.write_bytes(b"")
    assert updater.launcher_env(app, {})[0] == str(own)  # runtime/bin/uv before uv on PATH
    assert updater.launcher_env(app, {}, uv="/opt/uv")[0] == "/opt/uv"


def test_the_updater_runs_on_python_3_11():
    # deploy.ps1 may fall back to any Python 3.11+: no 3.14-only syntax (except A, B:).
    import ast

    ast.parse(SCRIPT.read_text(encoding="utf-8"), feature_version=(3, 11))


def test_deploy_gives_the_new_version_as_long_as_update_does():
    import re as regex

    deploy = (SCRIPT.parent / "deploy.ps1").read_text(encoding="utf-8")
    match = regex.search(r"\$HealthTimeoutSec = (\d+)", deploy)
    assert match is not None
    assert int(match.group(1)) == 90 == updater.Update.__init__.__kwdefaults__["health_timeout"]


# --- the five Windows tasks of 1.17 ---------------------------------------------------------------


def test_an_install_that_still_runs_the_five_tasks_is_refused(install):
    # 1.21 has no five-task layout: such an install switches with v1.20.0 first.
    fake = Fake(install["app"], windows=True, supervisor=False, tasks={"TOW-serve": "tow-serve.cmd"})
    fake.up = True
    before = head(install)

    code, lines = run(fake)

    assert code == 2
    assert head(install) == before
    assert fake.calls == []  # nothing stopped, nothing synced
    assert "v1.20.0" in lines[-1]
    assert "tow autostart migrate --apply" in lines[-1]
    assert not (install["root"] / "update-state.json").exists()
    assert not (install["root"] / "deploy-state.json").exists()


def test_another_folders_tow_serve_does_not_block_an_update(install):
    fake = Fake(install["app"], windows=True, tasks={})
    fake.tasks["TOW-serve"] = "../../Other/app/scripts/tow-serve.cmd"
    code, lines = run(fake)
    assert code == 0, lines


# --- texts, versions and the CLI ---------------------------------------------------------------


@pytest.mark.parametrize("encoding", ["cp1251", "cp1252", "ascii", "utf-8"])
@pytest.mark.parametrize("result", [0, 1, 2])
def test_cli_output_is_utf8_without_changing_arguments_or_result(tmp_path, monkeypatch, encoding, result):
    text = "Обновление: папка 日本語 📁 — готово\n"
    buffers = [io.BytesIO(), io.BytesIO()]
    streams = [io.TextIOWrapper(buffer, encoding=encoding, newline="\n", write_through=True) for buffer in buffers]
    system = object()
    calls = []

    def machine(app, *, uv):
        calls.append((app, uv))
        return system

    def run_update(ref, **options):
        calls.append((ref, options))
        work = object.__new__(updater.Update)
        work.texts = {"fixture": "{folder}"}
        work._say = print
        work.say("fixture", folder=text.rstrip("\n"))
        print(text, end="", file=sys.stderr)
        return result

    monkeypatch.setattr(updater, "System", machine)
    monkeypatch.setattr(updater, "update", run_update)
    with monkeypatch.context() as stdio:
        stdio.setattr(sys, "stdout", streams[0])
        stdio.setattr(sys, "stderr", streams[1])
        assert (
            updater.main(
                [
                    "--ref",
                    "v1.23.5",
                    "--health-timeout",
                    "120",
                    "--wait-minutes",
                    "7",
                    "--keep",
                    "123",
                    "--uv",
                    "fixture-uv",
                    "--source",
                    str(tmp_path / "source.tar.gz"),
                    "--sums",
                    str(tmp_path / "SHA256SUMS"),
                ]
            )
            == result
        )
    assert calls == [
        (updater.APP, "fixture-uv"),
        (
            "v1.23.5",
            {
                "system": system,
                "health_timeout": 120.0,
                "wait_minutes": 7.0,
                "keep": 123,
                "source": (tmp_path / "source.tar.gz").resolve(),
                "sums": (tmp_path / "SHA256SUMS").resolve(),
            },
        ),
    ]
    for stream, buffer in zip(streams, buffers, strict=True):
        stream.flush()
        assert buffer.getvalue().decode("utf-8") == text
        assert stream.write_through


def test_cli_configures_error_output_before_argument_validation(monkeypatch):
    buffer = io.BytesIO()
    stream = io.TextIOWrapper(buffer, encoding="ascii", newline="\n", write_through=True)
    monkeypatch.setattr(sys, "argv", ["更新📁.py"])
    monkeypatch.setattr(updater, "System", lambda *_args, **_kwargs: pytest.fail("not a valid request"))
    with monkeypatch.context() as stdio:
        stdio.setattr(sys, "stderr", stream)
        with pytest.raises(SystemExit) as caught:
            updater.main([])
    assert caught.value.code == 2
    stream.flush()
    assert "更新📁.py" in buffer.getvalue().decode("utf-8")


def test_cli_retains_unencodable_path_diagnostics_without_crashing(monkeypatch):
    buffer = io.BytesIO()
    stream = io.TextIOWrapper(buffer, encoding="ascii", newline="\n", write_through=True)
    monkeypatch.setattr(updater, "System", lambda *_args, **_kwargs: None)

    def run_update(*_args, **_kwargs):
        print("fixture-\udcff")
        return 2

    monkeypatch.setattr(updater, "update", run_update)
    with monkeypatch.context() as stdio:
        stdio.setattr(sys, "stdout", stream)
        assert updater.main(["--ref", "v1.23.5"]) == 2
    stream.flush()
    assert buffer.getvalue() == b"fixture-\\udcff\n"


@pytest.mark.parametrize("stream", [None, io.StringIO()])
def test_cli_accepts_replaced_or_missing_streams(monkeypatch, stream):
    monkeypatch.setattr(updater, "System", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(updater, "update", lambda *_args, **_kwargs: 2)
    with monkeypatch.context() as stdio:
        stdio.setattr(sys, "stdout", stream)
        stdio.setattr(sys, "stderr", stream)
        assert updater.main(["--ref", "v1.23.5"]) == 2


@pytest.mark.parametrize("error", [AttributeError, OSError, ValueError])
def test_cli_output_setup_failure_does_not_skip_other_stream(monkeypatch, error):
    calls = []

    def unavailable(**_kwargs):
        calls.append("unavailable")
        raise error("diagnostic stream unavailable")

    def available(**kwargs):
        calls.append(kwargs)

    monkeypatch.setattr(updater, "System", lambda *_args, **_kwargs: None)
    monkeypatch.setattr(updater, "update", lambda *_args, **_kwargs: 2)
    with monkeypatch.context() as stdio:
        stdio.setattr(sys, "stdout", SimpleNamespace(reconfigure=unavailable))
        stdio.setattr(sys, "stderr", SimpleNamespace(reconfigure=available))
        assert updater.main(["--ref", "v1.23.5"]) == 2
    assert calls == ["unavailable", {"encoding": "utf-8", "errors": "backslashreplace"}]


def test_the_texts_match_the_english_catalog():
    catalog = json.loads((SCRIPT.parents[1] / "src" / "tow" / "locales" / "en.json").read_text(encoding="utf-8"))
    assert catalog["update"] == updater.TEXTS


def test_the_owners_language_is_used(install):
    (install["root"] / "config.yaml").write_text("port: 18999\nlanguage: ru\n", encoding="utf-8")
    locales = install["app"] / "src" / "tow" / "locales"
    locales.mkdir(parents=True)
    source = SCRIPT.parents[1] / "src" / "tow" / "locales"
    for code in ("en", "ru"):
        (locales / f"{code}.json").write_text((source / f"{code}.json").read_text(encoding="utf-8"), encoding="utf-8")
    texts = updater._messages(install["app"], install["root"])
    assert texts["stopping"] == "останавливаю TOW..."


def test_task_xml_is_read_for_command_and_state():
    info = updater.task_info(
        '<Task xmlns="x"><Settings><Enabled>false</Enabled></Settings><Actions><Exec>'
        '<Command>"C:\\TOW\\app\\scripts\\tow-check.cmd"</Command></Exec></Actions></Task>'
    )
    assert info == {"command": "C:\\TOW\\app\\scripts\\tow-check.cmd", "enabled": False}
    assert updater.task_info("") is None
    assert updater.task_info("<oops") is None


def test_tow_update_prints_how_to_run_it(capsys, tmp_path, monkeypatch):
    from tow import cli, paths

    assert cli.main(["update", "--ref", "v1.18.0"]) == 0
    out = capsys.readouterr().out
    assert "update.py" in out
    assert "--ref v1.18.0" in out
    assert "v1.22.0" not in out  # a git clone: any tag from v1.18.0
    if os.name == "nt":
        assert "deploy.ps1" in out
    # An install without git (the Windows bundle, the installers) is told what it can update to.
    monkeypatch.setattr(paths, "repo_root", lambda: tmp_path / "TOW" / "app")
    assert cli.main(["update", "--ref", "latest"]) == 0
    out = capsys.readouterr().out
    assert "--ref latest" in out
    assert "v1.22.0" in out
    assert "latest" in out
