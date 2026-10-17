"""The POSIX backend's native probes on a real Linux or macOS runner, each fallback forced (TC-13).

Skipped unless TOW_REAL_PROBES=1, which only .github/workflows/real.yml sets on its Ubuntu and
macOS jobs. tests/test_platform.py drives the same branches with recorded tool output; here the
real tools and the real /proc answer, and a spy on the branch's own function proves that the
branch that should run did run:

- Linux: the listener of a port without ``ss`` and ``lsof`` (``/proc/net/tcp*`` and the
  ``/proc/<pid>/fd`` links), a command line when ``ps`` answers nothing (``/proc/<pid>/cmdline``),
  and the boot time from ``/proc/uptime`` (against ``/proc/stat`` btime, read here);
- macOS: the listener through ``lsof``, the command line through ``ps -ww`` and the boot time
  through ``sysctl kern.boottime`` (against ``who -b`` is not exact: within a day of uptime).
"""

from __future__ import annotations

import os
import socket
import sys
import time
from collections.abc import Callable, Iterator
from typing import Any

import pytest

from tow.platform import posix

pytestmark = [pytest.mark.real_system("TOW_REAL_PROBES"), pytest.mark.timeout(120)]

LINUX = sys.platform.startswith("linux")
MACOS = sys.platform == "darwin"


@pytest.fixture
def listener() -> Iterator[int]:
    """A port this test process listens on."""
    with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as server:
        server.bind(("127.0.0.1", 0))
        server.listen(1)
        yield server.getsockname()[1]


def _backend() -> posix.PosixBackend:
    return posix.PosixBackend("linux" if LINUX else "macos")


def _spy(monkeypatch: pytest.MonkeyPatch, name: str) -> list[Any]:
    calls: list[Any] = []
    original: Callable[..., Any] = getattr(posix, name)

    def spy(*args: Any, **kwargs: Any) -> Any:
        result = original(*args, **kwargs)
        calls.append(result)
        return result

    monkeypatch.setattr(posix, name, spy)
    return calls


def _without(monkeypatch: pytest.MonkeyPatch, *tools: str) -> list[str]:
    """``_which`` finds none of ``tools`` (as on a system without them); the names asked."""
    asked: list[str] = []
    original = posix._which

    def which(name: str) -> str | None:
        asked.append(name)
        return None if name in tools else original(name)

    monkeypatch.setattr(posix, "_which", which)
    return asked


@pytest.mark.skipif(not LINUX, reason="the /proc fallback is Linux's")
def test_linux_port_owner_through_proc_without_ss_or_lsof(monkeypatch, listener):
    ss = _spy(monkeypatch, "parse_ss_pid")
    inodes = _spy(monkeypatch, "proc_listeners")
    owner = _spy(monkeypatch, "proc_listener_pid")
    lsof = _spy(monkeypatch, "parse_lsof_pid")
    asked = _without(monkeypatch, "ss", "lsof")
    found = _backend().port_owner(listener)
    assert found is not None
    assert found["pid"] == os.getpid()
    assert "ss" in asked
    assert ss == []  # neither tool ran
    assert lsof == []
    assert inodes, "/proc/net/tcp* was not read"
    assert inodes[0], "no listening socket inode read from /proc/net/tcp*"
    assert owner == [os.getpid()]


@pytest.mark.skipif(not LINUX, reason="the /proc fallback is Linux's")
def test_linux_command_line_through_proc_when_ps_answers_nothing(monkeypatch):
    original = posix._run

    def run(args: list[str], timeout: float = 20) -> str:
        return "" if args and args[0] == "ps" else original(args, timeout=timeout)

    monkeypatch.setattr(posix, "_run", run)
    fallback = _spy(monkeypatch, "proc_parent_and_command")
    command = _backend().process_command(os.getpid())
    with open(f"/proc/{os.getpid()}/cmdline", "rb") as handle:
        expected = handle.read().rstrip(b"\0").replace(b"\0", b" ").decode("utf-8", "replace")
    assert fallback, "the /proc fallback did not run"
    assert fallback[0] is not None
    assert command == expected


@pytest.mark.skipif(not LINUX, reason="/proc/uptime is Linux's")
def test_linux_boot_time_from_proc_uptime(monkeypatch):
    calls = _spy(monkeypatch, "linux_boot_time")
    now = time.time()
    boot = _backend().boot_time(now)
    with open("/proc/stat", encoding="ascii") as handle:
        btime = next(int(line.split()[1]) for line in handle if line.startswith("btime "))
    assert calls == [boot]
    assert boot is not None
    assert abs(boot - btime) < 5  # /proc/uptime and btime are two clocks of one boot


@pytest.mark.skipif(not MACOS, reason="lsof, ps -ww and sysctl kern.boottime are macOS's probes")
def test_macos_port_owner_through_lsof(monkeypatch, listener):
    lsof = _spy(monkeypatch, "parse_lsof_pid")
    found = _backend().port_owner(listener)
    assert found is not None
    assert found["pid"] == os.getpid()
    assert lsof == [os.getpid()]


@pytest.mark.skipif(not MACOS, reason="lsof, ps -ww and sysctl kern.boottime are macOS's probes")
def test_macos_command_line_through_ps(monkeypatch):
    parsed = _spy(monkeypatch, "parse_ps")
    command = _backend().process_command(os.getpid())
    assert parsed, "ps -ww was not read"
    assert parsed[0] is not None
    assert command is not None
    assert "pytest" in command


@pytest.mark.skipif(not MACOS, reason="lsof, ps -ww and sysctl kern.boottime are macOS's probes")
def test_macos_boot_time_through_sysctl(monkeypatch):
    calls = _spy(monkeypatch, "macos_boot_time")
    now = time.time()
    boot = _backend().boot_time(now)
    assert calls == [boot]
    assert boot is not None
    assert now - 30 * 86400 < boot < now  # a runner booted within the last month, not in the future


@pytest.mark.skipif(not MACOS, reason="macOS has no /proc: its probes must not look for one")
def test_macos_never_reads_proc(monkeypatch, listener):
    reads = _spy(monkeypatch, "proc_listeners")
    commands = _spy(monkeypatch, "proc_parent_and_command")
    _backend().port_owner(listener)
    _backend().process_command(os.getpid())
    assert reads == []
    assert commands == []
