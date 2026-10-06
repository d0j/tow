@echo off
rem Start TOW and open its page: what "Start TOW.cmd" of the Windows bundle runs (docs/install.md).
rem The first time (or after the folder moved) it prepares Python and TOW's environment inside the
rem install: offline from runtime\cache when the bundle carries the packages, else from the
rem internet. Then `tow start`: TOW in the background, its page in the browser (TOW_NO_BROWSER=1:
rem no browser). No labels: this file has LF line endings (see tow-env.cmd).
setlocal
call "%~dp0tow-env.cmd"
rem An update cut off while it replaced the code leaves its record: app\ may hold half of the new code.
if exist "%TOW_ROOT%\.update-switch.json" echo TOW was not started: an update was cut off while it replaced the code in "%TOW_ROOT%". Run "Update TOW.cmd" there again: it puts the previous version back first.
if exist "%TOW_ROOT%\.update-switch.json" exit /b 3
set "TOW_READY="
set "TOW_PREPARED="
set "TOW_HERE=%TOW_ROOT%"
rem Ready: the environment runs, and it is this folder's own (not the one of a moved or copied folder).
if exist "%TOW_APP%\.venv\Scripts\python.exe" "%TOW_APP%\.venv\Scripts\python.exe" -c "import os, sys, tow; here = os.path.realpath(os.environ['TOW_HERE']).lower() + os.sep; sys.exit(0 if all(os.path.realpath(p).lower().startswith(here) for p in (tow.__file__, sys.base_prefix)) else 1)" >nul 2>&1 && set "TOW_READY=1"
if not defined TOW_READY set "TOW_PREPARED=1"
if not defined TOW_READY echo Preparing TOW in "%TOW_ROOT%" - the first time takes a minute or two...
rem Files from a downloaded zip carry the "Mark of the Web", and Windows would ask about each one.
if not defined TOW_READY powershell.exe -NoProfile -NonInteractive -ExecutionPolicy Bypass -Command "Get-ChildItem -LiteralPath $env:TOW_HERE -Recurse -File -ErrorAction SilentlyContinue | Unblock-File -ErrorAction SilentlyContinue" >nul 2>&1
if not defined TOW_READY set "UV_OFFLINE=1"
if not defined TOW_READY call "%~dp0tow-setup.cmd"
if not defined TOW_READY if not errorlevel 1 set "TOW_READY=1"
set "UV_OFFLINE="
if not defined TOW_READY echo The packages inside this folder were not enough: downloading them from the internet...
if not defined TOW_READY call "%~dp0tow-setup.cmd"
if not defined TOW_READY if not errorlevel 1 set "TOW_READY=1"
if not defined TOW_READY echo TOW could not be prepared: the reason is above.
if not defined TOW_READY exit /b 3
"%TOW_EXE%" start %*
set "TOW_CODE=%ERRORLEVEL%"
if not "%TOW_CODE%"=="0" exit /b %TOW_CODE%
rem The first start: the window stays until a key is pressed, so the master key note is read.
if defined TOW_PREPARED echo.
if defined TOW_PREPARED echo Keep a copy of "%TOW_ROOT%\keys\master.key" somewhere safe. TOW keeps running when this window closes.
if defined TOW_PREPARED pause
exit /b 0
