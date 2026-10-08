"""Every location of a TOW install, derived from one folder: the root.

TOW is portable: everything it writes lives inside the root (docs/PORTABLE.md)::

    <root>/app/           the code (a git clone at a release tag)
    <root>/config.yaml    live configuration
    <root>/data/          state, history, secrets.enc, logs/, tmp/, run/, browser profiles
    <root>/keys/          the master key (outside data/: copies of the data never carry it)
    <root>/backup/        night copies, pre-update snapshots
    <root>/runtime/       the Python uv installed for TOW, uv's cache

The root is ``TOW_ROOT`` when set; else the parent of the code folder when it is an ``app``
folder next to ``config.yaml`` or ``data/`` (a runtime install); else the code checkout itself
(development: its ``config.yaml``, ``data/`` and ``keys/`` are gitignored). ``TOW_HOME`` and
``TOW_CONFIG`` stay explicit overrides of the data folder and the config file.
"""

from __future__ import annotations

import contextlib
import os
import shutil
import tempfile
import time
from pathlib import Path

_ROOT_ENV = "TOW_ROOT"
_RUNTIME_HOME_ENV = "TOW_HOME"
_LEGACY_RUNTIME_HOME_ENV = "TOPIC_WATCH_HOME"
_CONFIG_ENV = "TOW_CONFIG"
# Child processes (and libraries that read the environment) use the same temp folder.
_TEMP_ENV = ("TMP", "TEMP", "TMPDIR")
TEMP_MAX_AGE_SEC = 24 * 3600


def repo_root() -> Path:
    """The code folder: ``<root>/app`` in a runtime install, the checkout in development."""
    return Path(__file__).resolve().parents[2]


def _absolute(value: str) -> Path:
    return Path(os.path.abspath(os.path.expanduser(value)))


def _env_path(*names: str) -> Path | None:
    for name in names:
        value = os.environ.get(name, "").strip()
        if value:
            return _absolute(value)
    return None


def _runtime_parent(code: Path) -> Path | None:
    """``<root>`` when ``code`` is ``<root>/app`` of a runtime install, else None."""
    if code.name.casefold() != "app":
        return None
    parent = code.parent
    if (parent / "config.yaml").is_file() or (parent / "data").is_dir():
        return parent
    return None


def _is_checkout(code: Path) -> bool:
    return (code / "pyproject.toml").is_file() and (code / "src" / "tow").is_dir()


def root() -> Path:
    """The install root (see the module docstring)."""
    code = repo_root()
    given = _env_path(_ROOT_ENV)
    if given is not None:
        # Older launchers set TOW_ROOT to the code folder; the install is its parent then.
        if os.path.normcase(str(given)) == os.path.normcase(str(code)):
            return _runtime_parent(code) or given
        return given
    runtime = _runtime_parent(code)
    if runtime is not None:
        return runtime
    if _is_checkout(code):
        return code
    # An installed package (a wheel) has no layout of its own: the explicit locations say where.
    explicit = _env_path(_CONFIG_ENV, _RUNTIME_HOME_ENV, _LEGACY_RUNTIME_HOME_ENV)
    if explicit is not None:
        return explicit.parent
    raise RuntimeError("TOW_ROOT must be configured for a portable runtime")


def _same_folder(first: Path, second: Path) -> bool:
    try:
        return os.path.samefile(first, second)
    except OSError:  # one of them is missing: compare the names
        return os.path.normcase(os.path.realpath(first)) == os.path.normcase(os.path.realpath(second))


def foreign_code() -> Path | None:
    """The install root when this process runs another folder's code for it, else None.

    A copy of an install keeps ``app/.venv`` with absolute paths: while the original folder is
    still there, the copy's environment imports the original's ``app/src``. Started with the
    copy as its root it would read and write the copy's data with the original's code - and
    take the original's web server for its own. An install's code is ``<root>/app``; a
    development checkout is its own root, and a root without ``app/`` (tests, a wheel) has no
    code of its own to compare with.
    """
    code = repo_root()
    try:
        install = root()
    except RuntimeError:
        return None
    if _same_folder(code, install) or _same_folder(code.parent, install):
        return None
    return install if (install / "app").is_dir() else None


def data_dir(*, create: bool = True) -> Path:
    p = _env_path(_RUNTIME_HOME_ENV, _LEGACY_RUNTIME_HOME_ENV) or root() / "data"
    if create:
        p.mkdir(parents=True, exist_ok=True)
    return p


def config_path() -> Path:
    value = _env_path(_CONFIG_ENV)
    if value is not None:
        return value
    bundled = root() / "config.yaml"
    if bundled.is_file():
        return bundled
    raise RuntimeError("TOW_CONFIG must be configured for portable runtime")


def keys_dir() -> Path:
    """``<root>/keys``: the master key, outside data/ (not created here)."""
    return root() / "keys"


def key_file() -> Path:
    """The install's own master key file (TOW_MASTER_KEY / TOW_MASTER_KEY_FILE win over it)."""
    return keys_dir() / "master.key"


def explicit_key_file() -> Path | None:
    """``TOW_MASTER_KEY_FILE``: an absolute path as given, a relative one inside the data folder;
    None when it is not set. Raises ValueError for a relative path that leads out of data/."""
    value = os.environ.get("TOW_MASTER_KEY_FILE", "").strip()
    if not value:
        return None
    path = Path(value)
    if path.is_absolute():
        return path
    if path.anchor:  # C:key or \key: relative to something other than data/
        raise ValueError("TOW_MASTER_KEY_FILE leads out of the data folder")
    try:
        base = data_dir().resolve()
        resolved = (base / path).resolve()
    except (OSError, RuntimeError) as exc:
        raise ValueError("TOW_MASTER_KEY_FILE cannot be resolved") from exc
    if not resolved.is_relative_to(base):
        raise ValueError("TOW_MASTER_KEY_FILE leads out of the data folder")
    return resolved


def legacy_key_file() -> Path:
    """``<data>/master.key``: where installs before 1.18 kept the master key. Used only while
    ``key_file()`` does not exist (tow.store); ``tow keys adopt`` moves it into keys/."""
    return data_dir() / "master.key"


def user_home() -> Path:
    """The owner's home folder. TOW writes there only the autostart entry (systemd user unit,
    LaunchAgent), and only on request; everything else stays inside the root."""
    return Path.home()


def launcher(*, windows: bool) -> str:
    """The launcher as typed in the install folder: ``app\\scripts\\tow.cmd`` (``app/scripts/tow``)
    in a runtime install, ``scripts\\tow.cmd`` in a development checkout. Relative, so a page
    shown on the network does not reveal where the install lives."""
    try:
        parts = repo_root().relative_to(root()).parts
    except RuntimeError, ValueError:
        parts = ()
    return ("\\" if windows else "/").join((*parts, "scripts", "tow.cmd" if windows else "tow"))


def lan_auth_token_file() -> Path:
    """``<data>/lan-auth.token``: the optional external access key (tow.auth reads it when
    neither TOW_LAN_AUTH_TOKEN nor TOW_LAN_AUTH_TOKEN_FILE is set)."""
    return data_dir() / "lan-auth.token"


def _subdir(name: str) -> Path:
    p = data_dir() / name
    p.mkdir(parents=True, exist_ok=True)
    return p


def tmp_dir(*, create: bool = True) -> Path:
    """``<data>/tmp``: TOW's temporary files (never the system temp folder)."""
    if not create:
        return data_dir(create=False) / "tmp"
    return _subdir("tmp")


def logs_dir() -> Path:
    return _subdir("logs")


def run_dir() -> Path:
    """``<data>/run``: pid and lock files of running TOW processes, control files."""
    return _subdir("run")


def backup_root() -> Path:
    """``<root>/backup``: night copies and pre-update snapshots (not created here)."""
    return root() / "backup"


def runtime_dir() -> Path:
    """``<root>/runtime``: the Python uv installed for TOW and uv's cache (not created here)."""
    return root() / "runtime"


def _sweep(folder: Path, *, older_than: float) -> None:
    """Remove what a crashed process left in the temp folder; anything in use is skipped."""
    with contextlib.suppress(OSError):
        for entry in folder.iterdir():
            try:
                if entry.lstat().st_mtime >= older_than:
                    continue
                if entry.is_dir() and not entry.is_symlink():
                    shutil.rmtree(entry, ignore_errors=True)
                else:
                    entry.unlink()
            except OSError:
                continue


def use_private_temp(*, max_age_sec: float = TEMP_MAX_AGE_SEC) -> Path | None:
    """Point ``tempfile`` and the TMP/TEMP/TMPDIR of child processes at ``<data>/tmp``.

    Called once at process start (the CLI and the web app). Entries older than a day are
    removed. Without a usable data folder nothing changes (the command reports the problem).
    """
    try:
        folder = tmp_dir()
    except OSError, RuntimeError:
        return None
    _sweep(folder, older_than=time.time() - max_age_sec)
    tempfile.tempdir = str(folder)
    for name in _TEMP_ENV:
        os.environ[name] = str(folder)
    return folder


def secrets_path() -> Path:
    return data_dir() / "secrets.json"


def state_path() -> Path:
    return data_dir() / "state.json"


def download_history_path() -> Path:
    return data_dir() / "download_history.json"
