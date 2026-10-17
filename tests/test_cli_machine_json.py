"""`tow ... --json` is machine output: ASCII JSON whose strings escape every other character (X27).

Windows PowerShell 5.1 decodes a native program's pipe in the console's OEM code page (cp866 on a
Russian Windows), whatever the program writes: TOW writes UTF-8, so a Cyrillic install folder came
back as "╨в╨╡╤Б╤В" from `tow.cmd keys status --json | ConvertFrom-Json` (measured on 5.1.26100).
JSON's own \\u escapes read the same in every code page. What TOW says to a person stays readable.
"""

from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

from tow import cli

FOLDER = "TOW Тест «ё»"


def test_machine_json_is_ascii_and_reads_back_the_cyrillic_folder(tmp_path, monkeypatch, capsys):
    root = tmp_path / FOLDER
    root.mkdir()
    monkeypatch.setenv("TOW_ROOT", str(root))
    assert cli.main(["keys", "status", "--json"]) == 0
    out = capsys.readouterr().out
    assert out.isascii()
    assert json.loads(out)["install_key_file"] == str(root / "keys" / "master.key")


def test_a_refusal_in_json_is_ascii_too(monkeypatch, capsys):
    monkeypatch.setattr(cli, "_foreign_code_refusal", lambda: "папка «Тест»: сначала setup")
    cli.main(["status", "--json"])
    out = capsys.readouterr().out
    assert out.isascii()
    assert json.loads(out)["error"] == "папка «Тест»: сначала setup"


@pytest.mark.allow_system  # Windows PowerShell 5.1 reads this Python's TOW through a native pipe
@pytest.mark.skipif(os.name != "nt", reason="Windows PowerShell 5.1 is Windows'")
def test_windows_powershell_5_1_reads_the_folder_from_the_pipe(tmp_path):
    root = tmp_path / FOLDER
    root.mkdir()
    system = Path(os.environ.get("SYSTEMROOT", r"C:\Windows"))
    shell = system / "System32" / "WindowsPowerShell" / "v1.0" / "powershell.exe"
    # The script names the folder by its characters' codes: a .ps1 or -Command text has no
    # code page of its own to trust either.
    expected = "+".join(f"[char]{ord(char)}" for char in str(root / "keys" / "master.key"))
    script = (
        "$ErrorActionPreference = 'Stop'; "
        f"$expected = [string]::Join('', @({expected.replace('+', ',')})); "
        '$read = (& $env:TOW_PY -m tow keys status --json) -join "`n" | ConvertFrom-Json; '
        "if ($read.install_key_file -ceq $expected) { 'same' } else { 'garbled: ' + $read.install_key_file }"
    )
    env = {key: value for key, value in os.environ.items() if not key.upper().startswith("TOW_")}
    env.update(TOW_ROOT=str(root), TOW_PY=sys.executable, PYTHONIOENCODING="utf-8")  # what tow-env.cmd gives
    done = subprocess.run(
        [str(shell), "-NoProfile", "-NonInteractive", "-Command", script],
        capture_output=True,
        text=True,
        encoding="utf-8",
        errors="replace",
        env=env,
        timeout=120,
        check=False,
    )
    assert done.returncode == 0, done.stdout + done.stderr
    assert done.stdout.strip() == "same", done.stdout + done.stderr
