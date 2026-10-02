"""TOW — Torrent Watcher."""

from importlib.metadata import PackageNotFoundError, version

try:
    __version__ = version("tow")  # single source of truth: pyproject.toml
except PackageNotFoundError:  # running from a source tree without an install
    __version__ = "0.0.0+unknown"
