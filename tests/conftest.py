import os
import shutil
import sys
import tempfile
import threading
from pathlib import Path

import pytest

pytest_plugins = ["pytester"]

_TEMPLATE_CONFIG = Path(__file__).parents[1] / "config.example.yaml"
_session_home: Path | None = None


def _temp_outside_the_user_profile() -> None:
    """Windows' default temp folder is inside AppData, a folder TOW refuses for its own folders:
    tests that save a folder in tmp_path would be refused for the wrong reason. Then the run uses
    `.tmp-tests` in the checkout (gitignored) for tmp_path and everything else temporary."""
    import re

    if not re.search(r"[\\/]Users[\\/][^\\/]+[\\/]AppData[\\/]", tempfile.gettempdir() + os.sep, re.IGNORECASE):
        return
    folder = Path(__file__).resolve().parents[1] / ".tmp-tests"
    folder.mkdir(exist_ok=True)
    for name in ("TMP", "TEMP", "TMPDIR", "PYTEST_DEBUG_TEMPROOT"):
        os.environ[name] = str(folder)
    tempfile.tempdir = str(folder)


_temp_outside_the_user_profile()
# The system temp folder as the run started: tests may write there (tmp_path lives in it). TOW
# itself moves `tempfile` into its data folder at start (tow.paths.use_private_temp).
_SYSTEM_TEMP = tempfile.gettempdir()
_TEMP_ENV = ("TMP", "TEMP", "TMPDIR")

# Credentials TOW reads from the environment. The developer's machine may set them (the
# runtime install needs TOW_MASTER_KEY_FILE); a test must never see the real ones.
_CREDENTIAL_ENV = ("TOW_MASTER_KEY", "TOW_MASTER_KEY_FILE", "TOW_LAN_AUTH_TOKEN", "TOW_LAN_AUTH_TOKEN_FILE")
# Folders derived from the runner's home that TOW (autostart) or a library could write to.
_HOME_ENV = ("XDG_CONFIG_HOME", "XDG_DATA_HOME", "XDG_CACHE_HOME", "XDG_STATE_HOME")


def _test_master_key() -> str:
    from cryptography.fernet import Fernet

    return Fernet.generate_key().decode("ascii")


def pytest_addoption(parser):
    group = parser.getgroup("tow order", "test order (order-dependence check)")
    group.addoption(
        "--test-order",
        choices=("file", "reverse", "random"),
        default="file",
        help="run tests in file order (default), reversed, or shuffled (modules, then tests in each)",
    )
    group.addoption("--test-seed", type=int, default=None, help="seed for --test-order random (default: new)")


def pytest_collection_modifyitems(session, config, items):
    """Reorder for the order-dependence check; every test must pass in any order."""
    order = config.getoption("--test-order")
    if order == "reverse":
        items.reverse()
    elif order == "random":
        import random

        rng = random.Random(config._tow_seed)
        modules: dict[str, list] = {}
        for item in items:
            modules.setdefault(item.nodeid.split("::", 1)[0], []).append(item)
        groups = list(modules.values())
        rng.shuffle(groups)
        for group in groups:
            rng.shuffle(group)
        items[:] = [item for group in groups for item in group]


def _order_line(config) -> str:
    order = config.getoption("--test-order")
    if order == "random":
        seed = config._tow_seed
        return f"test order: random, seed {seed} (repeat: --test-order random --test-seed {seed})"
    return f"test order: {order}"


def pytest_report_header(config):
    return _order_line(config)


def pytest_terminal_summary(terminalreporter, exitstatus, config):
    """The seed also at the end: -q (the gate) hides the header, and a failure needs it."""
    if config.getoption("--test-order") != "file":
        terminalreporter.write_line(_order_line(config))


def pytest_configure(config):
    """Session-wide backstop: TOW_HOME/TOW_CONFIG point at a throwaway directory.

    Each test sets its own pair (`_isolate`); this one covers everything outside a test
    (collection, a background thread that outlives its test, code after teardown), so
    nothing can fall back to the checkout's real data/ and config.yaml, or to the real
    master key and LAN token. It is not restored at the end on purpose: a stray daemon
    thread must not see the real paths or keys.
    """
    global _session_home
    seed = config.getoption("--test-seed", default=None)
    config._tow_seed = seed if seed is not None else int.from_bytes(os.urandom(3))
    _session_home = Path(tempfile.mkdtemp(prefix="tow-test-session-"))
    session_config = _session_home / "config.yaml"
    session_config.write_text(_TEMPLATE_CONFIG.read_text(encoding="utf-8"), encoding="utf-8")
    os.environ["TOW_ROOT"] = str(_session_home)
    os.environ["TOW_HOME"] = str(_session_home)
    os.environ["TOW_CONFIG"] = str(session_config)
    for name in _CREDENTIAL_ENV:
        os.environ.pop(name, None)
    os.environ["TOW_MASTER_KEY"] = _test_master_key()


def pytest_unconfigure(config):
    if _session_home is not None:
        shutil.rmtree(_session_home, ignore_errors=True)


@pytest.fixture(autouse=True)
def _isolate(tmp_path, monkeypatch):
    # The install root is the test's folder: keys/, backup/ and runtime/ land there, never in
    # the checkout. Data stays directly in tmp_path (TOW_HOME), as the suite was written.
    monkeypatch.setenv("TOW_ROOT", str(tmp_path))
    monkeypatch.setenv("TOW_HOME", str(tmp_path))
    # The runner's home is never reached through ~ or the XDG folders (autostart writes
    # ~/.config/systemd/user, ~/Library/LaunchAgents): HOME is a folder of the test (not created;
    # Windows reads USERPROFILE for ~ and keeps it).
    monkeypatch.setenv("HOME", str(tmp_path / "home"))
    for name in _HOME_ENV:
        monkeypatch.delenv(name, raising=False)
    # A CLI command or the web app moves `tempfile` and TMP/TEMP/TMPDIR into the data folder
    # (tow.paths.use_private_temp): put them back after every test.
    monkeypatch.setattr(tempfile, "tempdir", tempfile.tempdir)
    for name in _TEMP_ENV:
        # setenv records the value to restore (also "not set"); delenv of an absent one would not.
        monkeypatch.setenv(name, os.environ.get(name, ""))
        if not os.environ[name]:
            monkeypatch.delenv(name)
    # A fresh master key per test (never the developer's); no LAN token from the machine.
    # Tests that need "no key" or a key file delete/set these themselves.
    for name in _CREDENTIAL_ENV:
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("TOW_MASTER_KEY", _test_master_key())
    # Tests start from the versioned template, never from the live, UI-edited config.yaml.
    isolated_config = tmp_path / "config.yaml"
    # The suite was written against the Russian texts: pin Russian (the product default is
    # English, chosen by the browser; tests/test_i18n.py covers that).
    # The first-start page (no password yet) is passed: tests/test_password.py covers it.
    isolated_config.write_text(
        _TEMPLATE_CONFIG.read_text(encoding="utf-8") + "\nlanguage: ru\nsetup_done: true\n", encoding="utf-8"
    )
    monkeypatch.setenv("TOW_CONFIG", str(isolated_config))


@pytest.fixture(autouse=True)
def _umask_restored():
    """A CLI command or the web app sets umask 077 on Linux and macOS
    (tow.platform.use_private_files): the next test starts with the umask the run had."""
    previous = os.umask(0o022)
    os.umask(previous)
    yield
    os.umask(previous)


@pytest.fixture(autouse=True)
def _no_background_check_outlives_its_test():
    """The web "check all" job runs in a 'tow-check-all' thread; it ends with its test.

    Otherwise it could keep running into the next test (or after this test's TOW_HOME
    is gone) and its job state would leak into the next /check request.
    """
    yield
    module = sys.modules.get("tow.web.routes_check")
    stray = [thread for thread in threading.enumerate() if thread.name == "tow-check-all"]
    for thread in stray:
        thread.join(timeout=10)
    if module is not None:
        with module._CHECK_JOB_LOCK:
            module._check_job.clear()
    alive = [thread for thread in stray if thread.is_alive()]
    if alive:
        pytest.fail(f"background 'check all' thread still running 10 s after the test: {alive}")


@pytest.fixture(autouse=True)
def _pinned_time_zone(monkeypatch):
    """Local times are Israel time in every test, whatever zone this machine is in.

    Expected strings like "IL+03" and "+03:00" are written for the owner's zone; the
    pin keeps them true on a UTC CI runner or a laptop abroad. test_clock checks the
    seam with other zones.
    """
    from zoneinfo import ZoneInfo

    import tow.clock

    zone = ZoneInfo("Asia/Jerusalem")
    monkeypatch.setattr(tow.clock, "local_zone", lambda: zone)


@pytest.fixture(autouse=True)
def _loopback_test_client(monkeypatch):
    """TestClient talks to the app the way a local browser does: 127.0.0.1 to 127.0.0.1.

    Production code has no test-only host names; tests that simulate a LAN client
    still pass their own ``client=``/``base_url=``.
    """
    from fastapi.testclient import TestClient

    original = TestClient.__init__

    def init(self, app, *args, base_url="http://127.0.0.1", client=("127.0.0.1", 50000), **kwargs):
        original(self, app, *args, base_url=base_url, client=client, **kwargs)

    monkeypatch.setattr(TestClient, "__init__", init)


@pytest.fixture(autouse=True)
def _no_dns_lookups(monkeypatch):
    """Host names in tests are not resolved (no network); tests that need it patch their own."""
    monkeypatch.setattr("tow.web.site_form._resolve_addresses", lambda _name: [])


@pytest.fixture(autouse=True)
def _fresh_login_throttle(monkeypatch):
    """The login throttle is process-global state; no test may inherit another's lockout."""
    from tow.ratelimit import LoginThrottle
    from tow.web import services

    monkeypatch.setattr(services, "login_throttle", LoginThrottle())


@pytest.fixture(autouse=True)
def _no_inherited_language():
    """Every test starts without a "current language": it comes from its config or request.

    ``tow.cli.main`` and ``i18n.use`` set a ContextVar in the calling thread; left set, it
    made tests pass or fail depending on which test ran before (a test asserting Russian must
    get it from ``language: ru`` in its config, not from a previous test's CLI run).
    """
    import tow.i18n

    current = getattr(tow.i18n, "_CURRENT", None)
    if current is None:
        yield
        return
    current.set(None)
    yield
    current.set(None)


@pytest.fixture(autouse=True)
def _no_revoked_sessions():
    """Signed-out sessions are a process-wide set; a test starts with none revoked."""
    import tow.auth

    tow.auth.clear_sessions()
    yield
    tow.auth.clear_sessions()


@pytest.fixture(autouse=True)
def _fast_export_kdf(monkeypatch):
    """Bundles in tests use the lowest PBKDF2 cost the reader accepts (100k, ~6x faster).

    The production cost is guarded by test_export_import's source check.
    """
    import tow.bundle

    monkeypatch.setattr(tow.bundle, "KDF_ITERATIONS", 100_000)


@pytest.fixture(autouse=True)
def _fresh_task_cache(monkeypatch):
    """service_status caches the autostart read-back for a minute; tests never share it."""
    import tow.lifecycle

    monkeypatch.setattr(tow.lifecycle, "_autostart_cache", None)


# Live local services a test must never reach, even on loopback: qBittorrent WebUI,
# TOW itself, Transmission, Deluge daemon and Deluge Web.
_SERVICE_PORTS = frozenset({8080, 8112, 8787, 8788, 9091, 58846})
_LOOPBACK_HOSTS = frozenset({"127.0.0.1", "::1", "localhost"})


def _refused_endpoint(host, port) -> bool:
    """A connection the guard refuses: any non-loopback host, or a live service port."""
    try:
        port = int(port)
    except TypeError, ValueError:
        port = None
    return host not in _LOOPBACK_HOSTS or port in _SERVICE_PORTS


def _is_multiprocessing_child(application_name, command_line) -> bool:
    """multiprocessing's spawn bootstrap of this interpreter (the only allowed child)."""
    import multiprocessing.spawn
    import os
    import sys

    allowed = {
        os.path.normcase(os.path.abspath(exe))
        for exe in (sys.executable, getattr(sys, "_base_executable", None), multiprocessing.spawn.get_executable())
        if exe
    }
    exe = os.path.normcase(os.path.abspath(str(application_name))) if application_name else None
    command = str(command_line or "")
    return (
        exe in allowed
        and "from multiprocessing.spawn import spawn_main" in command
        and "--multiprocessing-fork" in command
    )


def _writable_roots() -> tuple[str, ...]:
    """Where a test may write: the temp folder (tmp_path, pytester) and the session home."""
    roots = [_SYSTEM_TEMP]
    if _session_home is not None:
        roots.append(str(_session_home))
    return tuple(os.path.normcase(os.path.realpath(root)) + os.sep for root in roots)


def _guard_file_writes(monkeypatch, deny) -> None:
    """Writes, creations, renames and deletes outside the temp folder are refused.

    TOW writes only inside its own install; in a test that is tmp_path. A refusal test that
    fails must not leave files in the checkout or in system folders (C:\\Windows, ~).
    """
    import builtins
    import io

    roots = _writable_roots()

    def outside(path) -> bool:
        if isinstance(path, int):  # an already open descriptor
            return False
        full = os.path.normcase(os.path.realpath(os.fspath(path))) + os.sep
        # Python's own bytecode cache (first import of a module) is not a TOW write.
        return not full.startswith(roots) and f"{os.sep}__pycache__{os.sep}" not in full

    def check(path, what: str) -> None:
        if outside(path):
            deny(f"{what} {os.fspath(path)}")()

    real_open = io.open

    def guarded_open(file, mode="r", *args, **kwargs):
        if any(flag in mode for flag in "wax+"):
            check(file, "write")
        return real_open(file, mode, *args, **kwargs)

    monkeypatch.setattr(builtins, "open", guarded_open)
    monkeypatch.setattr(io, "open", guarded_open)

    real_os_open = os.open
    writing = os.O_WRONLY | os.O_RDWR | os.O_CREAT | os.O_APPEND | os.O_TRUNC

    def guarded_os_open(path, flags, *args, **kwargs):
        if flags & writing and kwargs.get("dir_fd") is None:
            check(path, "write")
        return real_os_open(path, flags, *args, **kwargs)

    monkeypatch.setattr(os, "open", guarded_os_open)

    def one_path(name: str):
        real = getattr(os, name)

        def guarded(path, *args, **kwargs):
            if kwargs.get("dir_fd") is None:
                check(path, name)
            return real(path, *args, **kwargs)

        monkeypatch.setattr(os, name, guarded)

    for name in ("mkdir", "rmdir", "remove", "unlink"):
        one_path(name)

    def two_paths(name: str):
        real = getattr(os, name)

        def guarded(src, dst, *args, **kwargs):
            check(src, name)
            check(dst, name)
            return real(src, dst, *args, **kwargs)

        monkeypatch.setattr(os, name, guarded)

    for name in ("rename", "replace"):
        two_paths(name)


def _guard_signals(monkeypatch, deny) -> None:
    """Signals: a test may stop only the multiprocessing children it started itself (on Windows
    os.kill even terminates the process). tow.platform's process handling is tested with fakes.
    The one harmless signal is let through: signal 0 to this very process on Linux and macOS
    (an existence probe, how process_alive asks; on Windows os.kill(pid, 0) would terminate)."""
    own_pid = os.getpid()
    for name in ("kill", "killpg"):
        real_signal = getattr(os, name, None)
        if real_signal is None:
            continue

        def guarded_signal(pid, sig, *rest, _real=real_signal, _name=name):
            import multiprocessing

            if _name == "kill" and sig == 0 and pid == own_pid and sys.platform != "win32":
                return _real(pid, sig, *rest)
            if _name == "kill" and pid in {child.pid for child in multiprocessing.active_children()}:
                return _real(pid, sig, *rest)
            return deny(f"os.{_name} {pid}")()

        monkeypatch.setattr(os, name, guarded_signal)


@pytest.fixture(autouse=True)
def _no_real_system_effects(request, monkeypatch):  # noqa: C901 - one guard, every check in one place
    """Refuse every real side effect on this machine; tests patch what they need.

    Violations are recorded and fail the test at teardown, so code under test that
    swallows the error with `except Exception` cannot hide them. A test that really
    needs the system (none today) is marked `@pytest.mark.allow_system`.
    """
    if request.node.get_closest_marker("allow_system"):
        yield
        return

    import asyncio
    import os
    import socket
    import subprocess
    import webbrowser

    import httpx
    import requests

    from tow import browser_auth

    violations: list[str] = []
    # `allow_git`: the updater's tests run real git on a throwaway clone in the temp folder;
    # every other program stays refused.
    allow_git = request.node.get_closest_marker("allow_git") is not None

    def is_git(program) -> bool:
        return allow_git and os.path.basename(str(program or "")).lower().removesuffix(".exe") == "git"

    def deny(what: str):
        def refuse(*_args, **_kwargs):
            violations.append(what)
            raise AssertionError(f"[test guard] real system effect refused: {what}")

        return refuse

    real_popen = subprocess.Popen

    class GuardedPopen(real_popen):  # type: ignore[misc, valid-type]
        def __init__(self, args, *rest, **kwargs):
            argv = [str(a) for a in args] if isinstance(args, (list, tuple)) else [str(args)]
            joined = " ".join(argv).lower()
            if "schtasks" in joined and "/query" in joined and not kwargs.get("shell"):
                # Read-only task queries behave as "task absent" instead of reading this machine.
                raise FileNotFoundError("schtasks is stubbed in tests")
            if os.path.basename(argv[0]).lower() in {"icacls", "icacls.exe"} and not kwargs.get("shell"):
                # Windows folder permissions stay as the test made them (tow.platform.private_folders
                # reads back "still open" and warns); tests/test_private_folders.py fakes icacls.
                raise FileNotFoundError("icacls is stubbed in tests")
            if is_git(argv[0]) and not kwargs.get("shell"):
                super().__init__(args, *rest, **kwargs)
                return
            deny(f"process {argv[:2]}")()

    monkeypatch.setattr(subprocess, "Popen", GuardedPopen)
    _guard_file_writes(monkeypatch, deny)
    for module, name in (
        (os, "system"),
        (os, "startfile"),
        (webbrowser, "open"),
        (browser_auth, "find_browser_executable"),
    ):
        if hasattr(module, name):
            monkeypatch.setattr(module, name, deny(f"{module.__name__}.{name}"))
    _guard_signals(monkeypatch, deny)

    monkeypatch.setattr(asyncio, "create_subprocess_exec", deny("asyncio subprocess"))
    monkeypatch.setattr(asyncio, "create_subprocess_shell", deny("asyncio subprocess"))
    # Real HTTP of any kind (trackers, Telegram, qbittorrent-api); TestClient and
    # httpx.MockTransport do not go through these transports.
    monkeypatch.setattr(httpx.HTTPTransport, "handle_request", deny("httpx request"))
    monkeypatch.setattr(httpx.AsyncHTTPTransport, "handle_async_request", deny("httpx async request"))
    monkeypatch.setattr(requests.adapters.HTTPAdapter, "send", deny("requests/qbittorrent-api request"))

    # Lower-level process starts: os.spawn*/os.exec*, and on Windows the CreateProcess
    # call under subprocess/multiprocessing. Only multiprocessing's own spawn bootstrap
    # of this interpreter may start a child (test_log, test_persistence, test_export_import).
    for name in dir(os):
        if name.startswith(("spawn", "exec", "posix_spawn")) and callable(getattr(os, name)):
            monkeypatch.setattr(os, name, deny(f"os.{name}"))
    try:
        import _winapi
    except ImportError:
        _winapi = None  # type: ignore[assignment]
    if _winapi is not None:
        real_create_process = _winapi.CreateProcess

        def guarded_create_process(application_name, command_line, *rest):
            if _is_multiprocessing_child(application_name, command_line):
                return real_create_process(application_name, command_line, *rest)
            first = str(command_line or "").split('"')[1] if str(command_line or "").startswith('"') else None
            if application_name is None and is_git(first or str(command_line or "").split(" ", 1)[0]):
                return real_create_process(application_name, command_line, *rest)
            return deny(f"_winapi.CreateProcess {str(command_line or application_name)[:80]}")()

        monkeypatch.setattr(_winapi, "CreateProcess", guarded_create_process)

    def check_address(address, via: str) -> None:
        if not isinstance(address, tuple) or len(address) < 2:
            return
        host, port = address[0], address[1]
        if _refused_endpoint(host, port):
            deny(f"{via} {host}:{port}")()

    real_connect = socket.socket.connect
    real_connect_ex = socket.socket.connect_ex
    real_socketpair = socket.socketpair
    pairing = threading.local()

    def guarded_socketpair(*args, **kwargs):
        # On Windows socketpair() is a loopback listener plus a connect to it (asyncio's
        # self-pipe). Its port is ephemeral and can be any service port when the dynamic
        # range starts at 1024; that connect never leaves this process.
        pairing.active = True
        try:
            return real_socketpair(*args, **kwargs)
        finally:
            pairing.active = False

    monkeypatch.setattr(socket, "socketpair", guarded_socketpair)

    def guarded_connect(self, address):
        if self.family in (socket.AF_INET, socket.AF_INET6) and not getattr(pairing, "active", False):
            check_address(address, "socket")
        return real_connect(self, address)  # loopback is needed by asyncio's self-pipe

    def guarded_connect_ex(self, address):
        if self.family in (socket.AF_INET, socket.AF_INET6):
            check_address(address, "socket connect_ex")
        return real_connect_ex(self, address)

    monkeypatch.setattr(socket.socket, "connect", guarded_connect)
    monkeypatch.setattr(socket.socket, "connect_ex", guarded_connect_ex)

    # asyncio: the Proactor loop (Windows default) connects through ConnectEx, not
    # socket.connect, so the loop entry points are guarded themselves. create_connection
    # is checked before it resolves the host name.
    import asyncio.base_events
    import asyncio.proactor_events
    import asyncio.selector_events

    real_create_connection = asyncio.base_events.BaseEventLoop.create_connection

    async def guarded_create_connection(self, protocol_factory, host=None, port=None, *args, **kwargs):
        if host is not None:
            check_address((host, port), "asyncio connection")
        return await real_create_connection(self, protocol_factory, host, port, *args, **kwargs)

    monkeypatch.setattr(asyncio.base_events.BaseEventLoop, "create_connection", guarded_create_connection)
    for loop_class in (
        asyncio.selector_events.BaseSelectorEventLoop,
        asyncio.proactor_events.BaseProactorEventLoop,
    ):

        def guarded_sock_connect(self, sock, address, _real=loop_class.sock_connect):
            if sock.family in (socket.AF_INET, socket.AF_INET6):
                check_address(address, "asyncio socket")
            return _real(self, sock, address)

        monkeypatch.setattr(loop_class, "sock_connect", guarded_sock_connect)
    yield
    assert not violations, f"test guard violations (possibly swallowed by the code under test): {violations}"
