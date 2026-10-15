"""The test guard must fail a test even when the code under test swallows the error."""

import itertools
import shutil
import sys
import tempfile
import textwrap
from pathlib import Path

import pytest

# These tests start a nested pytest process; the nested run is guarded by its own conftest.
pytestmark = pytest.mark.allow_system

CONFTEST = Path(__file__).with_name("conftest.py")


@pytest.fixture
def nested(pytester, monkeypatch):
    """Runs a nested pytest session under the guard's conftest; returns its result.

    Everything a nested run writes stays in this test's own folder: the template the conftest
    reads one level above it (before: the worker's temp folder, shared by every pytester test),
    TMP/TEMP/TMPDIR (its session home), and a --basetemp that does not exist yet. pytest
    deletes and re-creates an existing one, and Windows can refuse that while an antivirus
    still holds the deleted folder."""
    tests = pytester.mkdir("tests")
    shutil.copy(CONFTEST, tests / "conftest.py")
    shutil.copy(Path(__file__).parents[1] / "config.example.yaml", pytester.path / "config.example.yaml")
    runs = itertools.count(1)

    def run(body: str):
        number = next(runs)
        (tests / "test_inner.py").write_text(textwrap.dedent(body), encoding="utf-8")
        temp = pytester.mkdir(f"temp-{number}")
        for name in ("TMP", "TEMP", "TMPDIR"):
            monkeypatch.setenv(name, str(temp))
        base = temp / "base"  # inside the temp folder: the nested guard lets tmp_path be written
        return pytester.runpytest_subprocess("-q", "-p", "no:cacheprovider", f"--basetemp={base}", "tests", timeout=120)

    return run


def test_swallowed_real_http_still_fails_the_test(nested):
    result = nested(
        """
import httpx

def test_inner():
    try:
        httpx.get("https://example.invalid/")
    except Exception:
        pass  # code under test swallowing the failure
""",
    )
    result.assert_outcomes(passed=1, errors=1)
    result.stdout.fnmatch_lines(["*test guard violations*httpx request*"])


def test_loopback_fixture_retries_service_ports_without_weakening_guard(nested):
    result = nested(
        """
import http.server
import pytest

def test_retries(monkeypatch, loopback_http_server):
    ports = iter([8788, 9091, 19000])
    closed = []
    class Server:
        def __init__(self, address, handler):
            self.server_address = (address[0], next(ports))
        def server_close(self):
            closed.append(self.server_address[1])
    monkeypatch.setattr(http.server, 'ThreadingHTTPServer', Server)
    server = loopback_http_server(object())
    assert server.server_address == ('127.0.0.1', 19000)
    assert closed == [8788, 9091]
    server.server_close()

def test_bounded(monkeypatch, loopback_http_server):
    closed = []
    class Server:
        def __init__(self, address, handler):
            self.server_address = ('127.0.0.1', 8788)
        def server_close(self):
            closed.append(True)
    monkeypatch.setattr(http.server, 'ThreadingHTTPServer', Server)
    with pytest.raises(RuntimeError, match='no guarded loopback test port'):
        loopback_http_server(object())
    assert len(closed) == 32
""",
    )
    result.assert_outcomes(passed=2)


def test_swallowed_system_command_still_fails_the_test(nested):
    result = nested(
        """
import subprocess

def test_inner():
    try:
        subprocess.run(["schtasks", "/Create", "/TN", "X"])
    except Exception:
        pass
""",
    )
    result.assert_outcomes(passed=1, errors=1)
    result.stdout.fnmatch_lines(["*test guard violations*process*schtasks*"])


_REAL_SYSTEM_TESTS = """
import subprocess, sys
import pytest

@pytest.mark.real_system("TOW_REAL_PROBE")
def test_real():
    subprocess.run([sys.executable, "-c", "pass"], check=True)

@pytest.mark.real_system("TOW_REAL_OTHER")
def test_real_of_another_variable():
    subprocess.run([sys.executable, "-c", "pass"], check=True)

@pytest.mark.real_system("NOT_TOW_REAL")
def test_real_of_a_variable_outside_the_family():
    subprocess.run([sys.executable, "-c", "pass"], check=True)

def test_unmarked():
    try:
        subprocess.run([sys.executable, "-c", "pass"])
    except Exception:
        pass
"""


def test_a_real_system_test_runs_only_with_its_variable_and_then_unguarded(nested, monkeypatch):
    """tests/integration: skipped on a development machine; with its TOW_REAL_* variable at 1 (the
    real.yml runners) it runs without the guard, and every other test stays guarded."""
    for name in ("TOW_REAL_PROBE", "TOW_REAL_OTHER"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("NOT_TOW_REAL", "1")
    monkeypatch.setenv("TOW_REAL_OTHER", "yes")  # only exactly 1 counts

    nested(_REAL_SYSTEM_TESTS).assert_outcomes(passed=1, skipped=3, errors=1)

    monkeypatch.setenv("TOW_REAL_PROBE", "1")
    result = nested(_REAL_SYSTEM_TESTS)
    result.assert_outcomes(passed=2, skipped=2, errors=1)
    result.stdout.fnmatch_lines(["*test guard violations*process*"])


def test_read_only_task_query_is_stubbed(nested):
    result = nested(
        """
import subprocess, pytest

def test_inner():
    with pytest.raises(FileNotFoundError):
        subprocess.run(["schtasks", "/Query", "/TN", "TOW-check"])
""",
    )
    result.assert_outcomes(passed=1)


def test_a_write_outside_the_temp_folder_fails_the_test(nested):
    # Outside the temp folder wherever the checkout is (the drive root), and in a folder that does
    # not exist, so nothing is written even if the guard were gone.
    target = Path(Path(tempfile.gettempdir()).anchor) / "__tow_never_created__" / "probe.txt"
    result = nested(
        f"""
import os

TARGET = {str(target)!r}

def swallow(call):
    try:
        call()
    except Exception:
        pass

def test_open_for_writing():
    swallow(lambda: open(TARGET, "w"))

def test_mkdir():
    swallow(lambda: os.mkdir(os.path.join(os.path.dirname(TARGET), "sub")))  # parent missing: never made

def test_writing_in_tmp_path_is_fine(tmp_path):
    (tmp_path / "ok.txt").write_text("ok", encoding="utf-8")
""",
    )
    result.assert_outcomes(passed=3, errors=2)
    result.stdout.fnmatch_lines(["*test guard violations*write*__tow_never_created__*"])
    result.stdout.fnmatch_lines(["*test guard violations*mkdir*__tow_never_created__*"])
    assert not target.parent.exists()


def test_socketpair_on_a_service_port_is_not_a_violation(nested):
    """Windows socketpair() (asyncio's self-pipe) connects to its own ephemeral port; with a
    dynamic range from 1024 that can be 8788 or 8080, and it must not fail the test."""
    result = nested(
        """
import asyncio, socket
import pytest

def _free_service_port():
    for port in (58846, 9091, 8112, 8788):
        with socket.socket() as probe:
            try:
                probe.bind(("127.0.0.1", port))
            except OSError:
                continue
        return port
    pytest.skip("every service port is in use on this machine")


def test_inner():
    port = _free_service_port()
    real_bind = socket.socket.bind
    socket.socket.bind = lambda self, addr: real_bind(self, (addr[0], port) if addr[1] == 0 else addr)
    try:
        a, b = socket.socketpair()
        a.close(), b.close()
    finally:
        socket.socket.bind = real_bind
    asyncio.run(asyncio.sleep(0))
""",
    )
    result.assert_outcomes(passed=1)


@pytest.mark.skipif(sys.platform == "win32", reason="os.kill(pid, 0) is CTRL_C_EVENT on Windows")
def test_only_an_existence_probe_of_this_process_is_let_through(nested):
    """Signal 0 to its own pid (process_alive of this process) is harmless; any other is refused."""
    result = nested(
        """
import os

def test_own_existence_probe():
    os.kill(os.getpid(), 0)

def test_another_process():
    try:
        os.kill(1, 0)  # init: harmless even without the guard
    except Exception:
        pass
""",
    )
    result.assert_outcomes(passed=2, errors=1)
    result.stdout.fnmatch_lines(["*test guard violations*os.kill 1*"])


def test_lower_level_bypasses_are_caught(nested):
    """connect_ex, asyncio connections, os.spawn/exec and CreateProcess all go through the guard."""
    result = nested(
        """
import asyncio, os, socket, sys
import pytest

TEST_NET = "192.0.2.1"  # documentation range: never routed, even if the guard failed


def swallow(call):
    try:
        call()
    except Exception:
        pass


def test_connect_ex_to_remote():
    with socket.socket() as sock:
        sock.settimeout(0.5)
        swallow(lambda: sock.connect_ex((TEST_NET, 80)))


def test_connect_ex_to_deluge_web_on_loopback():
    with socket.socket() as sock:
        sock.settimeout(0.5)
        swallow(lambda: sock.connect_ex(("127.0.0.1", 8112)))


def test_asyncio_open_connection_to_remote():
    async def go():
        await asyncio.wait_for(asyncio.open_connection(TEST_NET, 443), 1)

    swallow(lambda: asyncio.run(go()))


def test_asyncio_sock_connect_to_transmission_on_loopback():
    async def go():
        with socket.socket() as sock:
            sock.setblocking(False)
            await asyncio.wait_for(asyncio.get_running_loop().sock_connect(sock, ("127.0.0.1", 9091)), 1)

    swallow(lambda: asyncio.run(go()))


def test_os_spawn():
    swallow(lambda: os.spawnv(os.P_WAIT, sys.executable, [sys.executable, "-c", "pass"]))


def test_os_exec():
    swallow(lambda: os.execv(sys.executable, [sys.executable, "-c", "pass"]))


@pytest.mark.skipif(sys.platform != "win32", reason="Windows API")
def test_winapi_create_process_that_is_not_a_multiprocessing_child():
    import _winapi

    command = f'"{sys.executable}" -c pass'
    swallow(lambda: _winapi.CreateProcess(sys.executable, command, None, None, False, 0, None, None, None))
""",
    )
    windows = sys.platform == "win32"
    result.assert_outcomes(passed=7 if windows else 6, errors=7 if windows else 6, skipped=0 if windows else 1)
    for violation in (
        "socket connect_ex 192.0.2.1:80",
        "socket connect_ex 127.0.0.1:8112",
        "asyncio connection 192.0.2.1:443",
        "asyncio socket 127.0.0.1:9091",
        "os.spawnv",
        "os.execv",
    ):
        result.stdout.fnmatch_lines([f"*test guard violations*{violation}*"])
    if windows:
        result.stdout.fnmatch_lines(["*test guard violations*_winapi.CreateProcess*"])
