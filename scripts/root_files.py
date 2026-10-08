"""The start files in the Windows bundle's folder: "Start TOW.cmd", "Stop TOW.cmd", "Update TOW.cmd".

scripts/build-bundle.py writes them into the zip, and scripts/update.py writes them again after
an update (``refresh``), so an install that began with an older zip gets the files of the version
it runs: up to 1.28.1 they stayed as the first zip had them, and an "Update TOW.cmd" of 1.22
could not recover an update that was cut off ("can't open file ...\\app\\scripts\\update.py").
They are TOW's files: one that differs is replaced, except an "Update TOW.cmd" without the line
that runs update.py (docs/PORTABLE.md §4).

Standard library only and Python 3.11 syntax: update.py loads this file with the install's base
Python. The files are ASCII with CRLF line ends and have no labels and no blocks, as before.
"""

from __future__ import annotations

import os
from pathlib import Path

START_CMD = r"""@echo off
rem TOW: double-click to start it. Its page opens in your browser (http://127.0.0.1:8787).
rem The first start prepares TOW inside this folder, without the internet. Everything stays in
rem this folder. Stop: "Stop TOW.cmd". Help: README.txt
title TOW
setlocal
for %%I in ("%~dp0.") do set "TOW_DIR=%%~fI"
rem An update cut off while it replaced the code leaves its record, and app\ (app\scripts too) may
rem hold half of either version: the rule of app\scripts\tow-start.cmd, checked here before anything
rem in app\ runs (not when the record says the switch is finished, nor while an update holds
rem .update.lock). PowerShell reads both.
set "TOW_CUT="
if exist "%~dp0.update-switch.json" set "TOW_CUT=1"
if defined TOW_CUT "%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe" -NoProfile -NonInteractive -ExecutionPolicy Bypass -Command "$d = $env:TOW_DIR; try { $j = Get-Content -LiteralPath (Join-Path $d '.update-switch.json') -Raw | ConvertFrom-Json; if ($j.phase -eq 'accepted' -or $j.restored -eq $true) { exit 0 } } catch {}; try { $f = [IO.File]::Open((Join-Path $d '.update.lock'), 'Open', 'ReadWrite', 'ReadWrite') } catch { exit 3 }; try { $f.Lock(0, 1) } catch { $f.Close(); exit 0 }; $f.Unlock(0, 1); $f.Close(); exit 3" >nul 2>&1 && set "TOW_CUT="
if defined TOW_CUT echo TOW was not started: an update was cut off while it replaced the code in "%TOW_DIR%". Run "Update TOW.cmd" there again: it puts the previous version back first.
if not defined TOW_CUT if not exist "%~dp0app\scripts\tow-start.cmd" set "TOW_CUT=2"
if "%TOW_CUT%"=="2" echo TOW was not started: its code is incomplete, "%~dp0app\scripts\tow-start.cmd" is missing. To put it back, run "Update TOW.cmd" there in a terminal with the version you had: "Update TOW.cmd" vX.Y.Z
if defined TOW_CUT pause
if defined TOW_CUT exit /b 3
call "%~dp0app\scripts\tow-start.cmd" %*
set "TOW_CODE=%ERRORLEVEL%"
if not "%TOW_CODE%"=="0" echo.
if not "%TOW_CODE%"=="0" echo TOW did not start: the reason is above. The logs are in "%~dp0data\logs".
if not "%TOW_CODE%"=="0" pause
exit /b %TOW_CODE%
"""

STOP_CMD = r"""@echo off
rem TOW: double-click to stop it (a check that is running finishes first).
title TOW
setlocal
for %%I in ("%~dp0.") do set "TOW_DIR=%%~fI"
set "TOW_CODE=0"
rem Without app\scripts (an update cut off while it replaced the code) there is nothing to call.
if not exist "%~dp0app\scripts\tow.cmd" set "TOW_CODE=3"
if "%TOW_CODE%"=="3" if exist "%~dp0.update-switch.json" echo TOW cannot run: an update was cut off while it replaced the code in "%TOW_DIR%". Run "Update TOW.cmd" there again: it puts the previous version back first.
if "%TOW_CODE%"=="3" if not exist "%~dp0.update-switch.json" echo TOW cannot run: its code is incomplete, "%~dp0app\scripts\tow.cmd" is missing. To put it back, run "Update TOW.cmd" there in a terminal with the version you had: "Update TOW.cmd" vX.Y.Z
if "%TOW_CODE%"=="0" call "%~dp0app\scripts\tow.cmd" stop
if "%TOW_CODE%"=="0" set "TOW_CODE=%ERRORLEVEL%"
if not "%TOW_CODE%"=="0" pause
timeout /t 3 /nobreak >nul 2>&1
exit /b %TOW_CODE%
"""

# The first lines of "Update TOW.cmd", before ``RESUME`` (``refresh``): they mark a run of this very
# file with the offset of its ``RESUME`` line (0: the zip's file, which has none).
UPDATE_HEAD = r"""@echo off
setlocal
set "TOW_FILE_RUN={mark}"
"""

UPDATE_BODY = r"""rem TOW: double-click to update it to the latest release
rem (or, in a terminal: "Update TOW.cmd" v1.23.0).
rem It stops TOW, keeps a copy of the data and settings, switches the code, starts TOW and checks
rem it; if anything fails, the previous version comes back by itself. It needs the internet.
title TOW update
set "TOW_REF=%~1"
if not defined TOW_REF set "TOW_REF=latest"
set "TOW_PYS=%~dp0runtime\python"
set "TOW_CFG=%~dp0app\.venv\pyvenv.cfg"
set "TOW_RE=^cpython-3\.[0-9][0-9]*\.[0-9][0-9]*-"
set "TOW_PY="
rem The Python the environment was made from (app\.venv\pyvenv.cfg "home"), by its folder name in
rem runtime\python, so a moved folder still finds it.
if exist "%TOW_CFG%" for /f "usebackq tokens=1,* delims== " %%A in ("%TOW_CFG%") do if /i "%%A"=="home" ^
if exist "%TOW_PYS%\%%~nxB\python.exe" set "TOW_PY=%TOW_PYS%\%%~nxB\python.exe"
rem Else the newest cpython-3.X.Y there by number (3.14.10 after 3.14.8), never a link (uv's
rem cpython-3.X junction) and never a pre-release: set /a keeps the highest key, the name is kept
rem in TOW_V<X>_<Y>, and call reads the one of the highest key (no block, no label).
set /a "TOW_BEST=0"
rem A line that ends in ^ goes on in the next one, which must not start with a space.
if not defined TOW_PY for /f "delims=" %%D in ('dir /b /ad-l "%TOW_PYS%\cpython-3.*" 2^>nul ^| findstr "%TOW_RE%"') do ^
if exist "%TOW_PYS%\%%D\python.exe" for /f "tokens=3,4 delims=-." %%a in ("%%D") do set "TOW_V%%a_%%b=%%D" & ^
set /a "TOW_K=%%a*1000+%%b, TOW_BEST+=(TOW_K-TOW_BEST)&((TOW_BEST-TOW_K)>>31)"
set /a "TOW_X=TOW_BEST/1000, TOW_Y=TOW_BEST%%1000"
if not defined TOW_PY if not "%TOW_BEST%"=="0" call set "TOW_PYDIR=%%TOW_V%TOW_X%_%TOW_Y%%%"
if not defined TOW_PY if defined TOW_PYDIR set "TOW_PY=%TOW_PYS%\%TOW_PYDIR%\python.exe"
if not defined TOW_PY echo TOW's Python is not in "%~dp0runtime\python": start TOW once with "Start TOW.cmd" first.
if not defined TOW_PY pause
if not defined TOW_PY exit /b 3
set "TOW_UPDATE=%~dp0app\scripts\update.py"
if exist "%~dp0.update-switch.json" set "TOW_UPDATE=%~dp0runtime\update.py"
if not exist "%TOW_UPDATE%" set "TOW_UPDATE=%~dp0runtime\update.py"
"%TOW_PY%" "%TOW_UPDATE%" --ref "%TOW_REF%"
set "TOW_CODE=%ERRORLEVEL%"
pause
exit /b %TOW_CODE%
"""

UPDATE_CMD = UPDATE_HEAD.replace("{mark}", "0") + UPDATE_BODY

ROOT_FILES = {"Start TOW.cmd": START_CMD, "Stop TOW.cmd": STOP_CMD, "Update TOW.cmd": UPDATE_CMD}

# cmd.exe reads a running batch file again after each command, from the byte where it stopped. An
# "Update TOW.cmd" replaced while it waits for update.py goes on in the new file at the offset just
# after the line that ran update.py (``resume_offset``). ``update_file`` makes the new file's own
# update.py line end at that byte (``rem`` lines before it), so the old run goes on with the new
# file's last three lines - the exit code, the pause, the end - as every version of the file has
# them. Where that does not fit (the new file is longer up to that line), this line is put at that
# byte instead: it ends the old run the same way, and a run of the new file, whose first lines set
# TOW_FILE_RUN to this line's offset, passes over it. Not merely "defined": a run of an older file
# written this way set it too, passed the line, and ran the update a second time.
RESUME = 'if not "%TOW_FILE_RUN%"=="{mark}" pause & exit /b %ERRORLEVEL%\n'
# ``rem`` lines that fill the space before that byte are at most this long (cmd reads 8191 at most).
_PADDING_LINE = 1000
# The line of every "Update TOW.cmd" since 1.22.0 that runs update.py.
_RUNS_UPDATE = b'--ref "%TOW_REF%"'


def crlf(text: str) -> str:
    """Windows line ends: cmd.exe reads labels and blocks reliably only with them."""
    return text.replace("\r\n", "\n").replace("\n", "\r\n")


def rendered() -> dict[str, bytes]:
    """The files as the zip holds them."""
    return {name: crlf(text).encode("ascii") for name, text in ROOT_FILES.items()}


def resume_offset(current: bytes) -> int | None:
    """Where cmd.exe goes on in ``current`` (an "Update TOW.cmd") once update.py returns: the end
    of the line that runs it. None: not a file TOW wrote."""
    position, found = 0, None
    for line in current.splitlines(keepends=True):
        position += len(line)
        if _RUNS_UPDATE in line and not line.lstrip().lower().startswith(b"rem"):
            found = position
    return found


def _padding(size: int) -> bytes | None:
    """``size`` bytes of ``rem`` lines; None when no such lines are that long (below 0, 1 to 4)."""
    if size < 0 or 0 < size < len(b"rem\r\n"):
        return None
    lines = []
    while size:
        take = min(size, _PADDING_LINE)
        if 0 < size - take < len(b"rem\r\n"):
            take = size - len(b"rem\r\n")
        lines.append(b"rem" + b" " * (take - len(b"rem\r\n")) + b"\r\n")
        size -= take
    return b"".join(lines)


def update_file(resume_at: int | None) -> bytes | None:
    """ "Update TOW.cmd" that a run of the file it replaces, stopped at ``resume_at``, ends safely
    in; None when it cannot (``RESUME`` does not fit there either)."""
    if resume_at is None:
        return None
    body = crlf(UPDATE_BODY).encode("ascii")
    ends = resume_offset(body) or 0  # the end of the body's own update.py line
    head = crlf(UPDATE_HEAD.replace("{mark}", "0")).encode("ascii")
    padding = _padding(resume_at - len(head) - ends)
    if padding is not None:
        return head + padding + body
    mark = str(resume_at)
    head = crlf(UPDATE_HEAD.replace("{mark}", mark)).encode("ascii")
    padding = _padding(resume_at - len(head))
    if padding is None:
        return None
    return head + padding + crlf(RESUME.replace("{mark}", mark)).encode("ascii") + body


def _write(path: Path, data: bytes) -> None:
    temporary = path.with_name(f".{path.name}.tmp")
    with temporary.open("wb") as handle:
        handle.write(data)
        handle.flush()
        os.fsync(handle.fileno())
    os.replace(temporary, path)


def refresh(root: Path, *, resume_at: int | None = None) -> tuple[list[str], list[str]]:
    """Write this version's start files into ``root`` where they differ: (written, kept).

    ``resume_at``: where a run of the current "Update TOW.cmd" goes on (``resume_offset`` of the
    file as it was when this update began); found in the file when not given. An "Update TOW.cmd"
    TOW did not write (no line that runs update.py) is kept: it may be the one running."""
    written: list[str] = []
    kept: list[str] = []
    for name, data in rendered().items():
        path = root / name
        try:
            current: bytes | None = path.read_bytes()
        except FileNotFoundError:
            current = None
        if name == "Update TOW.cmd" and current is not None:
            if current == data:
                continue
            fitted = update_file(resume_at if resume_at is not None else resume_offset(current))
            if fitted is None:
                kept.append(name)
                continue
            if fitted == current:
                continue
            data = fitted
        elif current == data:
            continue
        _write(path, data)
        written.append(name)
    return written, kept
