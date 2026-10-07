"""What only the supervisor needs from the OS: the interpreter for its children, starting a
child it owns (with its output in a file) and asking whether its port is taken.

Everything else - whether a process is alive, stopping a process tree, the port's owner, a job
object that ends the children with the supervisor - comes from ``tow.platform``.
"""

from __future__ import annotations

import socket
import subprocess
import sys
from collections.abc import Mapping, Sequence
from pathlib import Path
from typing import IO, Any


def child_python() -> str:
    """The interpreter for children: python.exe next to a pythonw.exe supervisor.

    Children get files for their output; a console interpreter with CREATE_NO_WINDOW writes
    to them reliably and still shows no window.
    """
    executable = Path(sys.executable)
    if executable.name.lower() == "pythonw.exe":
        console = executable.with_name("python.exe")
        if console.is_file():
            return str(console)
    return str(executable)


def spawn(
    argv: Sequence[str],
    *,
    cwd: Path,
    env: Mapping[str, str],
    stdout: IO[Any] | int | None = None,
    stderr: IO[Any] | int | None = None,
) -> subprocess.Popen[bytes]:
    """A child the supervisor owns: hidden, in its own process group / session (so it can be
    stopped with everything it started)."""
    from tow import platform

    return subprocess.Popen(
        list(argv),
        cwd=str(cwd),
        env=dict(env),
        stdin=subprocess.DEVNULL,
        stdout=stdout if stdout is not None else subprocess.DEVNULL,
        stderr=stderr if stderr is not None else subprocess.DEVNULL,
        **platform.current().popen_options(new_group=True, hidden=True),
    )


def port_free(port: int) -> bool:
    """Nothing holds ``port``: it can be bound on 127.0.0.1 and on every address.

    Both: Windows lets a program bind 127.0.0.1 while another one listens on 0.0.0.0 (and the
    other way round). Linux and macOS refuse a port whose closed connections still wait
    (TIME_WAIT) unless SO_REUSEADDR is set, and with it still never share a listening port;
    on Windows that option would let the probe share it, so it is not set there.
    """
    from tow import platform

    for host in ("127.0.0.1", "0.0.0.0"):  # bound for an instant, never listened on
        with socket.socket(socket.AF_INET, socket.SOCK_STREAM) as probe:
            if not platform.is_windows():
                probe.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            try:
                probe.bind((host, int(port)))
            except OSError:
                return False
    return True


def port_open(port: int) -> bool:
    """Something listens on 127.0.0.1:<port>.

    A port that can be bound is free at once: a connection to a closed loopback port is
    retried by Windows for about a second and a half, which every ``tow run`` and ``tow setup``
    waited for. A port that cannot be bound is confirmed by a connection (it may be held by a
    socket that does not listen).
    """
    if port_free(port):
        return False
    try:
        with socket.create_connection(("127.0.0.1", int(port)), timeout=1.5):
            return True
    except OSError:
        return False
