@echo off
rem Python and TOW's environment inside the install (runtime\python, runtime\cache, app\.venv).
rem Run through `tow.cmd setup` with TOW stopped: after moving the install, a fresh clone or a new
rem Python version. It needs uv (runtime\bin\uv.exe or on PATH) and, the first time, the network.
rem In a development checkout it is `uv sync --frozen`, as before.
setlocal
if /i "%~1"=="setup" shift /1
if not "%~1"=="" (echo Usage: tow.cmd setup ^(no options^): Python and TOW's environment inside the install; run it with TOW stopped.& exit /b 2)
call "%~dp0tow-env.cmd"
set "TOW_REBUILD="
"%TOW_UV%" --version >nul 2>&1 || echo TOW setup: uv was not found. Put uv.exe into "%TOW_ROOT%\runtime\bin" or on PATH.
"%TOW_UV%" --version >nul 2>&1 || exit /b 3
cd /d "%TOW_APP%" || exit /b 3
if /i "%TOW_ROOT%"=="%TOW_APP%" "%TOW_UV%" sync --frozen
if /i "%TOW_ROOT%"=="%TOW_APP%" exit /b %ERRORLEVEL%
rem Never under a running TOW: Windows lets .venv be renamed while its programs run. Another
rem program or TOW folder on the port is only named (setup_check prints it); an older .venv has busy().
if exist ".venv\Scripts\python.exe" ".venv\Scripts\python.exe" -c "import sys; from tow.supervisor import layout as l; sys.exit(l.setup_check() if hasattr(l, 'setup_check') else 4 if l.busy() else 0)" 2>nul
rem Only exit 4 means busy: a moved Python launcher may return 103 (base Python not found).
if "%ERRORLEVEL%"=="4" (echo TOW setup: TOW is running from this folder. Stop it first ^(tow.cmd stop^), then run setup again.& exit /b 3)
rem .venv holds absolute paths: built again when its Python no longer runs (a moved folder) or is
rem not the one inside the install (an install from before 1.18).
if exist ".venv\Scripts\python.exe" ".venv\Scripts\python.exe" -c "import os,sys; base=os.path.normcase(os.path.realpath(sys.base_prefix)); managed=os.path.normcase(os.path.realpath(os.environ['UV_PYTHON_INSTALL_DIR'])); sys.exit(os.path.commonpath([base,managed]) != managed)" >nul 2>&1 || set "TOW_REBUILD=1"
rem A running TOW holds its files: the folder cannot be renamed then, and nothing is removed.
if defined TOW_REBUILD move ".venv" ".venv-old" >nul 2>&1 || echo TOW setup: app\.venv is in use. Stop TOW, then run setup again.
if defined TOW_REBUILD if exist ".venv" exit /b 3
if exist ".venv-old" rmdir /s /q ".venv-old"
rem The Python of .python-version, into runtime\python; no copies on PATH, no registry entries.
"%TOW_UV%" python install --no-bin --no-registry || exit /b 3
"%TOW_UV%" sync --frozen --no-dev || exit /b 3
rem First start: TOW creates its master key (keys\master.key) and says to keep a copy of it.
"%TOW_EXE%" keys ensure
