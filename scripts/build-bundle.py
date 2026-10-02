"""Build the Windows bundle: TOW-windows-x64.zip, one folder TOW\\ that starts offline.

    python scripts/build-bundle.py --out dist [--source tow-source.tar.gz]

    TOW\\
      Start TOW.cmd        the one file to double-click (app\\scripts\\tow-start.cmd does the work)
      Stop TOW.cmd, Update TOW.cmd, README.txt
      config.yaml          from config.example.yaml
      app\\                 the code (the source archive, else `git archive HEAD`) without tests
      runtime\\bin\\uv.exe   uv UV_VERSION, checked against the .sha256 of its release
      runtime\\python\\      the CPython of .python-version (uv python install)
      runtime\\cache\\       uv's cache holding every runtime wheel of uv.lock: the first start
                           builds app\\.venv from it without the internet

There is no app\\.venv in the zip: a venv holds absolute paths, so ``Start TOW.cmd`` builds it on
the first start (and again after the folder is moved). Standard library and uv only; it runs
on Windows (the Python and the wheels it puts in are this machine's) and needs the internet:
github.com (uv, the Python build) and pypi.org (the wheels). The finished zip is checked
(``check_zip``) and the cache is proved offline before the zip is written.
"""

from __future__ import annotations

import argparse
import hashlib
import io
import os
import re
import shutil
import stat
import subprocess
import sys
import tarfile
import tempfile
import time
import zipfile
from pathlib import Path
from urllib.request import Request, urlopen

REPO = Path(__file__).resolve().parents[1]
# uv inside the bundle (and install.sh downloads the same one: tests/test_bundle.py keeps them in step).
UV_VERSION = "0.12.20"
UV_TARGET = "x86_64-pc-windows-msvc"
UV_URL = f"https://github.com/astral-sh/uv/releases/download/{UV_VERSION}/uv-{UV_TARGET}.zip"
ZIP_NAME = "TOW-windows-x64.zip"
TOP = "TOW"
# Not needed to run TOW: left out of app\ (the source archive of an update brings them again).
LEFT_OUT = ("tests/", ".github/", ".githooks/")
# In runtime\cache only what a later `uv sync` reads: the wheels and their index. Interpreter
# facts and built packages are keyed by absolute paths of the build machine.
CACHE_DROPPED = ("interpreter-v", "builds-v", "sdists-v", "environments-v", ".tmp", "git-v")
DOCS = "https://github.com/d0j/tow/blob/main/docs/install.md"

START_CMD = r"""@echo off
rem TOW: double-click to start it. Its page opens in your browser (http://127.0.0.1:8787).
rem The first start prepares TOW inside this folder, without the internet. Everything stays in
rem this folder. Stop: "Stop TOW.cmd". Help: README.txt
title TOW
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
call "%~dp0app\scripts\tow.cmd" stop
set "TOW_CODE=%ERRORLEVEL%"
if not "%TOW_CODE%"=="0" pause
timeout /t 3 /nobreak >nul 2>&1
exit /b %TOW_CODE%
"""

UPDATE_CMD = r"""@echo off
rem TOW: double-click to update it to the latest release (or, in a terminal: "Update TOW.cmd" v1.23.0).
rem It stops TOW, keeps a copy of the data and settings, switches the code, starts TOW and checks
rem it; if anything fails, the previous version comes back by itself. It needs the internet.
title TOW update
setlocal
set "TOW_REF=%~1"
if not defined TOW_REF set "TOW_REF=latest"
set "TOW_PY="
for /d %%D in ("%~dp0runtime\python\cpython-3*") do if exist "%%~fD\python.exe" set "TOW_PY=%%~fD\python.exe"
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

README_TXT = """TOW {version} - torrent topic watcher

1. Double-click "Start TOW.cmd". The first start prepares TOW (a minute or two) and opens
   http://127.0.0.1:8787 in your browser. If Windows says "Windows protected your PC":
   More info, then Run anyway.
2. Copy keys\\master.key somewhere safe: without it, saved passwords cannot be restored.
3. Stop: "Stop TOW.cmd". Update: "Update TOW.cmd". Start with Windows: app\\scripts\\tow.cmd autostart on
Help: {docs}

1. Дважды щёлкните «Start TOW.cmd». Первый запуск готовит TOW (минуту-две) и открывает
   http://127.0.0.1:8787 в браузере. Если Windows пишет «Система Windows защитила ваш компьютер»:
   «Подробнее», затем «Выполнить в любом случае».
2. Сохраните копию keys\\master.key в надёжном месте: без неё сохранённые пароли не восстановить.
3. Остановить: «Stop TOW.cmd». Обновить: «Update TOW.cmd». Запуск вместе с Windows: app\\scripts\\tow.cmd autostart on
Инструкция: {docs_ru}
"""

ROOT_FILES = {"Start TOW.cmd": START_CMD, "Stop TOW.cmd": STOP_CMD, "Update TOW.cmd": UPDATE_CMD}


def say(text: str) -> None:
    print(f"build-bundle: {text}", flush=True)


def crlf(text: str) -> str:
    """Windows line ends: cmd.exe reads labels and blocks reliably only with them."""
    return text.replace("\r\n", "\n").replace("\n", "\r\n")


def readme(version: str) -> str:
    return README_TXT.format(version=version, docs=DOCS, docs_ru=DOCS.replace("/docs/", "/docs/ru/"))


# --- the code ------------------------------------------------------------------------------------


def _kept(relative: str) -> bool:
    return bool(relative) and not (relative.rstrip("/") + "/").startswith(LEFT_OUT)


def _source_tar(source: Path | None) -> tarfile.TarFile:
    if source is not None:
        return tarfile.open(source, "r:gz")
    archive = subprocess.run(
        ["git", "-C", str(REPO), "archive", "--format=tar", "HEAD"], capture_output=True, check=True
    )
    return tarfile.open(fileobj=io.BytesIO(archive.stdout), mode="r:")


def stage_code(app: Path, source: Path | None) -> None:
    """The code into ``app``: the source tarball's single top folder, else ``git archive HEAD``."""
    with _source_tar(source) as tar:
        members = tar.getmembers()
        tops = {member.name.split("/", 1)[0] for member in members}
        strip = source is not None and len(tops) == 1
        for member in members:
            name = member.name.split("/", 1)[1] if strip and "/" in member.name else ("" if strip else member.name)
            if not _kept(name) or ".." in Path(name).parts or member.issym() or member.islnk():
                continue
            target = app / name
            if member.isdir():
                target.mkdir(parents=True, exist_ok=True)
            elif member.isfile():
                target.parent.mkdir(parents=True, exist_ok=True)
                handle = tar.extractfile(member)
                assert handle is not None
                target.write_bytes(handle.read())


def write_root_files(top: Path, version: str) -> None:
    for name, text in ROOT_FILES.items():
        (top / name).write_bytes(crlf(text).encode("ascii"))
    (top / "README.txt").write_bytes(crlf(readme(version)).encode("utf-8-sig"))  # Notepad shows it right
    shutil.copyfile(top / "app" / "config.example.yaml", top / "config.yaml")


# --- uv, Python and the cache --------------------------------------------------------------------


def download(url: str) -> bytes:
    with urlopen(Request(url, headers={"User-Agent": "tow-build-bundle"}), timeout=300) as response:
        return response.read()


def fetch_uv(bin_dir: Path) -> Path:
    """uv.exe of UV_VERSION, checked against the checksum its release publishes."""
    say(f"uv {UV_VERSION}: {UV_URL}")
    data = download(UV_URL)
    expected = download(UV_URL + ".sha256").decode("ascii").split()[0].lower()
    if hashlib.sha256(data).hexdigest() != expected:
        raise SystemExit(f"build-bundle: {UV_URL} does not match its .sha256")
    with zipfile.ZipFile(io.BytesIO(data)) as archive:
        bin_dir.mkdir(parents=True, exist_ok=True)
        (bin_dir / "uv.exe").write_bytes(archive.read("uv.exe"))
    return bin_dir / "uv.exe"


def uv_env(top: Path, venv: Path) -> dict[str, str]:
    """The launchers' environment for this bundle (scripts/tow-env.cmd), the venv elsewhere."""
    runtime = top / "runtime"
    env = {key: value for key, value in os.environ.items() if not key.startswith(("UV_", "VIRTUAL_ENV"))}
    env.update(
        UV_PYTHON_INSTALL_DIR=str(runtime / "python"),
        UV_PYTHON_BIN_DIR=str(runtime / "bin"),
        UV_CACHE_DIR=str(runtime / "cache"),
        UV_PROJECT_ENVIRONMENT=str(venv),
        UV_MANAGED_PYTHON="1",
        UV_NO_CONFIG="1",  # the build machine's uv.toml stays out
    )
    return env


def run(argv: list[str], *, cwd: Path, env: dict[str, str]) -> None:
    say(" ".join(argv[1:]))
    subprocess.run(argv, cwd=str(cwd), env=env, check=True)


def prepare_runtime(top: Path, uv: Path, work: Path) -> None:
    """Python into runtime\\python, every wheel of uv.lock into runtime\\cache; then proved offline."""
    app = top / "app"
    python = (app / ".python-version").read_text(encoding="utf-8").strip()
    warm, check = work / "venv-warm", work / "venv-offline"
    run([str(uv), "python", "install", "--no-bin", "--no-registry", python], cwd=app, env=uv_env(top, warm))
    run([str(uv), "sync", "--frozen", "--no-dev"], cwd=app, env=uv_env(top, warm))
    remove(warm)
    run([str(uv), "cache", "prune"], cwd=app, env=uv_env(top, warm))
    prune_cache(top / "runtime" / "cache")
    prune_python(top / "runtime" / "python")
    # The proof: a new environment from the cache alone, and TOW imports in it.
    offline = {**uv_env(top, check), "UV_OFFLINE": "1", "UV_PYTHON_DOWNLOADS": "never"}
    run([str(uv), "sync", "--frozen", "--no-dev"], cwd=app, env=offline)
    imports = "import tow.cli, fastapi, uvicorn, cryptography, httpx, yaml, jinja2, bs4"
    run([str(check / "Scripts" / "python.exe"), "-c", imports], cwd=work, env=offline)
    remove(check)
    prune_cache(top / "runtime" / "cache")  # the proof's own build of TOW
    run([str(uv), "cache", "prune"], cwd=app, env=uv_env(top, warm))
    prune_python(top / "runtime" / "python")
    prune_code(app)


def prune_cache(cache: Path) -> None:
    for entry in cache.iterdir() if cache.is_dir() else []:
        if entry.name.startswith(CACHE_DROPPED):
            remove(entry)


def is_link(path: Path) -> bool:
    """A symbolic link or a junction (uv links cpython-3.14-... to cpython-3.14.7-... by an absolute path)."""
    return path.is_symlink() or os.path.isjunction(path)


def prune_python(folder: Path) -> None:
    """Only the interpreter folders (cpython-3.x.y-...) and uv's marker files stay; uv makes its
    links to them again where the bundle is unpacked."""
    for entry in folder.iterdir() if folder.is_dir() else []:
        if is_link(entry):
            os.rmdir(entry) if entry.is_dir() else entry.unlink()  # the link only, never its target
        elif entry.is_dir() and not entry.name.startswith("cpython-"):
            remove(entry)


def prune_code(app: Path) -> None:
    """What the proof's import left in the code (bytecode, with this machine's paths)."""
    for cache in sorted(app.rglob("__pycache__"), reverse=True):
        remove(cache)


def remove(path: Path) -> None:
    def writable(function, name, _error) -> None:  # read-only files of a venv (Windows)
        os.chmod(name, stat.S_IWRITE)
        function(name)

    for attempt in range(5):
        if not path.exists():
            return
        try:
            shutil.rmtree(path, onexc=writable)
            return
        except OSError:
            if attempt == 4:
                raise
            time.sleep(1)  # an antivirus scanner still holds a file


# --- the zip -------------------------------------------------------------------------------------


def write_zip(top: Path, destination: Path) -> None:
    """``top`` as the zip's one folder TOW/, in a stable order."""
    destination.parent.mkdir(parents=True, exist_ok=True)
    temporary = destination.with_name(destination.name + ".part")
    paths = []
    for folder, dirs, files in os.walk(top):
        dirs[:] = sorted(name for name in dirs if not is_link(Path(folder) / name))  # never followed
        paths += [Path(folder) / name for name in files if not is_link(Path(folder) / name)]
        if not dirs and not files:
            paths.append(Path(folder))
    with zipfile.ZipFile(temporary, "w", zipfile.ZIP_DEFLATED, compresslevel=9) as archive:
        for name, path in sorted((f"{TOP}/{path.relative_to(top).as_posix()}", path) for path in paths):
            if path.is_dir():
                archive.writestr(name + "/", b"")  # an empty folder
            else:
                archive.write(path, name)
    os.replace(temporary, destination)


REQUIRED = (
    "Start TOW.cmd",
    "Stop TOW.cmd",
    "Update TOW.cmd",
    "README.txt",
    "config.yaml",
    "app/pyproject.toml",
    "app/uv.lock",
    "app/.python-version",
    "app/scripts/tow.cmd",
    "app/scripts/tow-env.cmd",
    "app/scripts/tow-setup.cmd",
    "app/scripts/tow-start.cmd",
    "app/scripts/update.py",
    "app/src/tow/cli.py",
    "runtime/bin/uv.exe",
)


def check_zip(path: Path) -> list[str]:
    """What is wrong with a bundle (an empty list: nothing)."""
    problems = []
    with zipfile.ZipFile(path) as archive:
        names = archive.namelist()
        inside = {name[len(TOP) + 1 :] for name in names if name.startswith(f"{TOP}/")}
        problems += [f"outside {TOP}/: {name}" for name in names if not name.startswith(f"{TOP}/")]
        problems += [f"missing: {name}" for name in REQUIRED if name not in inside]
        if not any(re.fullmatch(r"runtime/python/cpython-3[^/]*/python\.exe", name) for name in inside):
            problems.append("missing: runtime/python/cpython-*/python.exe")
        if not any(name.startswith("runtime/cache/archive-v") for name in inside):
            problems.append("missing: the wheels in runtime/cache")
        for name in sorted(inside):
            parts = name.split("/")
            if ".venv" in parts or ".git" in parts or (len(parts) > 1 and parts[0] == "app" and parts[1] == "tests"):
                problems.append(f"must not be in the bundle: {name}")
                break
        for name in ROOT_FILES:
            if name in inside:
                text = archive.read(f"{TOP}/{name}")
                if not text.isascii() or b"\n" in text.replace(b"\r\n", b""):
                    problems.append(f"{name}: not ASCII with CRLF line ends")
    return problems


def version_of(app: Path) -> str:
    match = re.search(r'(?m)^version\s*=\s*"([^"]+)"', (app / "pyproject.toml").read_text(encoding="utf-8"))
    if not match:
        raise SystemExit("build-bundle: no version in pyproject.toml")
    return match.group(1)


def build(out: Path, *, source: Path | None, work: Path | None, keep: bool) -> Path:
    if os.name != "nt":
        raise SystemExit("build-bundle: build the Windows bundle on Windows (its Python and wheels are this machine's)")
    work = work or Path(tempfile.mkdtemp(prefix="tow-bundle-", dir=out.resolve()))
    top = work / TOP
    if top.exists():
        remove(top)
    started = time.monotonic()
    stage_code(top / "app", source)
    version = version_of(top / "app")
    say(f"TOW {version} in {top}")
    write_root_files(top, version)
    uv = fetch_uv(top / "runtime" / "bin")
    prepare_runtime(top, uv, work)
    destination = out / ZIP_NAME
    write_zip(top, destination)
    problems = check_zip(destination)
    if problems:
        raise SystemExit("build-bundle: the bundle is incomplete:\n  " + "\n  ".join(problems))
    size = sum(path.stat().st_size for path in top.rglob("*") if path.is_file())
    say(f"{destination}: {destination.stat().st_size / 2**20:.1f} MiB (unpacked {size / 2**20:.1f} MiB)")
    say(f"done in {time.monotonic() - started:.0f} s")
    if not keep:
        remove(work)
    return destination


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Build TOW-windows-x64.zip (run on Windows).")
    parser.add_argument("--out", type=Path, required=True, help="folder for the zip")
    parser.add_argument("--source", type=Path, default=None, help="the source tarball (default: git archive HEAD)")
    parser.add_argument("--work", type=Path, default=None, help="build folder (default: a new one inside --out)")
    parser.add_argument("--keep", action="store_true", help="keep the build folder")
    args = parser.parse_args(argv)
    args.out.mkdir(parents=True, exist_ok=True)
    build(args.out, source=args.source, work=args.work, keep=args.keep)
    return 0


if __name__ == "__main__":
    sys.exit(main())
