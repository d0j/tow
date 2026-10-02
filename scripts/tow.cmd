@echo off
rem TOW with this install's environment, e.g. `tow.cmd doctor --json`, `tow.cmd keys adopt`.
rem `tow.cmd setup` installs Python and TOW's environment inside the install (after a move, a fresh
rem clone or a new Python version): see docs/PORTABLE.md.
setlocal
call "%~dp0tow-env.cmd"
rem One last line, so its exit code is TOW's (`exit /b` inside a block would lose it): the
rem environment's own tow.exe; without one (not set up yet) uv runs TOW from the locked environment.
if /i "%~1"=="setup" (call "%~dp0tow-setup.cmd" %*) else if exist "%TOW_EXE%" ("%TOW_EXE%" %*) else ("%TOW_UV%" run --frozen --no-dev --project "%TOW_APP%" tow %*)
