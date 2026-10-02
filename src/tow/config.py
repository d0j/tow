from __future__ import annotations

import copy
import re
import threading
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import yaml

from tow.paths import config_path
from tow.store import atomic_write_text


def as_bool(value: object, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, int) and value in (0, 1):
        return bool(value)
    if isinstance(value, str):
        normalized = value.strip().lower()
        if normalized in {"1", "true", "yes", "on"}:
            return True
        if normalized in {"0", "false", "no", "off", ""}:
            return False
    return default


# libyaml's loader is ~8x faster than the pure-Python one; same safe tag set.
_SAFE_LOADER = getattr(yaml, "CSafeLoader", yaml.SafeLoader)
# The config is read several times per request; reparse only when the file changes.
# Writers replace the file atomically, so (mtime, size, file id) changes with every write.
_CACHE_LOCK = threading.Lock()
_cache: tuple[tuple[str, int, int, int], Any] | None = None


def _parsed_config_file(path: Path) -> Any:
    global _cache
    stat = path.stat()
    key = (str(path), stat.st_mtime_ns, stat.st_size, stat.st_ino)
    with _CACHE_LOCK:
        cached = _cache
    if cached is not None and cached[0] == key:
        return copy.deepcopy(cached[1])
    data = yaml.load(path.read_text(encoding="utf-8"), Loader=_SAFE_LOADER)
    with _CACHE_LOCK:
        _cache = (key, data)
    return copy.deepcopy(data)


# Limits the UI, the scheduler and the store share (one place for every bound).
INTERVAL_MIN_MINUTES = 15
INTERVAL_MAX_MINUTES = 1440
FLASH_TTL_MIN_SEC = 15
FLASH_TTL_MAX_SEC = 3600

# What load_config fills in when config.yaml does not say it; save_config leaves these
# out again unless the file had them, so a save never adds lines the owner did not write.
# ``trackers`` too (see defaults()): every site TOW has settings for.
DEFAULTS: dict[str, Any] = {
    "bind": "127.0.0.1",
    "allow_lan": False,
    "port": 8787,
    "interval_sec": 3600,
    "flash_ttl_sec": 60,
    "client": {"kind": "qbittorrent"},
}


def defaults() -> dict[str, Any]:
    """A fresh copy of DEFAULTS plus ``trackers``: a config without a ``trackers:`` section
    watches every site with known settings (``tow.trackers.presets``). Once the owner changes
    the sites, the section is written and lists exactly the sites TOW uses."""
    from tow.trackers.presets import known_sites

    return {**copy.deepcopy(DEFAULTS), "trackers": known_sites()}


# Keys older versions wrote that mean nothing now. ``lan_auth`` switched the network password
# off before 1.13; since then the network always needs the password (owner's decision).
OBSOLETE_KEYS = ("lan_auth",)


def interval_sec_of(cfg: Mapping[str, Any]) -> int:
    """The check interval of a loaded config in seconds (the default when it has none)."""
    return int(cfg.get("interval_sec") or DEFAULTS["interval_sec"])


def port_of(cfg: Mapping[str, Any]) -> int:
    """The web port of a loaded config (the default when it has none)."""
    return int(cfg.get("port") or DEFAULTS["port"])


def interval_minutes(interval_sec: int | None) -> int:
    """The check interval in whole minutes, within what the UI and the scheduler accept."""
    seconds = int(interval_sec or DEFAULTS["interval_sec"])
    return max(INTERVAL_MIN_MINUTES, min(INTERVAL_MAX_MINUTES, round(seconds / 60)))


DEFAULT_BACKUP_TIME = (3, 30)


def parse_backup_time(value: object) -> tuple[int, int]:
    """``backup_time: "03:30"`` -> (3, 30), local time of the night copy (default 03:30).

    YAML reads an unquoted 03:30 as 210 (base 60): that is accepted too.
    """
    if isinstance(value, bool) or value is None or value == "":
        return DEFAULT_BACKUP_TIME
    if isinstance(value, int):
        hours, minutes = divmod(value, 60)
    else:
        try:
            hours_text, minutes_text = str(value).strip().split(":", 1)
            hours, minutes = int(hours_text), int(minutes_text)
        except ValueError:
            raise ValueError(f'backup_time must look like "03:30", got {value!r}') from None
    if not (0 <= hours <= 23 and 0 <= minutes <= 59):
        raise ValueError(f'backup_time must look like "03:30", got {value!r}')
    return hours, minutes


def flash_ttl(value: object) -> int:
    try:
        seconds = int(value or 60)  # type: ignore[call-overload]
    except TypeError, ValueError:
        seconds = 60
    return int(max(FLASH_TTL_MIN_SEC, min(FLASH_TTL_MAX_SEC, seconds)))


def load_config() -> dict[str, Any]:
    path = config_path()
    if not path.is_file():
        raise FileNotFoundError(f"TOW config not found: {path}; create it or set TOW_CONFIG")
    data = _parsed_config_file(path) or {}
    if not isinstance(data, dict):
        raise TypeError("config.yaml must be a mapping")
    return validated(data)


def validated(raw: dict[str, Any]) -> dict[str, Any]:
    """``raw`` (config.yaml as parsed) with the defaults filled in and every value checked.

    The one schema of config.yaml: ``load_config`` and an imported bundle both use it.
    Raises ``ConfigError`` naming the first wrong value; ``raw`` itself is not changed.
    """
    data = copy.deepcopy(raw)  # the parsed file stays as it is on disk (see save_config)
    for key, value in defaults().items():
        data.setdefault(key, value)
    data["allow_lan"] = as_bool(data.get("allow_lan"))
    for key in OBSOLETE_KEYS:  # read from an older file and ignored; the next save leaves them out
        data.pop(key, None)
    _validate(data)
    return data


class ConfigError(ValueError):
    """config.yaml has a value of the wrong type: named here, not as a crash later."""


def _int_field(data: dict[str, Any], key: str, low: int, high: int) -> None:
    value = data.get(key)
    if isinstance(value, str) and value.strip().isdigit():
        value = int(value.strip())
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        raise ConfigError(f"config.yaml: {key} must be a whole number {low}..{high}, got {data.get(key)!r}")
    data[key] = value


def _bool_field(data: dict[str, Any], key: str) -> None:
    value = data.get(key)
    if value is not None and as_bool(value, True) != as_bool(value, False):
        raise ConfigError(f"config.yaml: {key} must be true or false, got {value!r}")


def _text_list_field(data: dict[str, Any], key: str, what: str) -> None:
    value = data.get(key)
    if value is not None and (not isinstance(value, list) or not all(isinstance(item, str) for item in value)):
        raise ConfigError(f"config.yaml: {key} must be a list of {what}")


def _validate(data: dict[str, Any]) -> None:
    _int_field(data, "port", 1, 65535)
    _int_field(data, "interval_sec", 60, 7 * 24 * 3600)
    _int_field(data, "flash_ttl_sec", 1, 24 * 3600)
    if not isinstance(data.get("bind"), str):
        raise ConfigError("config.yaml: bind must be a text address such as 127.0.0.1")
    if data.get("user_agent") is not None and not isinstance(data["user_agent"], str):
        raise ConfigError("config.yaml: user_agent must be text")
    trackers = data.get("trackers")
    if not isinstance(trackers, dict) or not all(isinstance(spec, dict) for spec in trackers.values()):
        raise ConfigError("config.yaml: trackers must map each site name to its settings")
    if not isinstance(data.get("client"), dict):
        raise ConfigError("config.yaml: client must be a mapping (kind, ...)")
    if "clients" in data and not isinstance(data["clients"], (dict, list)):
        raise ConfigError("config.yaml: clients must be a mapping or a list")
    for key in ("backup_dir", "restore_points_dir"):
        if data.get(key) is not None and not isinstance(data[key], str):
            raise ConfigError(f"config.yaml: {key} must be a folder path")
    _text_list_field(data, "allowed_save_roots", "folders")
    for key in ("allow_unc_save_paths", "allow_private_tracker_hosts", "allow_private_notifier_hosts"):
        _bool_field(data, key)
    if data.get("backup_keep") is not None:
        _int_field(data, "backup_keep", 1, 365)
    if data.get("backup_time") is not None:
        try:
            parse_backup_time(data["backup_time"])
        except ValueError as exc:
            raise ConfigError(f"config.yaml: {exc}") from None
    pulse = data.get("heartbeat_url")
    if pulse is not None and not (isinstance(pulse, str) and pulse.startswith("https://")):
        raise ConfigError("config.yaml: heartbeat_url must be an https:// address (e.g. from healthchecks.io)")
    if data.get("daily_digest_hour") is not None:
        _int_field(data, "daily_digest_hour", 0, 23)
    language = data.get("language")
    if language is not None and not (
        isinstance(language, str) and re.fullmatch(r"auto|[a-z]{2,3}(-[a-z0-9]{1,8})*", language, re.IGNORECASE)
    ):
        raise ConfigError('config.yaml: language must be "auto" or a language code such as en or ru')
    quiet = data.get("quiet_hours")
    if quiet is not None and not re.fullmatch(r"\s*\d{1,2}\s*-\s*\d{1,2}\s*", str(quiet)):
        raise ConfigError('config.yaml: quiet_hours must look like "23-8" (hours, local time)')


_HEADER = "# TOW config. Settings changed in the web UI are written here; comments are not kept.\n"


def save_config(data: dict[str, Any]) -> None:
    """Write the config without the defaults load_config added (unless the file had them)."""
    path = config_path()
    path.parent.mkdir(parents=True, exist_ok=True)
    on_disk = _parsed_config_file(path) if path.is_file() else {}
    present = set(on_disk) if isinstance(on_disk, dict) else set()
    default = defaults()
    out = {key: value for key, value in data.items() if key in present or key not in default or value != default[key]}
    if "clients" in out and out.get("client") == DEFAULTS["client"]:
        out.pop("client")  # the single-client block is replaced by the clients list
    text = _HEADER + yaml.safe_dump(out, allow_unicode=True, sort_keys=False, default_flow_style=False)
    atomic_write_text(path, text)


def _set_top_level_int(key: str, value: int) -> None:
    """Set one top-level scalar in place, keeping the owner's comments and layout.

    The edit stays on the key's own line (``[ \\t]*`` cannot cross a newline, so an empty
    value never swallows the next key) and is verified by re-parsing: unless exactly
    ``key`` changed, the whole config is rewritten from the parsed data instead.
    """
    path = config_path()
    text = path.read_text(encoding="utf-8")
    line = f"{key}: {value}"
    new, count = re.subn(rf"(?m)^{re.escape(key)}:[ \t]*[^\s#]*", line, text, count=1)
    if not count:
        new = text.rstrip() + f"\n{line}\n"
    try:
        before = yaml.safe_load(text) or {}
        after = yaml.safe_load(new) or {}
    except yaml.YAMLError:
        before = after = None
    if isinstance(before, dict) and after == {**before, key: value}:
        atomic_write_text(path, new)
        return
    # Raw (unvalidated) data: the broken value being replaced must not block its own fix.
    data = _parsed_config_file(path)
    if not isinstance(data, dict):
        raise ConfigError("config.yaml must be a mapping")
    data[key] = value
    save_config(data)


def set_interval_sec(sec: int) -> None:
    _set_top_level_int("interval_sec", int(sec))


def set_flash_ttl_sec(sec: int) -> None:
    _set_top_level_int("flash_ttl_sec", flash_ttl(sec))
