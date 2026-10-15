"""Standalone, loopback-only lazy broker for an explicitly installed local service.

Copied beside its manifest and run with the install's base Python. No TOW imports:
updates may replace app/ while an already issued public-page request finishes.
"""

from __future__ import annotations

import ctypes
import hashlib
import http.client
import ipaddress
import json
import os
import re
import shutil
import socket
import stat
import subprocess
import sys
import threading
import time
from collections.abc import Callable
from contextlib import closing
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any
from urllib.parse import urlsplit

MAX_BODY = 1024 * 1024
JOB_HANDLE: int | None = None


class StartupPending(TimeoutError):
    """This request expired, but the owned cold start may finish within its own cap."""


class BasicLimits(ctypes.Structure):
    _fields_ = [
        ("process_time", ctypes.c_longlong),
        ("job_time", ctypes.c_longlong),
        ("flags", ctypes.c_uint32),
        ("min_working", ctypes.c_size_t),
        ("max_working", ctypes.c_size_t),
        ("active", ctypes.c_uint32),
        ("affinity", ctypes.c_size_t),
        ("priority", ctypes.c_uint32),
        ("scheduling", ctypes.c_uint32),
    ]


class ExtendedLimits(ctypes.Structure):
    _fields_ = [
        ("basic", BasicLimits),
        ("io", ctypes.c_ulonglong * 6),
        ("process_memory", ctypes.c_size_t),
        ("job_memory", ctypes.c_size_t),
        ("peak_process", ctypes.c_size_t),
        ("peak_job", ctypes.c_size_t),
    ]


def bind_owned_children() -> None:
    """Windows-managed processes must die even if their broker is forcibly terminated."""
    global JOB_HANDLE
    if sys.platform != "win32":
        raise OSError("native demand management is not installed for this platform")
    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.CreateJobObjectW.restype = ctypes.c_void_p
    kernel.GetCurrentProcess.restype = ctypes.c_void_p
    handle = kernel.CreateJobObjectW(None, None)
    if not handle:
        raise OSError("local service job could not be created")
    limits = ExtendedLimits()
    limits.basic.flags = 0x2000  # KILL_ON_JOB_CLOSE; native children cannot break away
    if not kernel.SetInformationJobObject(
        ctypes.c_void_p(handle), 9, ctypes.byref(limits), ctypes.sizeof(limits)
    ) or not kernel.AssignProcessToJobObject(ctypes.c_void_p(handle), ctypes.c_void_p(kernel.GetCurrentProcess())):
        kernel.CloseHandle(ctypes.c_void_p(handle))
        raise OSError("local service process containment could not be confirmed")
    JOB_HANDLE = int(handle)  # retain this non-inheritable handle until broker exit


def plain(path: Path, *, directory: bool = False) -> bool:
    info = path.lstat()
    appropriate = stat.S_ISDIR(info.st_mode) if directory else stat.S_ISREG(info.st_mode)
    return appropriate and not getattr(info, "st_file_attributes", 0) & stat.FILE_ATTRIBUTE_REPARSE_POINT


def file_sha256(path: Path) -> str:
    with path.open("rb") as source:
        return hashlib.file_digest(source, "sha256").hexdigest()


def read_manifest(folder: Path, digest: Callable[[Path], str] = file_sha256) -> dict[str, Any]:
    """The validated manifest of an installed module, its launcher's SHA-256 included.

    The digest detects a changed launcher executable; it is not integrity of the whole module
    folder nor proof of its publisher. ``digest`` lets a display reuse a digest of unchanged
    file metadata; every start of the module hashes the file again (the default).
    """
    if not plain(folder, directory=True):
        raise ValueError("redirected local service folder")
    path = folder / "manifest.json"
    if not plain(path) or path.stat().st_size > 4096:
        raise ValueError("invalid local service manifest")
    try:
        value = json.loads(path.read_text(encoding="utf-8"))
    except RecursionError as exc:
        raise ValueError("invalid local service manifest nesting") from exc
    if not isinstance(value, dict) or set(value) != {
        "format",
        "kind",
        "url",
        "executable",
        "sha256",
        "version",
        "idle_sec",
    }:
        raise ValueError("invalid local service manifest fields")
    if value["format"] != "tow-local-service/v1" or value["kind"] != "flaresolverr":
        raise ValueError("unsupported local service module")
    if any(not isinstance(value[name], str) for name in ("url", "executable", "sha256", "version")):
        raise ValueError("invalid local service manifest types")
    target = urlsplit(value["url"])
    if (
        target.scheme != "http"
        or target.hostname != "127.0.0.1"
        or target.username
        or target.password
        or target.path != "/v1"
        or target.query
        or target.fragment
        or not target.port
    ):
        raise ValueError("local service API must be loopback HTTP")
    if type(value["idle_sec"]) is not int or not 30 <= value["idle_sec"] <= 900:
        raise ValueError("invalid local service idle interval")
    if not isinstance(value["version"], str) or not re.fullmatch(r"[0-9]+\.[0-9]+\.[0-9]+", value["version"]):
        raise ValueError("invalid local service version")
    name = value["executable"]
    if name not in {"bin/flaresolverr.exe", "bin/flaresolverr"}:
        raise ValueError("invalid local service executable")
    if not isinstance(value["sha256"], str) or not re.fullmatch(r"[0-9a-f]{64}", value["sha256"]):
        raise ValueError("invalid local service digest")
    binary = folder / name
    if not plain(binary.parent, directory=True) or not plain(binary) or binary.stat().st_size > 512 * MAX_BODY:
        raise ValueError("invalid local service binary")
    if digest(binary) != value["sha256"]:
        raise ValueError("local service binary changed")
    return value


def validate_request(value: object) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) - {"cmd", "url", "maxTimeout", "returnOnlyCookies", "disableMedia"}:
        raise ValueError("invalid anonymous service request")
    if value.get("cmd") != "request.get" or value.get("returnOnlyCookies") is not True:
        raise ValueError("only anonymous cookie requests are supported")
    timeout = value.get("maxTimeout")
    if type(timeout) is not int or not 1000 <= timeout <= 90000:
        raise ValueError("invalid browser time budget")
    if "disableMedia" in value and not isinstance(value["disableMedia"], bool):
        raise ValueError("invalid media option")
    url = value.get("url")
    if not isinstance(url, str) or len(url) > 2048 or any(c in url for c in "\\ \r\n\t"):
        raise ValueError("invalid anonymous public URL")
    parsed = urlsplit(url)
    if (
        parsed.scheme not in {"http", "https"}
        or not parsed.hostname
        or parsed.username
        or parsed.password
        or parsed.path not in {"/", "/login.php", "/browse.php"}
        or parsed.query
        or parsed.fragment
    ):
        raise ValueError("invalid anonymous public URL")
    return dict(value)


def public_target(url: str) -> None:
    parsed = urlsplit(url)
    answers = socket.getaddrinfo(
        parsed.hostname, parsed.port or (443 if parsed.scheme == "https" else 80), type=socket.SOCK_STREAM
    )
    addresses = [ipaddress.ip_address(str(row[4][0]).split("%", 1)[0]) for row in answers]
    translated = tuple(ipaddress.IPv6Network(prefix) for prefix in ("::/96", "::ffff:0:0:0/96", "64:ff9b::/96"))
    addresses = [
        ipaddress.IPv4Address(address.packed[12:])
        if isinstance(address, ipaddress.IPv6Address)
        and (address.ipv4_mapped is not None or any(address in prefix for prefix in translated))
        else address
        for address in addresses
    ]
    if not addresses or any(
        not address.is_global or address.is_multicast or getattr(address, "is_site_local", False)
        for address in addresses
    ):
        raise ValueError("non-public browser destination")


def safe_environment(folder: Path) -> dict[str, str]:
    """Only browser/OS essentials, never inherit application or account tokens."""
    allowed = {
        "SYSTEMROOT",
        "WINDIR",
        "COMSPEC",
        "PATHEXT",
        "PATH",
        "USERPROFILE",
        "APPDATA",
        "LOCALAPPDATA",
        "PROGRAMFILES",
        "PROGRAMFILES(X86)",
        "PROGRAMW6432",
        "SYSTEMDRIVE",
        "PROGRAMDATA",
        "LANG",
        "LC_ALL",
        "TZ",
    }
    env = {key: value for key, value in os.environ.items() if key.upper() in allowed}
    temp = folder / "temp"
    temp.mkdir(exist_ok=True)
    if not plain(temp, directory=True):
        raise ValueError("redirected local service temporary folder")
    env.update(TEMP=str(temp), TMP=str(temp), TMPDIR=str(temp))
    return env


def native_environment(folder: Path) -> dict[str, str]:
    """Only an idle/absent owned native process may discard its disposable work area."""
    env = safe_environment(folder)
    parent = folder / "temp"
    work = parent / "native"
    if work.parent.resolve() != folder.resolve() / "temp" or not plain(parent, directory=True):
        raise ValueError("redirected native work area")
    if work.exists() or work.is_symlink():
        if not plain(work, directory=True):
            raise ValueError("redirected native work folder")
        # Fixed private child only; rmtree does not traverse Windows directory junctions.
        shutil.rmtree(work)
    work.mkdir()
    env.update(TEMP=str(work), TMP=str(work), TMPDIR=str(work))
    return env


class NativeService:
    def __init__(self, folder: Path, manifest: dict[str, Any]):
        self.folder, self.manifest = folder, manifest
        self.child: subprocess.Popen[bytes] | None = None
        self.port = 0
        self.lock = threading.Lock()
        self.last_used = time.monotonic()
        self.closing = False
        self.next_stop = 0.0
        self.ready = False
        self.start_deadline = 0.0

    def start(self, deadline: float) -> None:
        if time.monotonic() >= deadline:
            raise TimeoutError("local service startup budget exhausted")
        if self.child is not None and self.child.poll() is None and self.ready:
            return
        if self.child is None or self.child.poll() is not None:
            self.launch(deadline)
        assert self.child is not None
        until = min(deadline, self.start_deadline)
        while time.monotonic() < until:
            if self.child.poll() is not None:
                raise OSError("local service exited during startup")
            try:
                with closing(
                    http.client.HTTPConnection("127.0.0.1", self.port, timeout=min(0.5, until - time.monotonic()))
                ) as connection:
                    connection.request("GET", "/")
                    response = connection.getresponse()
                    body = response.read(8193)
                    value = json.loads(body) if len(body) <= 8192 else None
                    if (
                        response.status == 200
                        and isinstance(value, dict)
                        and value.get("version") == self.manifest["version"]
                    ):
                        self.ready = True
                        return
            except (OSError, ValueError, http.client.HTTPException):
                pass
            time.sleep(min(0.1, max(0, until - time.monotonic())))
        if time.monotonic() < self.start_deadline:
            raise StartupPending("local module still warming within startup cap")
        raise TimeoutError("local service startup cap exhausted")

    def launch(self, deadline: float) -> None:
        read_manifest(self.folder)  # re-check the executable before every cold start
        with socket.socket() as listener:
            listener.bind(("127.0.0.1", 0))
            self.port = int(listener.getsockname()[1])
        env = native_environment(self.folder)
        env.update(
            HOST="127.0.0.1",
            PORT=str(self.port),
            HEADLESS="true",
            LOG_LEVEL="warning",
            LOG_HTML="false",
            TEST_URL="about:blank",
        )
        options: dict[str, Any] = {"start_new_session": True}
        if sys.platform == "win32":
            options = {"creationflags": subprocess.CREATE_NO_WINDOW | subprocess.CREATE_NEW_PROCESS_GROUP}
        binary = self.folder / self.manifest["executable"]
        if time.monotonic() >= deadline:
            raise TimeoutError("local service preparation exceeded request budget")
        self.child = subprocess.Popen(
            [str(binary)],
            cwd=binary.parent,
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL,
            **options,
        )
        self.ready = False
        self.start_deadline = time.monotonic() + 120

    def stop(self) -> bool:
        child = self.child
        if child is None or child.poll() is not None:
            self.child = None
            return True
        try:
            if sys.platform == "win32":
                subprocess.run(
                    ["taskkill", "/PID", str(child.pid), "/T", "/F"], capture_output=True, timeout=10, check=False
                )
            else:
                import signal

                os.killpg(child.pid, signal.SIGTERM)
            child.wait(timeout=10)
        except (subprocess.TimeoutExpired, OSError):
            return False
        self.child = None
        return True

    def idle(self) -> bool:
        if not self.lock.acquire(blocking=False):
            return False
        try:
            now = time.monotonic()
            if not self.closing and self.child is not None and not self.ready:
                if now < self.start_deadline:
                    return False
                self.closing = True
            if now < self.next_stop or (not self.closing and now - self.last_used < self.manifest["idle_sec"]):
                return False
            self.closing = True
            stopped = self.stop()
            if not stopped:
                self.next_stop = time.monotonic() + 10
            return stopped or (sys.platform == "win32" and JOB_HANDLE is not None)
        finally:
            self.lock.release()

    def forward(self, payload: dict[str, Any]) -> tuple[int, bytes]:
        deadline = time.monotonic() + payload["maxTimeout"] / 1000
        # A dead/sinkholed mirror is not a failed native module; preserve any warm owned service.
        public_target(payload["url"])
        if time.monotonic() >= deadline:
            raise TimeoutError("public destination resolution exceeded request budget")
        try:
            self.start(deadline)
            left = deadline - time.monotonic()
            if left < 1.25:
                raise StartupPending("request budget exhausted after module became ready")
            browser_left = left - min(1, left - 1)
            body = json.dumps({**payload, "maxTimeout": int(browser_left * 1000)}).encode()
            with closing(http.client.HTTPConnection("127.0.0.1", self.port, timeout=left)) as connection:
                connection.request("POST", "/v1", body=body, headers={"Content-Type": "application/json"})
                network_socket = connection.sock
                response = connection.getresponse()
                chunks, size = [], 0
                while True:
                    left = deadline - time.monotonic()
                    if left <= 0:
                        raise TimeoutError("local service response deadline exhausted")
                    if network_socket is not None:
                        network_socket.settimeout(left)
                    chunk = response.read1(min(16384, MAX_BODY + 1 - size))
                    if not chunk:
                        break
                    chunks.append(chunk)
                    size += len(chunk)
                    if size > MAX_BODY:
                        raise ValueError("local service response exceeds limit")
                return response.status, b"".join(chunks)
        except StartupPending:
            raise  # keep the owned warmup alive, bounded by start_deadline even without another request
        except Exception:
            self.closing = True
            self.stop()
            raise


class Broker(ThreadingHTTPServer):
    daemon_threads = False
    allow_reuse_address = False

    def __init__(self, folder: Path, manifest: dict[str, Any]):
        self.request_slots = threading.BoundedSemaphore(4)
        self.native = NativeService(folder, manifest)
        self.api_port = urlsplit(manifest["url"]).port
        super().__init__(("127.0.0.1", self.api_port), Handler)
        self.timeout = 0.25

    def process_request(self, request: socket.socket | tuple[bytes, socket.socket], client_address: Any) -> None:
        if not self.request_slots.acquire(blocking=False):
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except Exception:
            self.request_slots.release()
            raise

    def process_request_thread(self, request: socket.socket | tuple[bytes, socket.socket], client_address: Any) -> None:
        try:
            super().process_request_thread(request, client_address)
        finally:
            self.request_slots.release()

    def server_bind(self) -> None:
        if sys.platform == "win32":
            self.socket.setsockopt(socket.SOL_SOCKET, socket.SO_EXCLUSIVEADDRUSE, 1)
        super().server_bind()


class Handler(BaseHTTPRequestHandler):
    server: Broker

    def setup(self) -> None:
        super().setup()
        self.connection.settimeout(5)

    def log_message(self, *_args: Any) -> None:
        pass  # requests, headers, cookies and upstream errors never enter a broker log

    def reply(self, status: int, body: bytes) -> None:
        self.send_response(status)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.send_header("Connection", "close")
        if status == 503:
            self.send_header("Retry-After", "5")
            if b'"local module warming"' in body:
                self.send_header("X-TOW-Module-State", "warming")
        self.end_headers()
        self.wfile.write(body)

    def local(self) -> bool:
        if (
            self.client_address[0] != "127.0.0.1"
            or self.headers.get("Host")
            not in {f"127.0.0.1:{self.server.api_port}", f"localhost:{self.server.api_port}"}
            or self.headers.get("Origin")
        ):
            self.reply(403, b'{"status":"error","message":"local API only"}')
            return False
        return True

    def do_GET(self) -> None:
        if self.local():
            self.reply(200 if self.path == "/" else 404, b'{"msg":"Local module ready on demand","version":"1"}')

    def do_POST(self) -> None:
        if not self.local():
            return
        if self.path != "/v1" or self.headers.get("Content-Type", "").split(";", 1)[0] != "application/json":
            self.reply(415, b'{"status":"error","message":"JSON API only"}')
            return
        try:
            length = int(self.headers.get("Content-Length", "0"))
            if self.headers.get("Transfer-Encoding") or not 0 < length <= 65536:
                raise ValueError
            body = self.rfile.read(length)
            if len(body) != length:
                raise ValueError
            payload = validate_request(json.loads(body))
        except (ValueError, OSError, RecursionError):
            self.reply(400, b'{"status":"error","message":"invalid anonymous request"}')
            return
        native = self.server.native
        if not native.lock.acquire(blocking=False):
            self.reply(503, b'{"status":"error","message":"local module busy"}')
            return
        try:
            if native.closing:
                self.reply(503, b'{"status":"error","message":"local module stopping"}')
                return
            native.last_used = time.monotonic()
            status, response = native.forward(payload)
            self.reply(status, response)
        except StartupPending:
            self.reply(503, b'{"status":"error","message":"local module warming"}')
        except Exception:  # noqa: BLE001 - safe refusal at the private API boundary, never leak upstream data
            self.reply(500, b'{"status":"error","message":"local module startup or request failed"}')
        finally:
            if not native.closing:
                native.last_used = time.monotonic()
            native.lock.release()


def main() -> int:
    bind_owned_children()
    folder = Path(__file__).resolve().parent
    broker = Broker(folder, read_manifest(folder))
    try:
        while not broker.native.idle():
            broker.handle_request()
    finally:
        broker.server_close()
        stopped = broker.native.stop()
    return 0 if stopped else 1


if __name__ == "__main__":
    raise SystemExit(main())
