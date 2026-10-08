"""Start TOW with the computer: the only thing TOW writes outside its folder, and only on request.

One backend per OS, each behind the same four calls (``status``, ``enable``, ``disable``,
``start``), and each change read back before it counts:

- Windows: one Task Scheduler task "TOW" that runs ``<app>\\.venv\\Scripts\\pythonw.exe -m tow run``
  (no console window) at sign-in, or - "start without signing in" - at startup as the owner's
  account without a stored password (S4U);
- Linux: a systemd user unit ``~/.config/systemd/user/tow.service`` (Restart=on-failure); on a
  machine without systemd an XDG autostart entry ``~/.config/autostart/tow.desktop``;
- macOS: a LaunchAgent ``~/Library/LaunchAgents/io.tow.plist`` (KeepAlive on failure).

A registration that belongs to another TOW folder is never replaced or removed - unless the
program it runs no longer exists (that install was moved or deleted): then it is taken over.

Every OS command goes through a ``Runner`` so tests never touch the machine; files outside the
install are written under ``Install.home`` (an isolated folder in tests).
"""

from __future__ import annotations

import os
import subprocess
from collections.abc import Callable, Mapping, Sequence
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Protocol

TASK_NAME = "TOW"
UNIT_NAME = "tow.service"
AGENT_LABEL = "io.tow"
_NO_WINDOW = getattr(subprocess, "CREATE_NO_WINDOW", 0)


@dataclass(frozen=True)
class CommandResult:
    returncode: int
    stdout: str = ""
    stderr: str = ""

    @property
    def ok(self) -> bool:
        return self.returncode == 0

    @property
    def text(self) -> str:
        return (self.stdout or self.stderr or "").strip()


Runner = Callable[[Sequence[str]], CommandResult]


def default_runner(argv: Sequence[str]) -> CommandResult:
    """Run an OS command (schtasks, PowerShell, systemctl, launchctl) and read its output.

    Read as bytes and decoded by ``platform.decode_output``: systemctl and launchctl write UTF-8,
    schtasks and Windows PowerShell the console's OEM code page - which ``text=True`` read as
    the ANSI one, so a Russian error ("Отказано в доступе") came back garbled.
    """
    from tow.platform import decode_output

    try:
        done = subprocess.run(list(argv), capture_output=True, timeout=60, check=False, creationflags=_NO_WINDOW)
    except (OSError, subprocess.SubprocessError) as exc:
        return CommandResult(127, "", str(exc))
    return CommandResult(done.returncode, decode_output(done.stdout or b""), decode_output(done.stderr or b""))


@dataclass(frozen=True)
class Install:
    """This install as autostart sees it: where it is and whose computer it runs on."""

    root: Path  # <TOW>: data/, config.yaml, app/
    app: Path  # <TOW>/app: this code and its .venv
    home: Path  # the owner's home folder (systemd unit, LaunchAgent)
    user: str = ""
    uid: int = 0

    @classmethod
    def current(cls) -> Install:
        from tow.paths import repo_root, user_home
        from tow.platform import user_id, user_name
        from tow.supervisor.layout import install_root

        return cls(root=install_root(), app=repo_root(), home=user_home(), user=user_name(), uid=user_id())

    @property
    def is_runtime(self) -> bool:
        """A runtime install (code in <TOW>/app); a development checkout never registers autostart:
        the task, unit and agent names are one per user and would point the owner's TOW here."""
        return self.app.resolve().parent == self.root.resolve()

    def venv_bin(self, name: str) -> Path:
        windows = name.endswith(".exe")
        return self.app / ".venv" / ("Scripts" if windows else "bin") / name


class Backend(Protocol):
    name: str
    supports_without_login: bool

    def status(self) -> dict[str, Any]: ...

    def enable(self, *, without_login: bool = False) -> dict[str, Any]: ...

    def disable(self) -> dict[str, Any]: ...

    def start(self) -> bool: ...


def platform_name() -> str:
    """``windows``, ``linux`` or ``macos``: the backend this machine uses (``tow.platform``)."""
    from tow import platform

    return platform.this_os()


def backend(name: str | None = None, *, runner: Runner | None = None, install: Install | None = None) -> Backend:
    """The autostart backend of this OS (or the one named: tests use every backend anywhere)."""
    runner = runner or default_runner
    install = install or Install.current()
    chosen = name or platform_name()
    if chosen == "windows":
        from tow.autostart.windows import WindowsTask

        return WindowsTask(install, runner)
    if chosen == "macos":
        from tow.autostart.launchd import LaunchAgent

        return LaunchAgent(install, runner)
    from tow.autostart.systemd import SystemdUser

    return SystemdUser(install, runner)


def _cgroup() -> str:
    try:
        return Path("/proc/self/cgroup").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def service_manager(environ: Mapping[str, str] | None = None, cgroup: Callable[[], str] | None = None) -> str | None:
    """``systemd`` or ``launchd`` when this process runs in TOW's own autostart service, else None.

    Stopping that service ends every process it started, however detached: a web update
    started from it would be ended together with the TOW it stops. The unit and the agent say
    so (``TOW_AUTOSTART``); a unit an older TOW wrote does not, so systemd's
    ``INVOCATION_ID`` with this process in the ``tow.service`` control group (or that group
    unknown) counts too, and launchd's ``XPC_SERVICE_NAME`` of TOW's agent.
    """
    env = os.environ if environ is None else environ
    named = env.get("TOW_AUTOSTART", "")
    if named in ("systemd", "launchd"):
        return named
    if env.get("XPC_SERVICE_NAME") == AGENT_LABEL:
        return "launchd"
    if env.get("INVOCATION_ID"):
        groups = (cgroup or _cgroup)()  # looked up at call time: tests replace the module's reader
        # A terminal of a desktop also runs in a service of its own (gnome-terminal-server):
        # only TOW's unit counts.
        if not groups.strip() or any(line.rstrip().endswith(f"/{UNIT_NAME}") for line in groups.splitlines()):
            return "systemd"
    return None


def refusal(key: str, **params: Any) -> dict[str, Any]:
    from tow.i18n import t

    return {"ok": False, "refused": True, "error": t(key, **params)}


def write_text(path: Path, text: str) -> None:
    """A file outside the install (unit, desktop entry, LaunchAgent), replaced whole."""
    path.parent.mkdir(parents=True, exist_ok=True)
    temporary = path.with_name(f".{path.name}.tmp")
    temporary.write_text(text, encoding="utf-8", newline="\n")
    os.replace(temporary, path)


def read_text(path: Path) -> str | None:
    try:
        return path.read_text(encoding="utf-8")
    except OSError, UnicodeError:
        return None


__all__ = [
    "AGENT_LABEL",
    "TASK_NAME",
    "UNIT_NAME",
    "Backend",
    "CommandResult",
    "Install",
    "Runner",
    "backend",
    "default_runner",
    "platform_name",
    "service_manager",
]
