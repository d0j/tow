"""Windows: one Task Scheduler task "TOW" running ``pythonw.exe -m tow run``.

Created from XML and read back from XML, which is locale-independent: the LIST output is
translated by Windows.
"""

from __future__ import annotations

import os
import uuid
import xml.etree.ElementTree as ET
from html import escape
from pathlib import Path
from typing import Any

from tow.autostart import TASK_NAME, CommandResult, Install, Runner, refusal

ARGUMENTS = "-m tow run"
_MISSING = ("cannot find", "not found", "не удается найти", "не найден", "не удаётся найти")


def principal() -> str:
    domain = os.environ.get("USERDOMAIN", "").strip()
    if not domain or domain.upper() == "WORKGROUP":
        domain = os.environ.get("COMPUTERNAME", "").strip()
    username = os.environ.get("USERNAME", "").strip() or os.environ.get("USER", "").strip()
    return f"{domain}\\{username}" if domain and username else username


def _local(element: ET.Element) -> str:
    return element.tag.rsplit("}", 1)[-1]


def parse_task(raw: str) -> dict[str, Any] | None:
    """The parts of a task's XML that say what it runs and when (None: not a task)."""
    try:
        root = ET.fromstring(raw.strip())
    except ET.ParseError:
        return None

    def text(name: str, under: ET.Element = root) -> str:
        for element in under.iter():
            if _local(element) == name and element.text:
                return element.text.strip()
        return ""

    settings = next((child for child in root if _local(child) == "Settings"), None)
    triggers = next((child for child in root if _local(child) == "Triggers"), None)
    enabled = next(
        (
            (child.text or "").strip()
            for child in (settings if settings is not None else [])
            if _local(child) == "Enabled"
        ),
        "true",
    )
    restart = next((element for element in root.iter() if _local(element) == "RestartOnFailure"), None)
    return {
        "command": text("Command").strip('"'),
        "arguments": text("Arguments"),
        "working_directory": text("WorkingDirectory"),
        "logon_type": text("LogonType"),
        "triggers": sorted(_local(child) for child in (triggers if triggers is not None else [])),
        "enabled": (enabled or "true").lower() == "true",
        "execution_limit": text("ExecutionTimeLimit"),
        "restart_count": text("Count", restart) if restart is not None else "",
    }


def same_path(left: str | Path, right: str | Path) -> bool:
    def norm(value: str | Path) -> str:
        return os.path.normcase(os.path.normpath(str(value).strip().strip('"').replace("/", "\\")))

    return norm(left) == norm(right)


class WindowsTask:
    name = "windows"
    supports_without_login = True

    def __init__(self, install: Install, runner: Runner):
        self.install = install
        self.run = runner

    @property
    def command(self) -> Path:
        return self.install.venv_bin("pythonw.exe")

    def xml(self, *, without_login: bool = False) -> str:
        user = escape(principal())
        logon = f"<LogonTrigger><Enabled>true</Enabled><UserId>{user}</UserId></LogonTrigger>"
        # Without signing in: at startup, as the owner's account but without a stored password
        # (S4U). Signing in later does not start a second one (IgnoreNew).
        triggers = f"<BootTrigger><Enabled>true</Enabled></BootTrigger>{logon}" if without_login else logon
        logon_type = "S4U" if without_login else "InteractiveToken"
        return f"""<?xml version="1.0" encoding="UTF-16"?>
<Task version="1.4" xmlns="http://schemas.microsoft.com/windows/2004/02/mit/task">
  <RegistrationInfo><Author>{user}</Author><Description>TOW — Torrent Watcher (tow run)</Description></RegistrationInfo>
  <Triggers>{triggers}</Triggers>
  <Principals>
    <Principal id="Author">
      <UserId>{user}</UserId>
      <LogonType>{logon_type}</LogonType>
      <RunLevel>LeastPrivilege</RunLevel>
    </Principal>
  </Principals>
  <Settings>
    <MultipleInstancesPolicy>IgnoreNew</MultipleInstancesPolicy>
    <DisallowStartIfOnBatteries>false</DisallowStartIfOnBatteries>
    <StopIfGoingOnBatteries>false</StopIfGoingOnBatteries>
    <AllowHardTerminate>true</AllowHardTerminate>
    <StartWhenAvailable>true</StartWhenAvailable>
    <RunOnlyIfNetworkAvailable>false</RunOnlyIfNetworkAvailable>
    <IdleSettings>
      <StopOnIdleEnd>false</StopOnIdleEnd>
      <RestartOnIdle>false</RestartOnIdle>
    </IdleSettings>
    <AllowStartOnDemand>true</AllowStartOnDemand>
    <Enabled>true</Enabled>
    <Hidden>false</Hidden>
    <RunOnlyIfIdle>false</RunOnlyIfIdle>
    <WakeToRun>false</WakeToRun>
    <ExecutionTimeLimit>PT0S</ExecutionTimeLimit>
    <Priority>7</Priority>
    <RestartOnFailure><Interval>PT1M</Interval><Count>999</Count></RestartOnFailure>
  </Settings>
  <Actions Context="Author">
    <Exec>
      <Command>{escape(str(self.command))}</Command>
      <Arguments>{ARGUMENTS}</Arguments>
      <WorkingDirectory>{escape(str(self.install.root))}</WorkingDirectory>
    </Exec>
  </Actions>
</Task>
"""

    # --- reading ------------------------------------------------------------------------------

    def query(self, name: str = TASK_NAME) -> dict[str, Any]:
        """A task by name: state absent / present / error, and its parsed XML."""
        result = self.run(["schtasks", "/Query", "/TN", name, "/XML"])
        if not result.ok:
            lowered = result.text.lower()
            state = "absent" if any(marker in lowered for marker in _MISSING) else "error"
            return {"name": name, "state": state, "error": result.text[:300] if state == "error" else ""}
        parsed = parse_task(result.stdout)
        if parsed is None:
            return {"name": name, "state": "error", "error": "task XML unreadable"}
        return {"name": name, "state": "present", **parsed}

    def status(self) -> dict[str, Any]:
        task = self.query()
        present = task.get("state") == "present"
        ours = present and same_path(task.get("command") or "", self.command)
        # A task whose program is gone (the install was moved or deleted) may be taken over.
        registered = str(task.get("command") or "")
        stale = present and not ours and not (registered and Path(registered).exists())
        problems = []
        if ours:
            if (task.get("arguments") or "").strip() != ARGUMENTS:
                problems.append("arguments")
            if not same_path(task.get("working_directory") or "", self.install.root):
                problems.append("working directory")
            if task.get("execution_limit") != "PT0S":
                problems.append("execution time limit")
            if task.get("restart_count") != "999":
                problems.append("restart on failure")
            if "LogonTrigger" not in (task.get("triggers") or []) and "BootTrigger" not in (task.get("triggers") or []):
                problems.append("trigger")
        without_login = ours and task.get("logon_type") == "S4U" and "BootTrigger" in (task.get("triggers") or [])
        return {
            "backend": self.name,
            "on": bool(ours and task.get("enabled") and not problems),
            "ours": ours,
            "stale": bool(stale),
            "without_login": bool(without_login),
            "where": TASK_NAME,
            "state": task.get("state"),
            "enabled": task.get("enabled"),
            "command": task.get("command"),
            "problems": problems,
            "error": task.get("error") or "",
        }

    # --- changing -----------------------------------------------------------------------------

    def _create(self, name: str, xml: str) -> CommandResult:
        from tow.paths import data_dir

        folder = data_dir() / "tmp"  # same as tow.paths.tmp_dir() in 1.18: never the system temp
        folder.mkdir(parents=True, exist_ok=True)
        path = folder / f"task-{name.lower()}-{uuid.uuid4().hex[:8]}.xml"
        try:
            path.write_text(xml, encoding="utf-16")
            return self.run(["schtasks", "/Create", "/F", "/TN", name, "/XML", str(path)])
        finally:
            path.unlink(missing_ok=True)

    def enable(self, *, without_login: bool = False) -> dict[str, Any]:
        if not self.install.is_runtime:
            return refusal("autostart.not_runtime")
        if not self.command.is_file():
            return refusal("autostart.no_venv", path=str(self.command))
        before = self.status()
        if before.get("state") == "present" and not before.get("ours") and not before.get("stale"):
            return refusal("autostart.other_install", where=before.get("command") or TASK_NAME)
        created = self._create(TASK_NAME, self.xml(without_login=without_login))
        after = self.status()
        ok = created.ok and after["on"] and after["without_login"] == without_login
        result: dict[str, Any] = {"ok": ok, "changed": created.ok, "status": after}
        if not created.ok:
            result["error"] = created.text[:300]
        return result

    def disable(self) -> dict[str, Any]:
        before = self.status()
        if before.get("state") == "absent":
            return {"ok": True, "changed": False, "status": before}
        if before.get("state") == "present" and not before.get("ours") and not before.get("stale"):
            return refusal("autostart.other_install", where=before.get("command") or TASK_NAME)
        deleted = self.run(["schtasks", "/Delete", "/TN", TASK_NAME, "/F"])
        after = self.status()
        result: dict[str, Any] = {"ok": after.get("state") == "absent", "changed": deleted.ok, "status": after}
        if not deleted.ok:
            result["error"] = deleted.text[:300]
        return result

    def start(self) -> bool:
        return self.run(["schtasks", "/Run", "/TN", TASK_NAME]).ok
