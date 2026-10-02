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


def port_open(port: int) -> bool:
    """Something listens on 127.0.0.1:<port>."""
    try:
        with socket.create_connection(("127.0.0.1", int(port)), timeout=1.5):
            return True
    except OSError:
        return False
