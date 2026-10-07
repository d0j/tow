"""Linux: a systemd user unit, or an XDG autostart entry where systemd does not run at all.

The unit starts ``<app>/.venv/bin/tow run`` when the owner's user manager starts (at sign-in;
with ``loginctl enable-linger`` at boot, without signing in) and again after a failure. A stop
asked through TOW ends with code 0 and stays stopped; "already running" ends with code 0 too,
a busy port with 3, which is not retried in a loop (``RestartPreventExitStatus=3``).

A stop by systemd (``systemctl --user stop``, a shutdown) signals ``tow run`` alone
(``KillMode=mixed``): it lets a running job finish for a while, stops its web server and exits;
only what is left after ``TimeoutStopSec`` is killed. Everything the unit started ends with it,
also a process started detached, so an update is started from a terminal, not from the web page
(``TOW_AUTOSTART=systemd``, ``tow.autostart.service_manager``).
"""

from __future__ import annotations

import os
import re
from pathlib import Path
from typing import Any

from tow.autostart import UNIT_NAME, Install, Runner, read_text, refusal, write_text

# systemd waits this long for `tow run` to stop before it kills what is left: more than the
# supervisor needs after SIGTERM (a running job 20 s, then stopping it and the web server).
TIMEOUT_STOP_SEC = 90


def _quoted(value: str | Path) -> str:
    """One word of ExecStart=: C-style quotes, ``%`` and ``$`` kept literal (no specifiers, no
    variables) - a folder named ``50%`` or ``$HOME`` still names itself."""
    text = str(value).replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%").replace("$", "$$")
    return f'"{text}"'


def _env_quoted(value: str) -> str:
    """One assignment of Environment=: quoted; specifiers (``%``) expand there, variables do not."""
    text = value.replace("\\", "\\\\").replace('"', '\\"').replace("%", "%%")
    return f'"{text}"'


def _path_value(value: str | Path) -> str:
    """A path setting (WorkingDirectory=): unquoted; only specifiers expand."""
    return str(value).replace("%", "%%")


def _desktop_quoted(value: str | Path) -> str:
    """One argument of a desktop entry's Exec= (quoted, then the file's own escaping)."""
    inner = re.sub(r'(["`$\\])', r"\\\1", str(value))
    return '"' + inner.replace("\\", "\\\\").replace("%", "%%") + '"'


_EXEC_START = re.compile(r'(?m)^ExecStart=\s*"((?:[^"\\]|\\.)*)"')


def registered_command(unit: str) -> str | None:
    """The program a unit's ExecStart= runs (quoted form, as TOW writes it), or None."""
    match = _EXEC_START.search(unit)
    if match is None:
        return None
    text = re.sub(r"\\(.)", r"\1", match.group(1))
    return text.replace("%%", "%").replace("$$", "$")


class SystemdUser:
    name = "linux"
    supports_without_login = True
    # systemd is the init system (sd_booted): its user manager not answering is then an error,
    # never a reason to fall back to a desktop entry.
    systemd_runtime = Path("/run/systemd/system")

    def __init__(self, install: Install, runner: Runner):
        self.install = install
        self.run = runner

    @property
    def config_home(self) -> Path:
        xdg = os.environ.get("XDG_CONFIG_HOME")
        return Path(xdg) if xdg and Path(xdg).is_absolute() else self.install.home / ".config"

    @property
    def unit_path(self) -> Path:
        return self.config_home / "systemd" / "user" / UNIT_NAME

    @property
    def desktop_path(self) -> Path:
        return self.config_home / "autostart" / "tow.desktop"

    @property
    def command(self) -> Path:
        return self.install.venv_bin("tow")

    def unit(self, *, marked: bool = True) -> str:
        """The unit TOW writes; ``marked=False`` is the one of TOW 1.24.1 and older, which did
        not tell ``tow run`` that systemd started it (still counted as on: it works)."""
        root = self.install.root
        # TOW_AUTOSTART: stopping the unit ends every process it started, so the web page does
        # not start an update from it (tow.web_update); the terminal command does it.
        marker = "Environment=TOW_AUTOSTART=systemd\n" if marked else ""
        # No After=network-online.target: the user manager has no such unit (it is a system
        # one); TOW waits for the network itself.
        return (
            "[Unit]\n"
            "Description=TOW - Torrent Watcher\n"
            "\n"
            "[Service]\n"
            "Type=simple\n"
            f"WorkingDirectory={_path_value(root)}\n"
            f"Environment={_env_quoted(f'TOW_ROOT={root}')}\n"
            f"{marker}"
            f"ExecStart={_quoted(self.command)} run\n"
            "Restart=on-failure\n"
            "RestartSec=60\n"
            "RestartPreventExitStatus=3\n"
            "KillMode=mixed\n"
            f"TimeoutStopSec={TIMEOUT_STOP_SEC}\n"
            "\n"
            "[Install]\n"
            "WantedBy=default.target\n"
        )

    def desktop(self) -> str:
        root = self.install.root
        return (
            "[Desktop Entry]\n"
            "Type=Application\n"
            "Name=TOW\n"
            "Comment=TOW - Torrent Watcher\n"
            f"Exec=env {_desktop_quoted(f'TOW_ROOT={root}')} {_desktop_quoted(self.command)} run\n"
            f"Path={root}\n"
            "Terminal=false\n"
            "X-GNOME-Autostart-enabled=true\n"
        )

    # --- reading ------------------------------------------------------------------------------

    def systemd_booted(self) -> bool:
        return self.systemd_runtime.is_dir()

    def linger(self) -> bool:
        if not self.install.user:
            return False
        result = self.run(["loginctl", "show-user", self.install.user, "--property=Linger"])
        return result.ok and result.stdout.strip().lower() == "linger=yes"

    def status(self) -> dict[str, Any]:
        unit = read_text(self.unit_path)
        desktop = read_text(self.desktop_path)
        status: dict[str, Any] = {"backend": self.name, "on": False, "ours": True, "without_login": False}
        if unit is not None:
            registered = registered_command(unit)
            status["where"] = str(self.unit_path)
            status["command"] = registered
            status["ours"] = registered == str(self.command)
            # A unit whose program is gone (the install was moved or deleted) may be taken over.
            status["stale"] = not status["ours"] and not (registered and Path(registered).exists())
            enabled = self.run(["systemctl", "--user", "is-enabled", UNIT_NAME])
            status["enabled"] = enabled.stdout.strip()
            status["on"] = unit in (self.unit(), self.unit(marked=False)) and enabled.stdout.strip() == "enabled"
            status["without_login"] = status["on"] and self.linger()
        elif desktop is not None:
            status["where"] = str(self.desktop_path)
            status["ours"] = _desktop_quoted(self.command) in desktop
            status["on"] = desktop == self.desktop()
            status["fallback"] = True
        return status

    # --- changing -----------------------------------------------------------------------------

    def enable(self, *, without_login: bool = False) -> dict[str, Any]:
        if not self.install.is_runtime:
            return refusal("autostart.not_runtime")
        if not self.command.is_file():
            return refusal("autostart.no_venv", path=str(self.command))
        before = self.status()
        if not before.get("ours") and not before.get("stale"):
            return refusal("autostart.other_install", where=before.get("where") or UNIT_NAME)
        probe = self.run(["systemctl", "--user", "show-environment"])
        if not probe.ok:
            if self.systemd_booted():
                # systemd runs this computer but its user manager did not answer (an SSH session
                # without one, a broken D-Bus): say so instead of writing a desktop entry.
                return refusal("autostart.systemd_unreachable", error=probe.text[:200] or str(probe.returncode))
            if without_login:
                return refusal("autostart.without_login_unsupported")
            write_text(self.desktop_path, self.desktop())
            after = self.status()
            return {"ok": bool(after["on"]), "changed": True, "status": after}
        write_text(self.unit_path, self.unit())
        steps = [
            self.run(["systemctl", "--user", "daemon-reload"]),
            self.run(["systemctl", "--user", "enable", "--now", UNIT_NAME]),
        ]
        after = self.status()
        result: dict[str, Any] = {"ok": bool(after["on"]), "changed": True, "status": after}
        failed = [step.text for step in steps if not step.ok]
        if failed:
            result["error"] = failed[0][:300]
        if without_login and not after.get("without_login"):
            from tow.i18n import t

            # Starting without signing in needs lingering for this user; TOW only says how.
            result["hint"] = t("autostart.linger_hint", user=self.install.user or "$USER")
        return result

    def disable(self) -> dict[str, Any]:
        before = self.status()
        if not before.get("ours") and not before.get("stale"):
            return refusal("autostart.other_install", where=before.get("where") or UNIT_NAME)
        changed = False
        if self.unit_path.exists():
            # Not --now: a TOW running right now keeps running; it just will not start again.
            self.run(["systemctl", "--user", "disable", UNIT_NAME])
            self.unit_path.unlink(missing_ok=True)
            self.run(["systemctl", "--user", "daemon-reload"])
            changed = True
        if self.desktop_path.exists():
            self.desktop_path.unlink(missing_ok=True)
            changed = True
        after = self.status()
        return {"ok": not after["on"] and "where" not in after, "changed": changed, "status": after}

    def start(self) -> bool:
        if read_text(self.unit_path) is None:
            return False
        return self.run(["systemctl", "--user", "start", UNIT_NAME]).ok
