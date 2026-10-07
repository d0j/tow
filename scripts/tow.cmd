@echo off
rem TOW with this install's environment, e.g. `tow.cmd doctor --json`, `tow.cmd keys adopt`.
rem `tow.cmd setup` installs Python and TOW's environment inside the install (after a move, a fresh
rem clone or a new Python version): see docs/PORTABLE.md.
setlocal
call "%~dp0tow-env.cmd"
rem A moved folder: its environment's Python points to the old place and every command failed with
rem "uv trampoline failed to canonicalize script path". Said here, like tow-start.cmd does.
set "TOW_MOVED="
if /i not "%~1"=="setup" if exist "%TOW_APP%\.venv\Scripts\python.exe" "%TOW_APP%\.venv\Scripts\python.exe" -I -S -c "" >nul 2>&1 || set "TOW_MOVED=1"
if defined TOW_MOVED echo TOW's environment still belongs to the folder TOW was moved or copied from. Run "%~f0" setup first.
if defined TOW_MOVED exit /b 3
rem An update cut off while it replaced the code (the rule of tow-start.cmd): app\ may hold half of the
rem new code and no environment. Setup, or uv without an environment, would build one from it (a raw
rem uv error and a stray app\.venv); TOW's own tow.exe says it itself (`tow status`, `tow start`).
set "TOW_CUT="
if exist "%TOW_ROOT%\.update-switch.json" if /i "%~1"=="setup" set "TOW_CUT=1"
if exist "%TOW_ROOT%\.update-switch.json" if not exist "%TOW_EXE%" set "TOW_CUT=1"
if defined TOW_CUT "%SystemRoot%\System32\WindowsPowerShell\v1.0\powershell.exe" -NoProfile -NonInteractive -ExecutionPolicy Bypass -Command "$d = $env:TOW_ROOT; try { $j = Get-Content -LiteralPath (Join-Path $d '.update-switch.json') -Raw | ConvertFrom-Json; if ($j.phase -eq 'accepted' -or $j.restored -eq $true) { exit 0 } } catch {}; try { $f = [IO.File]::Open((Join-Path $d '.update.lock'), 'Open', 'ReadWrite', 'ReadWrite') } catch { exit 3 }; try { $f.Lock(0, 1) } catch { $f.Close(); exit 0 }; $f.Unlock(0, 1); $f.Close(); exit 3" >nul 2>&1 && set "TOW_CUT="
if defined TOW_CUT echo TOW cannot run: an update was cut off while it replaced the code in "%TOW_ROOT%". Run "Update TOW.cmd" there again: it puts the previous version back first.
if defined TOW_CUT exit /b 3
rem One last line, so its exit code is TOW's (`exit /b` inside a block would lose it): the
rem environment's own tow.exe; without one (not set up yet) uv runs TOW from the locked environment.
if /i "%~1"=="setup" (call "%~dp0tow-setup.cmd" %*) else if exist "%TOW_EXE%" ("%TOW_EXE%" %*) else ("%TOW_UV%" run --frozen --no-dev --project "%TOW_APP%" tow %*)
