"""macOS: a LaunchAgent ``~/Library/LaunchAgents/io.tow.plist`` for the owner's session.

KeepAlive only on failure: a stop asked through TOW (exit code 0) stays stopped, and a port
taken by another program ends ``tow run`` with 0 too when launchd started it
(``TOW_AUTOSTART=launchd``), because launchd has no limit on retries. ``ExitTimeOut`` gives
``tow run`` time to stop its job and web server after SIGTERM.

Turning it off unloads the agent (``launchctl bootout``) and removes the plist: launchd stops a
TOW it started, and nothing keeps it alive or starts it at the next sign-in. Starting without
signing in would need a system LaunchDaemon (administrator): not offered.
"""

from __future__ import annotations

import plistlib
from pathlib import Path
from typing import Any

from tow.autostart import AGENT_LABEL, Install, Runner, refusal, write_text

# launchd waits this long after SIGTERM before it kills `tow run` (a running job gets 20 s,
# then it and the web server are stopped).
EXIT_TIMEOUT_SEC = 60


class LaunchAgent:
    name = "macos"
    supports_without_login = False

    def __init__(self, install: Install, runner: Runner):
        self.install = install
        self.run = runner

    @property
    def plist_path(self) -> Path:
        return self.install.home / "Library" / "LaunchAgents" / f"{AGENT_LABEL}.plist"

    @property
    def command(self) -> Path:
        return self.install.venv_bin("tow")

    @property
    def domain(self) -> str:
        return f"gui/{self.install.uid}"

    @property
    def target(self) -> str:
        return f"{self.domain}/{AGENT_LABEL}"

    def plist(self) -> dict[str, Any]:
        root = self.install.root
        log = root / "data" / "logs" / "launchd.log"
        return {
            "Label": AGENT_LABEL,
            "ProgramArguments": [str(self.command), "run"],
            "WorkingDirectory": str(root),
            "EnvironmentVariables": {"TOW_ROOT": str(root), "TOW_AUTOSTART": "launchd"},
            "RunAtLoad": True,
            "KeepAlive": {"SuccessfulExit": False},
            "ThrottleInterval": 60,
            "ExitTimeOut": EXIT_TIMEOUT_SEC,
            "ProcessType": "Adaptive",
            "StandardOutPath": str(log),
            "StandardErrorPath": str(log),
        }

    def _read(self) -> dict[str, Any] | None:
        try:
            with self.plist_path.open("rb") as handle:
                value = plistlib.load(handle)
        except OSError, ValueError, plistlib.InvalidFileException:
            return None
        return value if isinstance(value, dict) else None

    def loaded(self) -> bool:
        return self.run(["launchctl", "print", self.target]).ok

    def status(self) -> dict[str, Any]:
        current = self._read()
        status: dict[str, Any] = {"backend": self.name, "on": False, "ours": True, "without_login": False}
        if current is None:
            return status
        arguments = current.get("ProgramArguments") or []
        registered = str(arguments[0]) if arguments else ""
        status["where"] = str(self.plist_path)
        status["command"] = registered
        status["ours"] = registered == str(self.command)
        # An agent whose program is gone (the install was moved or deleted) may be taken over.
        status["stale"] = not status["ours"] and not (registered and Path(registered).exists())
        loaded = self.loaded()
        status["loaded"] = loaded
        status["on"] = current == self.plist() and loaded
        return status

    def enable(self, *, without_login: bool = False) -> dict[str, Any]:
        if without_login:
            return refusal("autostart.without_login_unsupported")
        if not self.install.is_runtime:
            return refusal("autostart.not_runtime")
        if not self.command.is_file():
            return refusal("autostart.no_venv", path=str(self.command))
        before = self.status()
        if not before.get("ours") and not before.get("stale"):
            return refusal("autostart.other_install", where=before.get("where") or AGENT_LABEL)
        if before.get("loaded") and before.get("stale"):
            self.run(["launchctl", "bootout", self.target])  # the moved install's agent
        (self.install.root / "data" / "logs").mkdir(parents=True, exist_ok=True)
        write_text(self.plist_path, plistlib.dumps(self.plist()).decode("utf-8"))
        self.run(["launchctl", "enable", self.target])
        error = ""
        if not self.loaded():
            # Over SSH without a GUI session for this user this fails: sign in at the Mac once.
            loaded = self.run(["launchctl", "bootstrap", self.domain, str(self.plist_path)])
            error = "" if loaded.ok else loaded.text[:300]
        after = self.status()
        result: dict[str, Any] = {"ok": bool(after["on"]), "changed": True, "status": after}
        if error:
            result["error"] = error
        return result

    def disable(self) -> dict[str, Any]:
        before = self.status()
        if "where" not in before:
            return {"ok": True, "changed": False, "status": before}
        if not before.get("ours") and not before.get("stale"):
            return refusal("autostart.other_install", where=before.get("where") or AGENT_LABEL)
        result: dict[str, Any] = {"changed": True}
        if before.get("loaded"):
            # Unloaded, not only removed: a loaded agent would keep TOW alive (and launchd would
            # start it again) until the next sign-in. A TOW launchd started stops with it.
            stopped = self.run(["launchctl", "bootout", self.target])
            if not stopped.ok:
                result["error"] = stopped.text[:300]
            else:
                from tow.i18n import t

                result["hint"] = t("autostart.launchd_stopped")
        self.plist_path.unlink(missing_ok=True)
        after = self.status()
        return {**result, "ok": "where" not in after and not self.loaded(), "status": after}

    def start(self) -> bool:
        if self._read() is None:
            return False
        if self.loaded():
            return self.run(["launchctl", "kickstart", self.target]).ok
        return self.run(["launchctl", "bootstrap", self.domain, str(self.plist_path)]).ok
