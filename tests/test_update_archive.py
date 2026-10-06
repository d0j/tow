"""scripts/update.py on an install without git (the Windows bundle, install.ps1, install.sh).

The release comes from a local web server that plays GitHub (`/releases/latest`, the tag's
archive, the release's SHA256SUMS and its copy of the archive); tow run, uv and the web server
are faked as in test_update. No git: the test guard would refuse it.
"""

from __future__ import annotations

import hashlib
import io
import json
import os
import shutil
import subprocess
import sys
import tarfile
import threading
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any

import pytest
from test_update import SCRIPT, Fake, updater

REPO = "d0j/tow"


def tarball(version: str, *, top: str | None = None, extra: dict[str, bytes] | None = None) -> bytes:
    """A source archive as GitHub makes it: one folder tow-<version>/ with the code."""
    top = top or f"tow-{version}"
    files = {
        "pyproject.toml": f'[project]\nname = "tow"\nversion = "{version}"\n'.encode(),
        "README.md": b"TOW\n",
        f"marker-{version}": b"new code\n",
        "scripts/tow": b"#!/bin/sh\n",
        **(extra or {}),
    }
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as tar:
        for name, data in files.items():
            info = tarfile.TarInfo(f"{top}/{name}")
            info.size = len(data)
            info.mode = 0o755 if name == "scripts/tow" else 0o644
            tar.addfile(info, io.BytesIO(data))
    return buffer.getvalue()


def sums(**files: bytes) -> bytes:
    return "".join(f"{hashlib.sha256(data).hexdigest()}  {name}\n" for name, data in files.items()).encode()


class GitHub:
    """Routes: path -> (status, body, headers)."""

    def __init__(self) -> None:
        self.routes: dict[str, tuple[int, bytes, dict[str, str]]] = {}
        self.asked: list[str] = []

    def release(self, tag: str, archive: bytes, *, listed: bytes | None = b"auto", copy: bytes | None = None) -> None:
        """A release: GitHub's archive of the tag, the uploaded copy and SHA256SUMS."""
        self.routes[f"/{REPO}/archive/refs/tags/{tag}.tar.gz"] = (200, archive, {})
        self.routes[f"/{REPO}/releases/download/{tag}/tow-source.tar.gz"] = (200, copy or archive, {})
        if listed == b"auto":
            listed = sums(**{"tow-source.tar.gz": archive})
        if listed is not None:
            self.routes[f"/{REPO}/releases/download/{tag}/SHA256SUMS"] = (200, listed, {})

    def latest(self, tag: str) -> None:
        self.routes[f"/{REPO}/releases/latest"] = (302, b"", {"Location": f"/{REPO}/releases/tag/{tag}"})


@pytest.fixture
def github(monkeypatch):
    site = GitHub()

    class Handler(BaseHTTPRequestHandler):
        def do_GET(self) -> None:
            site.asked.append(self.path)
            status, body, headers = site.routes.get(self.path, (404, b"Not Found", {}))
            self.send_response(status)
            for name, value in headers.items():
                self.send_header(name, value)
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)

        def log_message(self, *_args: Any) -> None:
            pass

    server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
    thread = threading.Thread(target=server.serve_forever, daemon=True)
    thread.start()
    monkeypatch.setattr(urllib.request, "getproxies", dict)  # no proxy between the test and its server
    site.url = f"http://127.0.0.1:{server.server_address[1]}"  # type: ignore[attr-defined]
    try:
        yield site
    finally:
        server.shutdown()
        server.server_close()
        thread.join(timeout=5)


@pytest.fixture
def install(tmp_path) -> dict[str, Path]:
    """<tmp>/TOW: app (TOW 1.22.0 from an archive: no .git) with its .venv, data, config.yaml."""
    root = tmp_path / "TOW"
    app = root / "app"
    (app / ".venv" / "bin").mkdir(parents=True)
    (app / ".venv" / "bin" / "python").write_text("old venv", encoding="utf-8")
    (app / "pyproject.toml").write_text('[project]\nname = "tow"\nversion = "1.22.0"\n', encoding="utf-8")
    (app / "marker-1.22.0").write_text("old code\n", encoding="utf-8")
    (root / "config.yaml").write_text("port: 18999\nlanguage: en\n", encoding="utf-8")
    (root / "data").mkdir()
    (root / "data" / "state.json").write_text('{"topics": []}', encoding="utf-8")
    return {"root": root, "app": app}


class Machine(Fake):
    """test_update's machine, whose releases come from the local GitHub."""

    def __init__(self, app: Path, github: GitHub, **scenario: Any):
        super().__init__(app, **scenario)
        self.github = github.url  # type: ignore[attr-defined]

    def http_json(self, url: str, timeout: float = 3.0):
        if self.up and self.head_version() == self.scenario.get("silent_version"):
            return None  # this version never answers
        return super().http_json(url, timeout)


def run(machine: Machine, ref: str = "latest", **options: Any) -> tuple[int, list[str]]:
    lines: list[str] = []
    code = updater.update(ref, system=machine, app=machine.app, say=lines.append, health_timeout=5, **options)
    return code, lines


def state(install) -> dict[str, Any]:
    return json.loads((install["root"] / "update-state.json").read_text(encoding="utf-8"))


def names(folder: Path) -> list[str]:
    return sorted(os.listdir(folder))


def leftovers(install) -> list[str]:
    return [name for name in ("app.new", "app.failed", ".update-download") if (install["root"] / name).exists()]


def test_the_latest_release_is_downloaded_checked_and_switched_to(install, github):
    archive = tarball("1.23.0")
    github.latest("v1.23.0")
    github.release("v1.23.0", archive, listed=sums(**{"tow-source.tar.gz": archive, "TOW-windows-x64.zip": b"z"}))
    machine = Machine(install["app"], github)

    code, lines = run(machine)

    assert code == 0, lines
    assert machine.calls == ["stop request", "uv sync 1.23.0", "spawn tow run 1.23.0"]
    assert names(install["app"]) == ["README.md", "marker-1.23.0", "pyproject.toml", "scripts"]
    # one previous version is kept as it was, its environment too
    assert names(install["root"] / "app.prev") == [".venv", "marker-1.22.0", "pyproject.toml"]
    assert leftovers(install) == []
    record = state(install)
    assert (record["status"], record["target"], record["previous"]) == ("ok", "v1.23.0", "v1.22.0")
    assert lines[0] == f"downloading {github.url}/{REPO}/releases/download/v1.23.0/SHA256SUMS"
    assert "TOW update: v1.22.0 -> v1.23.0 (latest) in" in lines[2]
    assert lines[-1] == "TOW 1.23.0 is running and answers on 127.0.0.1:18999"
    assert f"/{REPO}/archive/refs/tags/v1.23.0.tar.gz" in github.asked  # GitHub's archive of the tag
    if os.name != "nt":
        assert os.access(install["app"] / "scripts" / "tow", os.X_OK)  # the launcher stays executable


def test_a_changed_github_archive_falls_back_to_the_releases_own_copy(install, github):
    archive = tarball("1.23.0")
    github.release("v1.23.0", tarball("1.23.0", extra={"x": b"regenerated"}), copy=archive)
    github.routes[f"/{REPO}/releases/download/v1.23.0/SHA256SUMS"] = (200, sums(**{"tow-source.tar.gz": archive}), {})
    code, lines = run(Machine(install["app"], github), "v1.23.0")
    assert code == 0, lines
    assert f"/{REPO}/releases/download/v1.23.0/tow-source.tar.gz" in github.asked


def test_an_archive_that_matches_no_checksum_changes_nothing(install, github):
    github.release("v1.23.0", tarball("1.23.0"), listed=sums(**{"tow-source.tar.gz": b"something else"}))
    machine = Machine(install["app"], github)
    code, lines = run(machine, "v1.23.0")
    assert code == 2
    assert machine.calls == []  # TOW was never stopped
    assert "does not match the release's SHA256SUMS" in lines[-1]
    assert names(install["app"]) == [".venv", "marker-1.22.0", "pyproject.toml"]
    assert leftovers(install) == []
    assert not (install["root"] / "update-state.json").exists()


def test_a_release_without_sums_changes_nothing(install, github):
    github.release("v1.23.0", tarball("1.23.0"), listed=None)
    machine = Machine(install["app"], github)
    code, lines = run(machine, "v1.23.0")
    assert code == 2
    assert machine.calls == []
    assert "the release has no checksum for the source archive; nothing was updated" in lines[-1]


@pytest.mark.parametrize(
    ("ref", "release", "message"),
    [
        ("v1.23.0", None, "the release has no checksum for the source archive"),
        ("v1.21.0", "1.21.0", "goes no further back than v1.22.0"),
        ("v1.23.0", "1.24.0", "the archive of v1.23.0 holds TOW 1.24.0"),
        ("main", None, "updates to a release tag (for example v1.22.0) or latest, not to main"),
    ],
)
def test_refused_targets_stop_nothing(install, github, ref, release, message):
    if release:
        github.release(ref, tarball(release))
    machine = Machine(install["app"], github)
    code, lines = run(machine, ref)
    assert code == 2
    assert message in lines[-1]
    assert machine.calls == []
    assert names(install["app"]) == [".venv", "marker-1.22.0", "pyproject.toml"]
    assert leftovers(install) == []


def test_a_download_that_fails_says_where(install, github):
    github.routes[f"/{REPO}/releases/latest"] = (500, b"oops", {})
    code, lines = run(Machine(install["app"], github))
    assert code == 2
    assert "the download failed" in lines[-1]
    assert "HTTP 500" in lines[-1]


@pytest.mark.parametrize(
    "entry",
    [
        "../escape",
        "/absolute",
        "C:/escape",
        "file:stream",
        "CON",
        "trailing.",
        "double//slash",
    ],
)
def test_an_archive_that_leaves_its_folder_is_refused(install, github, entry):
    buffer = io.BytesIO()
    with tarfile.open(fileobj=buffer, mode="w:gz") as tar:
        for name in ("tow-1.23.0/pyproject.toml", f"tow-1.23.0/{entry}" if entry.startswith("..") else entry):
            data = b'version = "1.23.0"\n'
            info = tarfile.TarInfo(name)
            info.size = len(data)
            tar.addfile(info, io.BytesIO(data))
    github.release("v1.23.0", buffer.getvalue())
    machine = Machine(install["app"], github)
    code, lines = run(machine, "v1.23.0")
    assert code == 2
    assert "cannot be unpacked" in lines[-1]
    assert machine.calls == []
    assert not (install["root"].parent / "escape").exists()


def test_archive_unpack_preserves_executable_scripts(tmp_path):
    archive = tmp_path / "source.tar.gz"
    archive.write_bytes(tarball("1.23.0"))

    top = updater.unpack(archive, tmp_path / "unpacked")

    script = top / "scripts" / "tow"
    assert script.read_bytes() == b"#!/bin/sh\n"
    if os.name != "nt":
        assert script.stat().st_mode & 0o111 == 0o111


@pytest.mark.parametrize("limit", ["MAX_ARCHIVE_FILES", "MAX_MEMBER_BYTES", "MAX_UNPACKED_BYTES"])
def test_archive_expansion_limits_apply_before_switch(install, github, monkeypatch, limit):
    github.release("v1.23.0", tarball("1.23.0"))
    monkeypatch.setattr(updater, limit, 1)
    machine = Machine(install["app"], github)

    code, lines = run(machine, "v1.23.0")

    assert code == 2
    assert machine.calls == []
    assert "source archive exceeds its unpacked size or file-count limit" in lines[-1]
    assert names(install["app"]) == [".venv", "marker-1.22.0", "pyproject.toml"]


def test_a_new_version_that_does_not_answer_puts_the_previous_code_back(install, github):
    github.release("v1.23.0", tarball("1.23.0"))
    machine = Machine(install["app"], github, silent_version="1.23.0")

    code, lines = run(machine, "v1.23.0")

    assert code == 1
    record = state(install)
    assert record["status"] == "rolled_back"
    assert [step["ok"] for step in record["rollback"]] == [True, True, True, True, True, True]
    assert names(install["app"]) == [".venv", "marker-1.22.0", "pyproject.toml"]
    assert (install["app"] / ".venv" / "bin" / "python").read_text(encoding="utf-8") == "old venv"
    assert not (install["root"] / "app.prev").exists()  # emptied by the rollback
    assert leftovers(install) == []
    assert machine.calls[-1] == "spawn tow run 1.22.0"
    assert "the previous version is back and answers" in lines


def test_a_switch_cut_off_halfway_is_undone_exactly(install, github, monkeypatch):
    github.release("v1.23.0", tarball("1.23.0"))
    machine = Machine(install["app"], github)
    real = updater.os.replace

    def stuck(source, destination):
        if Path(source).name == "pyproject.toml" and Path(source).parent.name == "app.new":
            raise PermissionError("in use")
        return real(source, destination)

    monkeypatch.setattr(updater.os, "replace", stuck)
    code, lines = run(machine, "v1.23.0")
    assert code == 1
    assert any("the code could not be switched" in line for line in lines)
    assert state(install)["status"] == "rolled_back"
    assert names(install["app"]) == [".venv", "marker-1.22.0", "pyproject.toml"]
    assert leftovers(install) == []


class Crash(BaseException):
    """A hard kill (the power went, the process was ended): nothing after it runs."""


def killed(monkeypatch) -> None:
    """A Crash ends the process: not even the rollback runs."""

    def gone(*_args: Any) -> str:
        raise Crash()

    monkeypatch.setattr(updater.Update, "roll_back", gone)


@pytest.mark.parametrize("interruption", [KeyboardInterrupt, SystemExit])
def test_ctrl_c_during_uv_sync_rolls_the_code_back_before_it_ends_the_run(install, github, interruption):
    # Before: only Exception was caught, so Ctrl+C (or closing the window) left app/ with the new
    # code, no environment and the switch record; TOW started from it on the next start.
    github.release("v1.23.0", tarball("1.23.0"))
    machine = Machine(install["app"], github)
    real = machine.uv_sync

    def interrupted() -> None:
        real()
        if machine.syncs == 1:
            raise interruption()

    machine.uv_sync = interrupted  # type: ignore[method-assign]

    lines: list[str] = []
    with pytest.raises(interruption):
        updater.update("v1.23.0", system=machine, app=machine.app, say=lines.append, health_timeout=5)

    assert names(install["app"]) == [".venv", "marker-1.22.0", "pyproject.toml"]
    assert not (install["root"] / ".update-switch.json").exists()
    assert leftovers(install) == []
    assert state(install)["status"] == "rolled_back"
    assert machine.calls[-1] == "spawn tow run 1.22.0"


def test_ctrl_c_waits_while_the_code_is_switched(install, github):
    import signal

    github.release("v1.23.0", tarball("1.23.0"))
    machine = Machine(install["app"], github)
    real = machine.uv_sync

    def pressed() -> None:
        signal.raise_signal(signal.SIGINT)  # the owner presses Ctrl+C in the update's window
        real()

    machine.uv_sync = pressed  # type: ignore[method-assign]
    code, lines = run(machine, "v1.23.0")
    assert code == 0, lines
    assert signal.getsignal(signal.SIGINT) is signal.default_int_handler  # given back afterwards


@pytest.mark.parametrize("cut_after", [2, 4, 7])
def test_a_hard_crash_in_the_archive_switch_is_recovered_on_next_run(install, github, monkeypatch, cut_after):
    github.release("v1.23.0", tarball("1.23.0"))
    machine = Machine(install["app"], github)
    real = updater.os.replace
    count = 0

    def cut(source, destination):
        nonlocal count
        result = real(source, destination)
        if Path(source).parent.name in {"app", "app.new"}:
            count += 1
            if count == cut_after:
                raise Crash()
        return result

    monkeypatch.setattr(updater.os, "replace", cut)
    killed(monkeypatch)
    with pytest.raises(Crash):
        run(machine, "v1.23.0")
    monkeypatch.setattr(updater.os, "replace", real)
    assert (install["root"] / ".update-switch.json").exists()
    assert (install["root"] / "runtime" / "update.py").is_file()

    recovered = Machine(install["app"], github, supervisor=False)
    code, _lines = run(recovered, "not-a-tag")

    assert code == 2  # the requested tag is invalid, after the earlier switch was repaired
    assert names(install["app"]) == [".venv", "marker-1.22.0", "pyproject.toml"]
    assert not (install["root"] / ".update-switch.json").exists()
    assert leftovers(install) == []
    assert recovered.calls == ["spawn tow run 1.22.0"]


def _cut_off_switch(install, github, monkeypatch) -> None:
    """An update to v1.23.0 killed after it moved the first entries of the code."""
    github.release("v1.23.0", tarball("1.23.0"))
    real = updater.os.replace

    def cut(source, destination):
        result = real(source, destination)
        if Path(source).parent.name == "app.new":
            raise Crash()
        return result

    monkeypatch.setattr(updater.os, "replace", cut)
    killed(monkeypatch)
    with pytest.raises(Crash):
        run(Machine(install["app"], github), "v1.23.0")
    monkeypatch.setattr(updater.os, "replace", real)
    assert (install["root"] / ".update-switch.json").exists()


def test_a_recovery_whose_previous_version_does_not_answer_is_a_failure_not_a_refusal(install, github, monkeypatch):
    # Before: exit code 2, "refused before anything changed", while the code had been put back.
    _cut_off_switch(install, github, monkeypatch)
    machine = Machine(install["app"], github, supervisor=False, silent_version="1.22.0")

    code, lines = run(machine, "v1.23.0")

    assert code == 1
    assert machine.calls == ["spawn tow run 1.22.0"]  # recovery only: the update itself never began
    record = state(install)
    assert record["status"] == "recovery_failed"
    assert "did not answer as 1.22.0" in record["error"]
    assert any(line.startswith("the cut-off update could not be undone") for line in lines)
    assert names(install["app"]) == [".venv", "marker-1.22.0", "pyproject.toml"]
    assert (install["root"] / ".update-switch.json").exists()  # the next run tries again


def test_a_recovery_that_cannot_stop_tow_starts_it_again(install, github, monkeypatch):
    # Before: the stop failed outside run()'s try/finally, and TOW stayed stopped.
    github.release("v1.23.0", tarball("1.23.0"))
    original = updater.Update.start_and_check

    def start_then_crash(work, version):
        result = original(work, version)
        if version == "1.23.0":
            raise Crash()  # killed during the health check: the new version keeps running
        return result

    monkeypatch.setattr(updater.Update, "start_and_check", start_then_crash)
    killed(monkeypatch)
    with pytest.raises(Crash):
        run(Machine(install["app"], github), "v1.23.0")
    monkeypatch.setattr(updater.Update, "start_and_check", original)
    machine = Machine(install["app"], github)
    machine.port_open = lambda port: True  # type: ignore[method-assign]  # never free

    code, lines = run(machine, "v1.23.0")

    assert code == 1
    assert machine.calls == ["stop request", "spawn tow run 1.23.0"]  # what it found is running again
    assert state(install)["status"] == "recovery_failed"
    assert any("port 18999 is still in use" in line for line in lines)
    assert (install["root"] / ".update-switch.json").exists()


def test_a_crash_after_new_code_changes_data_restores_the_snapshot_too(install, github, monkeypatch):
    github.release("v1.23.0", tarball("1.23.0"))
    original = updater.Update.start_and_check

    def start_then_crash(work, version):
        result = original(work, version)
        if version == "1.23.0":
            (install["root"] / "data" / "state.json").write_text('{"schema": 2}', encoding="utf-8")
            (install["root"] / "config.yaml").write_text("port: 18999\nschema: 2\n", encoding="utf-8")
            raise Crash()
        return result

    monkeypatch.setattr(updater.Update, "start_and_check", start_then_crash)
    killed(monkeypatch)
    with pytest.raises(Crash):
        run(Machine(install["app"], github), "v1.23.0")
    monkeypatch.setattr(updater.Update, "start_and_check", original)
    assert (install["root"] / ".update-switch.json").exists()

    recovered = Machine(install["app"], github)
    code, _lines = run(recovered, "not-a-tag")

    assert code == 2
    assert (install["root"] / "data" / "state.json").read_text(encoding="utf-8") == '{"topics": []}'
    assert (install["root"] / "config.yaml").read_text(encoding="utf-8") == "port: 18999\nlanguage: en\n"
    assert names(install["app"]) == [".venv", "marker-1.22.0", "pyproject.toml"]
    assert state(install)["status"] == "recovered"
    assert not (install["root"] / ".update-switch.json").exists()


def test_data_changed_after_a_cut_off_update_is_never_replaced_unasked(install, github, monkeypatch):
    # Before: the snapshot from before the update was put back over whatever the new version had
    # written since it was left running - days of data, silently.
    github.release("v1.23.0", tarball("1.23.0"))
    original = updater.Update.start_and_check

    def start_then_crash(work, version):
        result = original(work, version)
        if version == "1.23.0":
            raise Crash()  # killed during the health check: the new version keeps running
        return result

    monkeypatch.setattr(updater.Update, "start_and_check", start_then_crash)
    killed(monkeypatch)
    with pytest.raises(Crash):
        run(Machine(install["app"], github), "v1.23.0")
    monkeypatch.setattr(updater.Update, "start_and_check", original)
    state_file = install["root"] / "data" / "state.json"
    state_file.write_text('{"topics": ["added later"]}', encoding="utf-8")
    later = state_file.stat().st_mtime + 2 * (5 + updater.STOP_GRACE)  # long after the health check
    os.utime(state_file, (later, later))

    machine = Machine(install["app"], github)
    code, lines = run(machine, "v1.23.0")

    assert code == 2
    assert machine.calls == []  # nothing stopped
    assert "state.json" in lines[-1]
    assert "--discard-newer-data" in lines[-1]
    assert state_file.read_text(encoding="utf-8") == '{"topics": ["added later"]}'
    assert "marker-1.23.0" in names(install["app"])
    assert (install["root"] / ".update-switch.json").exists()

    # Asked for: the snapshot goes back over it.
    code, _lines = run(Machine(install["app"], github), "not-a-tag", discard_newer_data=True)
    assert code == 2  # the tag, after the recovery
    assert state(install)["status"] == "recovered"
    assert state_file.read_text(encoding="utf-8") == '{"topics": []}'
    assert names(install["app"]) == [".venv", "marker-1.22.0", "pyproject.toml"]


@pytest.mark.allow_system  # this Python runs the copy; a refused tag ends it before TOW is stopped or started
def test_the_copy_in_runtime_updates_the_install_it_belongs_to(install):
    # The start files run <TOW>/runtime/update.py while a switch record exists: it must find
    # <TOW>/app, not take <TOW> for the code (which refused every run as "not a runtime install").
    root = install["root"]
    copy = root / "runtime" / "update.py"
    copy.parent.mkdir()
    shutil.copy2(SCRIPT, copy)
    (root / "app.new").mkdir()
    record = {"format": "tow-update-switch/v1", "phase": "accepted", "old": [], "new": []}
    (root / ".update-switch.json").write_text(json.dumps(record), encoding="utf-8")

    done = subprocess.run(
        [sys.executable, str(copy), "--ref", "main"], capture_output=True, text=True, timeout=120, check=False
    )

    assert done.returncode == 2, done.stdout + done.stderr
    assert "not to main" in done.stdout
    assert "not a runtime install" not in done.stdout
    assert not (root / ".update-switch.json").exists()  # the accepted switch was finished
    assert leftovers(install) == []


def test_a_damaged_update_snapshot_blocks_crash_recovery(install, github, monkeypatch):
    github.release("v1.23.0", tarball("1.23.0"))
    real = updater.os.replace

    def cut(source, destination):
        result = real(source, destination)
        if Path(source).parent.name == "app" and Path(source).name == "pyproject.toml":
            raise Crash()
        return result

    monkeypatch.setattr(updater.os, "replace", cut)
    killed(monkeypatch)
    with pytest.raises(Crash):
        run(Machine(install["app"], github), "v1.23.0")
    monkeypatch.setattr(updater.os, "replace", real)
    snapshot = next((install["root"] / "backup").glob("update-*-before-*"))
    (snapshot / "data" / "state.json").write_text("damaged", encoding="utf-8")

    code, lines = run(Machine(install["app"], github, supervisor=False), "not-a-tag")

    assert code == 2
    assert "snapshot is damaged: state.json" in lines[-1]
    assert (install["root"] / ".update-switch.json").exists()


def test_only_one_previous_version_is_kept(install, github):
    for version in ("1.23.0", "1.24.0"):
        github.release(f"v{version}", tarball(version))
        code, lines = run(Machine(install["app"], github), f"v{version}")
        assert code == 0, lines
    assert names(install["app"])[1] == "marker-1.24.0"
    assert "marker-1.23.0" in names(install["root"] / "app.prev")
    assert sorted(p.name for p in install["root"].iterdir() if p.name.startswith("app")) == ["app", "app.prev"]


def test_a_local_archive_is_checked_against_local_sums(install, github, tmp_path):
    archive = tmp_path / "tow-source.tar.gz"
    archive.write_bytes(tarball("1.23.0"))
    good = tmp_path / "SHA256SUMS"
    good.write_bytes(sums(**{"tow-source.tar.gz": archive.read_bytes()}))
    bad = tmp_path / "BAD"
    bad.write_bytes(sums(**{"tow-source.tar.gz": b"other"}))
    code, lines = run(Machine(install["app"], github), "v1.23.0", source=archive, sums=bad)
    assert code == 2
    assert "does not match" in lines[-1]
    code, lines = run(Machine(install["app"], github), "v1.23.0", source=archive, sums=good)
    assert code == 0, lines
    assert github.asked == []  # nothing was downloaded
    assert archive.exists()  # the given file is left where it was


def test_a_git_install_never_downloads(tmp_path):
    (tmp_path / "TOW" / "app" / ".git").mkdir(parents=True)
    work = updater.Update(updater.System(tmp_path / "TOW" / "app"), "v1.23.0", say=lambda _line: None)
    assert work.code.kind == "git"


def test_sums_are_read_as_sha256sum_writes_them():
    digest = "ab" * 32
    text = f"{digest}  tow-source.tar.gz\n{digest.upper()} *TOW-windows-x64.zip\nnot a line\n"
    assert updater.parse_sums(text) == {"tow-source.tar.gz": digest, "TOW-windows-x64.zip": digest}
