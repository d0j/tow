"""Crash cleanup smoke (TC-04): a real install whose `tow run` dies, and a port that is not TOW's.

    python scripts/crash-smoke.py (--zip TOW-windows-x64.zip | --source tow-source.tar.gz) [--port 18959]

The candidate is installed into a throwaway folder with a Cyrillic name (Windows: the bundle,
unpacked and set up offline; Linux and macOS: this checkout's install/install.sh with the source
archive) on --port (never 8787) and started with `tow start`. Then, kept apart:

0. permissions (TC-09): scripts/permissions-smoke.py on the running install, with its repair
   and failure legs;
1. **killed parent**: `tow run` (the supervisor, data/run/status.json "pid") is killed hard -
   TerminateProcess / SIGKILL, never its tree - and its web server child ("server.pid") must be
   gone and the port free within 30 s (Windows job object, Linux PR_SET_PDEATHSIG, macOS the
   server's parent poll); the next `tow start` is clean;
2. **orphan takeover**: a web server of this install left on the port by a dead supervisor is
   taken over by the next `tow start` (stopped, then TOW starts): on Linux and macOS a real one
   (the server is frozen with SIGSTOP, so it cannot notice, then the supervisor is killed); on
   Windows the job object leaves none, so the record of a stopped TOW is pointed at a server
   started by hand - a synthetic stale record, said so in the log;
3. **unrelated listener**: a plain listener of this script on the port survives `tow start`,
   which exits 3 naming the port, and still accepts connections; once it is closed TOW starts.

Only processes of this install are ever killed: each PID comes from the install's own records
and its program must lie inside the install's folder (checked before every kill), or it is this
script's own child. Standard library only; TOW is stopped and the folder removed unless --keep.
"""

from __future__ import annotations

import argparse
import contextlib
import hashlib
import importlib.util
import json
import os
import re
import shutil
import signal
import socket
import subprocess
import sys
import tempfile
import time
import zipfile
from pathlib import Path
from urllib.request import ProxyHandler, build_opener

WINDOWS = os.name == "nt"
ROOT = Path(__file__).resolve().parents[1]
GONE_SEC = 30  # the server's parent poll (2 s) plus its grace for a request (15 s), with margin
TAKEOVER_LINE = "was left running"  # run.log: "a web server of this install was left running (pid N)"


class SmokeError(RuntimeError):
    pass


def say(text: str) -> None:
    print(f"crash-smoke: {text}", flush=True)


def expect(condition: object, text: str) -> None:
    if not condition:
        raise SmokeError(text)


# --- processes, by PID, inside the install only ---------------------------------------------------


def _kernel32():
    import ctypes
    from ctypes import wintypes

    kernel32 = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel32.OpenProcess.restype = wintypes.HANDLE
    kernel32.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    kernel32.QueryFullProcessImageNameW.argtypes = [
        wintypes.HANDLE,
        wintypes.DWORD,
        wintypes.LPWSTR,
        ctypes.POINTER(wintypes.DWORD),
    ]
    kernel32.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    kernel32.TerminateProcess.argtypes = [wintypes.HANDLE, wintypes.UINT]
    kernel32.CloseHandle.argtypes = [wintypes.HANDLE]
    return kernel32


_QUERY = 0x1000  # PROCESS_QUERY_LIMITED_INFORMATION
_TERMINATE = 0x0001
_STILL_ACTIVE = 259


def alive(pid: int) -> bool:
    if pid <= 0:
        return False
    if WINDOWS:
        import ctypes
        from ctypes import wintypes

        kernel32 = _kernel32()
        handle = kernel32.OpenProcess(_QUERY, False, pid)
        if not handle:
            return False
        try:
            code = wintypes.DWORD()
            return bool(kernel32.GetExitCodeProcess(handle, ctypes.byref(code))) and code.value == _STILL_ACTIVE
        finally:
            kernel32.CloseHandle(handle)
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True
    state = subprocess.run(["ps", "-o", "stat=", "-p", str(pid)], capture_output=True, text=True, check=False)
    return not state.stdout.strip().startswith("Z")  # a zombie no longer runs


def program(pid: int) -> str:
    """The process's program (Windows) or command line (POSIX), "" when unknown."""
    if WINDOWS:
        import ctypes
        from ctypes import wintypes

        kernel32 = _kernel32()
        handle = kernel32.OpenProcess(_QUERY, False, pid)
        if not handle:
            return ""
        try:
            size = wintypes.DWORD(32768)
            buffer = ctypes.create_unicode_buffer(size.value)
            if not kernel32.QueryFullProcessImageNameW(handle, 0, buffer, ctypes.byref(size)):
                return ""
            return buffer.value
        finally:
            kernel32.CloseHandle(handle)
    done = subprocess.run(
        ["ps", "-ww", "-o", "command=", "-p", str(pid)],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        check=False,
    )
    return done.stdout.strip()


def ours(pid: int, root: Path) -> bool:
    """The process runs this install's program (its Python lies inside the install's folder)."""
    text = program(pid)
    if not text:
        return False
    if WINDOWS:
        return os.path.normcase(text).startswith(os.path.normcase(str(root)) + os.sep)
    return str(root) + "/" in text or str(root.resolve()) + "/" in text


def kill_hard(pid: int, root: Path, what: str) -> None:
    """End one process of this install at once - no tree, no signal it could handle."""
    expect(ours(pid, root), f"{what} (pid {pid}) is not a process of {root}: not touched ({program(pid)!r})")
    say(f"killing {what} (pid {pid}) hard")
    if WINDOWS:
        kernel32 = _kernel32()
        handle = kernel32.OpenProcess(_TERMINATE | _QUERY, False, pid)
        expect(handle, f"cannot open {what} (pid {pid})")
        try:
            expect(kernel32.TerminateProcess(handle, 1), f"TerminateProcess of {what} failed")
        finally:
            kernel32.CloseHandle(handle)
    else:
        os.kill(pid, signal.SIGKILL)


def freeze(pid: int, root: Path, what: str) -> None:
    expect(not WINDOWS, "SIGSTOP is POSIX only")
    expect(ours(pid, root), f"{what} (pid {pid}) is not a process of {root}: not touched")
    os.kill(pid, signal.SIGSTOP)


def port_open(port: int) -> bool:
    with contextlib.closing(socket.socket(socket.AF_INET, socket.SOCK_STREAM)) as probe:
        probe.settimeout(2)
        return probe.connect_ex(("127.0.0.1", port)) == 0


def wait_until(condition, seconds: float) -> bool:
    deadline = time.monotonic() + seconds
    while time.monotonic() < deadline:
        if condition():
            return True
        time.sleep(0.5)
    return bool(condition())


# --- the install ----------------------------------------------------------------------------------


def clean_environment() -> dict[str, str]:
    env = {
        name: value
        for name, value in os.environ.items()
        if not name.upper().startswith(("TOW_", "UV_"))
        and name.upper() not in {"VIRTUAL_ENV", "PYTHONHOME", "PYTHONPATH"}
    }
    env["TOW_NO_BROWSER"] = "1"
    return env


class Install:
    def __init__(self, base: Path, port: int):
        self.folder = base / f"tow crash smoke Тест {os.getpid()}"
        self.root = self.folder / "TOW"
        self.port = port
        self.env = clean_environment()
        self.opener = build_opener(ProxyHandler({}))

    def run(self, argv: list[str], *, env: dict[str, str] | None = None, timeout: float = 900) -> tuple[int, str]:
        say("$ " + " ".join(f'"{part}"' if " " in part else part for part in argv))
        if WINDOWS and argv[0].lower().endswith(".cmd"):
            argv = ["cmd.exe", "/d", "/c", "call", *argv]
        # Output to a file, not a pipe: a started TOW must not hold a pipe of ours.
        with tempfile.TemporaryFile() as output:
            done = subprocess.run(
                argv, env=env or self.env, stdin=subprocess.DEVNULL, stdout=output, stderr=subprocess.STDOUT,
                timeout=timeout, check=False,
            )  # fmt: skip
            output.seek(0)
            text = output.read().decode("utf-8", errors="replace")
        print(text, flush=True)
        return done.returncode, text

    def tow(self, *args: str, timeout: float = 900) -> tuple[int, str]:
        return self.run(
            [str(self.root / "app" / "scripts" / ("tow.cmd" if WINDOWS else "tow")), *args], timeout=timeout
        )

    def health(self) -> dict | None:
        try:
            with self.opener.open(f"http://127.0.0.1:{self.port}/healthz", timeout=3) as response:
                value = json.loads(response.read().decode("utf-8"))
        except OSError, ValueError:
            return None
        return value if isinstance(value, dict) and value.get("ok") is True else None

    def status(self) -> dict:
        path = self.root / "data" / "run" / "status.json"
        return json.loads(path.read_text(encoding="utf-8"))

    def pids(self) -> tuple[int, int]:
        """The supervisor's and its web server's PID, as `tow run` records them."""
        status = self.status()
        server = status.get("server") or {}
        supervisor, child = status.get("pid"), server.get("pid")
        expect(isinstance(supervisor, int) and supervisor > 0, f"status.json has no supervisor pid: {status}")
        expect(isinstance(child, int) and child > 0, f"status.json has no server pid: {status}")
        return supervisor, child

    def install(self, zip_path: Path | None, source: Path | None) -> None:
        self.folder.mkdir(parents=True)
        if WINDOWS:
            expect(zip_path is not None, "Windows installs the bundle: give --zip")
            assert zip_path is not None
            with zipfile.ZipFile(zip_path) as bundle:
                bundle.extractall(self.folder)
            # The owner's unpacked folder is theirs; an elevated runner's belongs to Administrators.
            _permissions().own(self.root, recursive=True)
            config = self.root / "config.yaml"
            config.write_text(
                re.sub(r"(?m)^port:.*$", f"port: {self.port}", config.read_text(encoding="utf-8")), encoding="utf-8"
            )
            self.env["UV_OFFLINE"] = "1"  # the bundle carries everything
            code, _ = self.tow("setup")
            expect(code == 0, f"tow setup exited with {code}")
        else:
            expect(source is not None, "Linux and macOS install with install.sh: give --source")
            assert source is not None
            sums = self.folder / "SHA256SUMS"
            sums.write_text(f"{hashlib.sha256(source.read_bytes()).hexdigest()}  tow-source.tar.gz\n", encoding="ascii")
            env = dict(self.env, TOW_INSTALL_SOURCE=str(source), TOW_INSTALL_SUMS=str(sums))
            installer = ROOT / "install" / "install.sh"
            code, _ = self.run(["sh", str(installer), "--dir", str(self.root), "--port", str(self.port)], env=env)
            expect(code == 0, f"install.sh exited with {code}")

    def start(self) -> None:
        code, output = self.tow("start")
        expect(code == 0, f"tow start exited with {code}")
        expect(
            wait_until(lambda: self.health() is not None, 60), f"TOW does not answer on {self.port}: {output[-300:]}"
        )

    def stop(self) -> None:
        code, _ = self.tow("stop")
        expect(code == 0 and wait_until(lambda: self.health() is None, 120), "TOW still answers after `tow stop`")

    def run_log(self) -> str:
        path = self.root / "data" / "logs" / "run.log"
        return path.read_text(encoding="utf-8", errors="replace") if path.is_file() else ""


def _permissions():
    spec = importlib.util.spec_from_file_location("permissions_smoke", Path(__file__).with_name("permissions-smoke.py"))
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


# --- the legs -------------------------------------------------------------------------------------


def leg_permissions(install: Install, *, strict: bool) -> None:
    code = _permissions().main(["--root", str(install.root), "--legs", *(["--strict"] if strict else [])])
    expect(code == 0, "the permissions of the install (above)")
    say("0. permissions: private after install and start; repair and failure kept apart")


def leg_killed_parent(install: Install) -> None:
    supervisor, server = install.pids()
    expect(
        alive(server) and ours(server, install.root), f"the web server (pid {server}) is not running from the install"
    )
    kill_hard(supervisor, install.root, "tow run")
    expect(wait_until(lambda: not alive(supervisor), 15), f"tow run (pid {supervisor}) survived the kill")
    expect(
        wait_until(lambda: not alive(server) and not port_open(install.port), GONE_SEC),
        f"the web server (pid {server}) outlived its killed parent: alive {alive(server)},"
        f" port open {port_open(install.port)}",
    )
    say(f"1. killed parent: the web server and port {install.port} were gone with it")
    install.start()
    new_supervisor, new_server = install.pids()
    expect(new_supervisor != supervisor and new_server != server, "the next start reused the dead processes' record")
    say("   the next `tow start` is clean")


def _start_server_by_hand(install: Install) -> subprocess.Popen:
    """Windows: `tow serve` of this install started like the supervisor starts it, without a parent
    to follow - what a dead supervisor's server is, as the job object never leaves one."""
    venv = install.root / "app" / ".venv" / "Scripts" / "python.exe"
    log = install.root / "data" / "logs" / "serve.log"
    flags = subprocess.CREATE_NEW_PROCESS_GROUP | 0x00000008  # DETACHED_PROCESS
    return subprocess.Popen(
        [str(venv), "-m", "tow", "serve", "--log-file", str(log)],
        cwd=install.root,
        env=dict(install.env, TOW_ROOT=str(install.root)),
        stdin=subprocess.DEVNULL,
        stdout=subprocess.DEVNULL,
        stderr=subprocess.DEVNULL,
        creationflags=flags,
    )


def _listener_pid(install: Install, started: int) -> int:
    """The PID that listens on the port among the hand-started server and its children (the venv's
    python.exe is a launcher that runs the base Python as its child)."""
    script = (
        "$c = Get-NetTCPConnection -State Listen -LocalPort $env:TOW_SMOKE_PORT -ErrorAction SilentlyContinue;"
        " ($c | Select-Object -First 1).OwningProcess"
    )
    env = dict(os.environ, TOW_SMOKE_PORT=str(install.port))
    done = subprocess.run(
        ["powershell.exe", "-NoProfile", "-NonInteractive", "-Command", script],
        capture_output=True, text=True, env=env, timeout=60, check=False,
    )  # fmt: skip
    found = done.stdout.strip()
    expect(found.isdigit(), f"nothing listens on {install.port} after the hand start of pid {started}: {done.stderr}")
    return int(found)


def leg_orphan(install: Install) -> None:
    if WINDOWS:
        install.stop()
        started = _start_server_by_hand(install)
        expect(wait_until(lambda: install.health() is not None, 120), "the hand-started `tow serve` does not answer")
        orphan = _listener_pid(install, started.pid)
        expect(ours(orphan, install.root), f"the listener (pid {orphan}) is not this install's")
        status = install.status()
        status.setdefault("server", {})["pid"] = orphan
        (install.root / "data" / "run" / "status.json").write_text(json.dumps(status), encoding="utf-8")
        say(f"2. synthetic stale record (Windows leaves no real orphan): status.json names the server pid {orphan}")
    else:
        supervisor, orphan = install.pids()
        freeze(orphan, install.root, "the web server")
        kill_hard(supervisor, install.root, "tow run")
        expect(wait_until(lambda: not alive(supervisor), 15), f"tow run (pid {supervisor}) survived the kill")
        time.sleep(5)
        expect(alive(orphan) and port_open(install.port), "the frozen web server did not stay on the port")
        say(f"2. a real orphan: the frozen web server (pid {orphan}) holds port {install.port}, its supervisor is dead")
    before = install.run_log()
    install.start()
    expect(wait_until(lambda: not alive(orphan), 30), f"the orphan (pid {orphan}) still runs after the start")
    after = install.run_log()[len(before) :]
    expect(TAKEOVER_LINE in after, f"run.log does not say the orphan was taken over: {after[-500:]!r}")
    _supervisor, server = install.pids()
    expect(server != orphan, "the new start names the orphan as its server")
    say("   the next `tow start` took it over and runs its own server")


# Another program's listener. On Linux and macOS the stopped server's closed connections stay in
# TIME_WAIT on the port for a minute, and a bind without SO_REUSEADDR fails there although nothing
# listens (as every server sets it, TOW's too); it still never binds over a listening socket. On
# Windows SO_REUSEADDR would let it take a port that is in use, so it is not set there.
LISTENER = (
    "import os, socket, sys\n"
    "s = socket.socket()\n"
    "if os.name != 'nt':\n"
    "    s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)\n"
    "s.bind(('127.0.0.1', int(sys.argv[1]))); s.listen(16)\n"
    "print('listening', flush=True)\n"
    "while True:\n"
    "    c, _ = s.accept(); c.close()\n"
)


def leg_unrelated(install: Install) -> None:
    install.stop()
    expect(not port_open(install.port), f"port {install.port} is still in use after `tow stop`")
    listener = subprocess.Popen(
        [sys.executable, "-c", LISTENER, str(install.port)], stdout=subprocess.PIPE, stdin=subprocess.DEVNULL, text=True
    )
    try:
        expect(
            listener.stdout is not None and listener.stdout.readline().strip() == "listening",
            "the listener did not start",
        )
        status, output = install.tow("start", "--wait", "30")
        expect(status == 3, f"tow start over another program's port exited with {status}, not 3")
        expect(str(install.port) in output, f"tow start did not name port {install.port}")
        expect(listener.poll() is None, "the unrelated listener was ended by `tow start`")
        expect(port_open(install.port), "the unrelated listener no longer accepts connections")
        expect(install.health() is None, "something answers /healthz on the unrelated listener's port")
        say(f"3. unrelated listener: it survived, `tow start` exited 3 naming port {install.port}")
    finally:
        listener.kill()  # this script's own child
        listener.wait(30)
    expect(wait_until(lambda: not port_open(install.port), 30), "the port stays in use after the listener ended")
    install.start()
    say("   once it is gone, TOW starts")


def _utf8_output() -> None:
    for stream in (sys.stdout, sys.stderr):
        with contextlib.suppress(AttributeError, ValueError):
            stream.reconfigure(encoding="utf-8", errors="replace")


def main(argv: list[str] | None = None) -> int:
    _utf8_output()
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    parser.add_argument("--zip", type=Path, help="Windows: the bundle (TOW-windows-x64.zip)")
    parser.add_argument("--source", type=Path, help="Linux, macOS: the source archive (tow-source.tar.gz)")
    parser.add_argument("--port", type=int, default=18959, help="the test install's port (never 8787)")
    parser.add_argument("--strict", action="store_true", help="the permissions failure leg must run")
    parser.add_argument("--keep", action="store_true", help="keep the test install's folder")
    args = parser.parse_args(argv)
    if args.port == 8787:
        parser.error("never on 8787 (a real TOW may run there)")
    base = Path(os.environ.get("RUNNER_TEMP") or tempfile.gettempdir())
    install = Install(base, args.port)
    if port_open(args.port):
        parser.error(f"port {args.port} is in use")
    failed = True
    try:
        install.install(args.zip.resolve() if args.zip else None, args.source.resolve() if args.source else None)
        install.start()
        leg_permissions(install, strict=args.strict)
        leg_killed_parent(install)
        leg_orphan(install)
        leg_unrelated(install)
        install.stop()
        failed = False
        say("passed")
    except (SmokeError, OSError, ValueError, subprocess.SubprocessError, zipfile.BadZipFile) as exc:
        say(f"FAILED: {exc}")
    finally:
        if (install.root / "app" / "scripts").is_dir():
            with contextlib.suppress(Exception):
                if install.health() is not None:
                    install.tow("stop")
        if failed:
            say("--- data/logs/run.log (end)")
            print("\n".join(install.run_log().splitlines()[-40:]), flush=True)
        if not args.keep:
            for _attempt in range(10):
                shutil.rmtree(install.folder, ignore_errors=True)
                if not install.folder.exists():
                    break
                time.sleep(1)
    return 1 if failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
