@echo off
rem The environment of a TOW install for the Windows launchers (docs/PORTABLE.md):
rem   TOW_APP   the code: the folder above this script
rem   TOW_ROOT  the install: app\, config.yaml, data\, keys\, backup\, runtime\ (in a development
rem             checkout the checkout itself)
rem   uv, Python and uv's cache inside the install (runtime\) for a runtime install.
rem No labels or blocks here: cmd.exe misreads them in files with LF line endings.
for %%I in ("%~dp0..") do set "TOW_APP=%%~fI"
rem The install is the parent of an app folder that has config.yaml or data\ next to it.
for %%I in ("%TOW_APP%") do set "TOW_APP_NAME=%%~nxI"
for %%I in ("%TOW_APP%\..") do set "TOW_PARENT=%%~fI"
if not defined TOW_ROOT if /i "%TOW_APP_NAME%"=="app" if exist "%TOW_PARENT%\config.yaml" set "TOW_ROOT=%TOW_PARENT%"
if not defined TOW_ROOT if /i "%TOW_APP_NAME%"=="app" if exist "%TOW_PARENT%\data\" set "TOW_ROOT=%TOW_PARENT%"
if not defined TOW_ROOT set "TOW_ROOT=%TOW_APP%"
set "TOW_APP_NAME="
set "TOW_PARENT="
rem The installed console script: launchers need neither uv nor any uv on PATH at runtime.
if not defined TOW_EXE set "TOW_EXE=%TOW_APP%\.venv\Scripts\tow.exe"
if not defined TOW_HOME set "TOW_HOME=%TOW_ROOT%\data"
rem Output that goes to logs\*.log keeps Cyrillic readable (stdout/stderr only).
set "PYTHONIOENCODING=utf-8"
if not defined TOW_CONFIG if exist "%TOW_ROOT%\config.yaml" set "TOW_CONFIG=%TOW_ROOT%\config.yaml"
rem The master key (keys\master.key, or a data\master.key of an older install) and data\lan-auth.token
rem are found by TOW itself (tow.store, tow.auth), the same way on every system and every start.
rem uv: the one inside the install when present; Python, its downloads and the cache stay inside too.
set "TOW_UV=uv"
if exist "%TOW_ROOT%\runtime\bin\uv.exe" set "TOW_UV=%TOW_ROOT%\runtime\bin\uv.exe"
if /i not "%TOW_ROOT%"=="%TOW_APP%" set "UV_PYTHON_INSTALL_DIR=%TOW_ROOT%\runtime\python"
if /i not "%TOW_ROOT%"=="%TOW_APP%" set "UV_PYTHON_BIN_DIR=%TOW_ROOT%\runtime\bin"
if /i not "%TOW_ROOT%"=="%TOW_APP%" set "UV_CACHE_DIR=%TOW_ROOT%\runtime\cache"
if /i not "%TOW_ROOT%"=="%TOW_APP%" set "UV_PROJECT_ENVIRONMENT=%TOW_APP%\.venv"
if /i not "%TOW_ROOT%"=="%TOW_APP%" set "UV_MANAGED_PYTHON=1"
