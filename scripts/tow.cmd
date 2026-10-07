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
rem One last line, so its exit code is TOW's (`exit /b` inside a block would lose it): the
rem environment's own tow.exe; without one (not set up yet) uv runs TOW from the locked environment.
if /i "%~1"=="setup" (call "%~dp0tow-setup.cmd" %*) else if exist "%TOW_EXE%" ("%TOW_EXE%" %*) else ("%TOW_UV%" run --frozen --no-dev --project "%TOW_APP%" tow %*)
