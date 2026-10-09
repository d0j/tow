from __future__ import annotations

import copy
import os
import re
import threading
from collections.abc import Mapping
from pathlib import Path
from typing import Any

import yaml

from tow.errors import TowError
from tow.paths import config_path
from tow.store import atomic_write_text
from tow.yaml_guard import SAFE_LOADER, YamlLimitError, validate_graph
from tow.yaml_guard import dump as dump_yaml
from tow.yaml_guard import load as load_yaml
from tow.yaml_guard import read_text as read_yaml_text


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
_SAFE_LOADER = SAFE_LOADER
# The config is read several times per request; reparse only when the file changes.
# Writers replace the file atomically, so (mtime, size, file id) changes with every write.
_CACHE_LOCK = threading.Lock()
_cache: tuple[tuple[str, int, int, int], Any] | None = None
# load_config's checked result for the same file version: checking every site (204 sites:
# ~25 ms) on every request is the same work again. Callers get their own deep copy.
_validated_cache: tuple[tuple[str, int, int, int], dict[str, Any]] | None = None


def _file_key(path: Path) -> tuple[str, int, int, int]:
    stat = path.stat()
    return (str(path), stat.st_mtime_ns, stat.st_size, stat.st_ino)


def _config_text(path: Path) -> str:
    """config.yaml as text: a file saved in another encoding (Notepad's "ANSI") is named so."""
    try:
        return read_yaml_text(path)
    except UnicodeDecodeError as exc:
        raise ConfigError("config_error.encoding") from exc


def _parsed_config_file(path: Path) -> Any:
    return _parsed_config_version(path)[1]


def _parsed_config_version(path: Path) -> tuple[tuple[str, int, int, int], Any]:
    """(the file version read, a fresh copy of what it parses to)."""
    global _cache
    key = _file_key(path)
    with _CACHE_LOCK:
        cached = _cache
    if cached is not None and cached[0] == key:
        return key, copy.deepcopy(cached[1])
    data = load_yaml(_config_text(path), loader=_SAFE_LOADER)
    with _CACHE_LOCK:
        _cache = (key, data)
    return key, copy.deepcopy(data)


# Limits the UI, the scheduler and the store share (one place for every bound).
INTERVAL_MIN_MINUTES = 15
INTERVAL_MAX_MINUTES = 1440
FLASH_TTL_MIN_SEC = 15
FLASH_TTL_MAX_SEC = 3600
HISTORY_MAX_DAYS = 36500
HISTORY_MAX_ITEMS = 1_000_000
# Settings → Theme (``theme:``): the pages' colours; "auto" (or no key) follows the system.
THEMES = ("auto", "light", "dark")

# What load_config fills in when config.yaml does not say it; save_config leaves these
# out again unless the file had them, so a save never adds lines the owner did not write.
# ``trackers`` too (see defaults()): every site TOW has settings for.
DEFAULTS: dict[str, Any] = {
    "bind": "127.0.0.1",
    "allow_lan": False,
    "check_updates": True,
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
            raise ValueError('backup_time must look like "03:30"') from None
    if not (0 <= hours <= 23 and 0 <= minutes <= 59):
        raise ValueError('backup_time must look like "03:30"')
    return hours, minutes


def flash_ttl(value: object) -> int:
    try:
        seconds = int(value or 60)  # type: ignore[call-overload]
    except TypeError, ValueError:
        seconds = 60
    return int(max(FLASH_TTL_MIN_SEC, min(FLASH_TTL_MAX_SEC, seconds)))


def load_config() -> dict[str, Any]:
    global _validated_cache
    path = config_path()
    if not path.is_file():
        raise FileNotFoundError(f"TOW config not found: {path}; create it or set TOW_CONFIG")
    with _CACHE_LOCK:
        known = _validated_cache
    if known is not None and known[0] == _file_key(path):
        return copy.deepcopy(known[1])
    try:
        key, data = _parsed_config_version(path)
    except YamlLimitError:
        raise
    except yaml.YAMLError as exc:
        mark = getattr(exc, "problem_mark", None)
        line, column = (mark.line + 1, mark.column + 1) if mark is not None else (0, 0)
        raise ConfigError("config_error.syntax", line=line, column=column) from exc
    if data is None:
        data = {}
    if not isinstance(data, dict):
        raise ConfigError("config_error.mapping")
    result = validated(data)  # a wrong value raises every time: only a checked config is kept
    with _CACHE_LOCK:
        _validated_cache = (key, result)
    return copy.deepcopy(result)


def validated(raw: dict[str, Any]) -> dict[str, Any]:
    """``raw`` (config.yaml as parsed) with the defaults filled in and every value checked.

    The one schema of config.yaml: ``load_config`` and an imported bundle both use it.
    Raises ``ConfigError`` naming the first wrong value; ``raw`` itself is not changed.
    """
    validate_graph(raw)
    data = copy.deepcopy(raw)  # the parsed file stays as it is on disk (see save_config)
    for key, value in defaults().items():
        data.setdefault(key, value)
    data["allow_lan"] = as_bool(data.get("allow_lan"))
    _bool_field(data, "check_updates")
    data["check_updates"] = as_bool(data.get("check_updates"))
    for key in OBSOLETE_KEYS:  # read from an older file and ignored; the next save leaves them out
        data.pop(key, None)
    _validate(data)
    return data


class ConfigError(TowError, ValueError):
    """config.yaml has a value of the wrong type: named here (``config_error.*`` and the
    setting), not as a crash later; ``str()`` is the text in the current language."""


# What tow.i18n says, once per process, when config.yaml cannot give it the language.
LANGUAGE_UNREAD = "config_error.language_unread"


_WHOLE_NUMBER = re.compile(r"[+-]?[0-9]{1,18}")


def whole_number(text: object) -> int | None:
    """A whole number written as text (config.yaml, a form field), or None.

    ASCII digits with an optional sign, and few of them (spaces around are fine): int() alone
    also takes "٣٠", "１５" and "1_000", str.isdigit() takes "²", and int() refuses 4300+ digits."""
    if not isinstance(text, str) or not _WHOLE_NUMBER.fullmatch(text.strip()):
        return None
    return int(text.strip())


def _int_field(data: dict[str, Any], key: str, low: int, high: int) -> None:
    value = data.get(key)
    if isinstance(value, str) and (number := whole_number(value)) is not None:
        value = number
    if isinstance(value, bool) or not isinstance(value, int) or not low <= value <= high:
        raise ConfigError("config_error.whole_number", key=key, low=low, high=high)
    data[key] = value


def _bool_field(data: dict[str, Any], key: str) -> None:
    value = data.get(key)
    if value is not None and as_bool(value, True) != as_bool(value, False):
        raise ConfigError("config_error.true_false", key=key)


def _folder_list_field(data: dict[str, Any], key: str) -> None:
    value = data.get(key)
    if value is not None and (not isinstance(value, list) or not all(isinstance(item, str) for item in value)):
        raise ConfigError("config_error.folder_list", key=key)


def web_address(value: object) -> bool:
    """An http:// or https:// address with a host: what a site, a mirror or a topic link is.
    Anything else (javascript:, file:, a bare word, "--switch") is never opened or linked."""
    from tow.mirrors import origin_key

    return isinstance(value, str) and origin_key(value) is not None


# A site name or a topic id is part of page addresses (/sites/<name>/delete): an imported one
# must be a plain id. config.yaml keeps loading older names (YAML scalars such as 123, markup
# shown as text) as long as they cannot move such an address elsewhere ("../topics/1").
SAFE_ID = re.compile(r"[A-Za-z0-9_-]{1,64}")


def path_safe_name(name: object) -> bool:
    text = str(name)
    return text not in {".", ".."} and "/" not in text and "\\" not in text


# The type of each setting of a site TOW reads (tow.trackers.generic); empty (null) means "not set".
_SITE_TEXT = ("title", "url_regex", "download_href_regex", "topic_path", "download_path", "login_path", "search_path")
_SITE_FLAGS = ("browser_auth", "page_download")
_SITE_NUMBERS = ("fail_threshold", "cooldown_sec")
_LOGIN_FORM_KEYS = {"user_field", "pw_field", "extra"}


def _texts(value: object) -> bool:
    return isinstance(value, list) and all(isinstance(item, str) for item in value)


def _login_form(value: object) -> bool:
    if value is None:
        return True
    if not isinstance(value, dict) or set(value) - _LOGIN_FORM_KEYS:
        return False
    extra = value.get("extra")
    return all(value.get(key) is None or isinstance(value[key], str) for key in ("user_field", "pw_field")) and (
        extra is None
        or (isinstance(extra, dict) and all(isinstance(k, str) and isinstance(v, str) for k, v in extra.items()))
    )


def _as_text(value: object) -> str | None:
    """A site text: a number written without quotes (``title: 2024``) is that text."""
    if isinstance(value, str):
        return value
    return str(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def _as_flag(value: object) -> bool | None:
    """true/false, also as 1/0 or a quoted 'true', 'yes', 'off'... (what older versions read)."""
    flag = as_bool(value, True)
    return flag if flag == as_bool(value, False) else None


def _as_whole(value: object) -> int | None:
    """A whole number, also as 1800.0 or a quoted '1800' (what older versions read with int())."""
    if isinstance(value, bool):
        return None
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, str):
        try:
            return int(value.strip())
        except ValueError:
            return None
    return value if isinstance(value, int) else None


def _site_spec(name: object, spec: dict[str, Any]) -> None:
    """Check a site's settings and bring each one to the type the site code reads.

    Older versions read them loosely (``int(...)``, truthiness), so a hand-written '1800', 1800.0,
    'true', 1 or an unquoted number as a title kept working: they are taken as what they mean.
    Only a value that can never work (a list for a number, a word for a number) is refused."""
    if not path_safe_name(name):
        raise ConfigError("config_error.site_name")
    site = str(name)
    for key in ("fetch_hosts", "login_hosts"):
        hosts = spec.get(key)
        if isinstance(hosts, str) and web_address(hosts):
            spec[key] = hosts = [hosts]  # one address written without the list brackets
        if hosts is not None and (not isinstance(hosts, list) or not all(web_address(host) for host in hosts)):
            raise ConfigError("config_error.site_hosts", site=site, key=key)
    for keys, convert, code in (
        (_SITE_TEXT, _as_text, "config_error.site_text"),
        (_SITE_FLAGS, _as_flag, "config_error.site_true_false"),
        (_SITE_NUMBERS, _as_whole, "config_error.site_number"),
    ):
        for key in keys:
            if spec.get(key) is None:
                continue
            value = convert(spec[key])
            if value is None:
                raise ConfigError(code, site=site, key=key)
            spec[key] = value
    if spec.get("cookie_names") is not None and not _texts(spec["cookie_names"]):
        raise ConfigError("config_error.site_text_list", site=site, key="cookie_names")
    if not _login_form(spec.get("login_form")):
        raise ConfigError("config_error.site_login_form", site=site)


def _validate(data: dict[str, Any]) -> None:
    _int_field(data, "port", 1, 65535)
    _int_field(data, "interval_sec", 60, 7 * 24 * 3600)
    _int_field(data, "flash_ttl_sec", 1, 24 * 3600)
    if not isinstance(data.get("bind"), str):
        raise ConfigError("config_error.bind")
    if data.get("user_agent") is not None and not isinstance(data["user_agent"], str):
        raise ConfigError("config_error.user_agent")
    trackers = data.get("trackers")
    if not isinstance(trackers, dict) or not all(isinstance(spec, dict) for spec in trackers.values()):
        raise ConfigError("config_error.trackers")
    for name, spec in trackers.items():
        _site_spec(name, spec)
    if not isinstance(data.get("client"), dict):
        raise ConfigError("config_error.client")
    if "clients" in data and not isinstance(data["clients"], (dict, list)):
        raise ConfigError("config_error.clients")
    for key in ("backup_dir", "restore_points_dir"):
        if data.get(key) is not None and not isinstance(data[key], str):
            raise ConfigError("config_error.folder", key=key)
    _folder_list_field(data, "allowed_save_roots")
    for key in ("allow_unc_save_paths", "allow_private_tracker_hosts", "allow_private_notifier_hosts"):
        _bool_field(data, key)
    if data.get("backup_keep") is not None:
        _int_field(data, "backup_keep", 1, 365)
    _bool_field(data, "backup_enabled")
    for key, lower, upper in (
        ("backup_days", 1, 3650),
        ("backup_max_mib", 0, 1048576),
        # Download history (tow.download_history): 0 = no limit. A wrong value was the default
        # without a word, and a huge number of days made every check fail on the date.
        ("history_keep_days", 0, HISTORY_MAX_DAYS),
        ("history_max_items", 0, HISTORY_MAX_ITEMS),
    ):
        if data.get(key) is not None:
            _int_field(data, key, lower, upper)
    if data.get("backup_time") is not None:
        try:
            parse_backup_time(data["backup_time"])
        except ValueError:
            raise ConfigError("config_error.backup_time") from None
    pulse = data.get("heartbeat_url")
    # "https://" alone, or with a space for a host, was taken and then failed every ping.
    if pulse is not None and not (
        isinstance(pulse, str) and pulse.startswith("https://") and web_address(pulse) and not re.search(r"\s", pulse)
    ):
        raise ConfigError("config_error.heartbeat_url")
    if data.get("daily_digest_hour") is not None:
        _int_field(data, "daily_digest_hour", 0, 23)
    _validate_look(data)
    quiet = data.get("quiet_hours")
    span = re.fullmatch(r"\s*(\d{1,2})\s*-\s*(\d{1,2})\s*", str(quiet))
    if quiet is not None and not span:
        raise ConfigError("config_error.quiet_hours")
    if span and not (int(span[1]) <= 23 and int(span[2]) <= 24 and int(span[1]) % 24 != int(span[2]) % 24):
        # "0-24" or "8-8" would be read as no quiet hours at all.
        raise ConfigError("config_error.quiet_hours_span", value=str(quiet).strip())


def _validate_look(data: dict[str, Any]) -> None:
    """How the pages look: ``language`` (auto or a language code) and ``theme`` (THEMES)."""
    language = data.get("language")
    if language is not None and not (
        isinstance(language, str) and re.fullmatch(r"auto|[a-z]{2,3}(-[a-z0-9]{1,8})*", language, re.IGNORECASE)
    ):
        raise ConfigError("config_error.language")
    if data.get("theme") is not None and data["theme"] not in THEMES:
        raise ConfigError("config_error.theme")


def _header(path: Path) -> str:
    """The comment a saved config starts with: the UI writes no comments, so it names the
    commented reference that ships with the code (``app/config.example.yaml`` in an install)."""
    from tow.paths import repo_root

    reference = repo_root() / "config.example.yaml"
    try:
        shown = Path(os.path.relpath(reference, path.parent)).as_posix()
    except ValueError:  # another drive (TOW_CONFIG elsewhere): the full path
        shown = str(reference)
    return (
        "# TOW config. Settings changed in the web UI are written here, without comments.\n"
        f"# Every setting, explained and with its default: {shown}\n"
    )


def save_config(data: dict[str, Any]) -> None:
    """Write the config without the defaults load_config added (unless the file had them)."""
    validate_graph(data)
    path = config_path()
    on_disk = _parsed_config_file(path) if path.is_file() else {}
    present = set(on_disk) if isinstance(on_disk, dict) else set()
    default = defaults()
    out = {key: value for key, value in data.items() if key in present or key not in default or value != default[key]}
    if "clients" in out and out.get("client") == DEFAULTS["client"]:
        out.pop("client")  # the single-client block is replaced by the clients list
    text = dump_yaml(out, prefix=_header(path))
    path.parent.mkdir(parents=True, exist_ok=True)
    atomic_write_text(path, text)


def _set_top_level_int(key: str, value: int) -> None:
    """Set one top-level scalar in place, keeping the owner's comments and layout.

    The edit stays on the key's own line (``[ \\t]*`` cannot cross a newline, so an empty
    value never swallows the next key) and is verified by re-parsing: unless exactly
    ``key`` changed, the whole config is rewritten from the parsed data instead.
    """
    path = config_path()
    text = _config_text(path)
    line = f"{key}: {value}"
    new, count = re.subn(rf"(?m)^{re.escape(key)}:[ \t]*[^\s#]*", line, text, count=1)
    if not count:
        new = text.rstrip() + f"\n{line}\n"
    try:
        before = load_yaml(text) or {}
        after = load_yaml(new) or {}
    except yaml.YAMLError:
        before = after = None
    if isinstance(before, dict) and after == {**before, key: value}:
        atomic_write_text(path, new)
        return
    # Raw (unvalidated) data: the broken value being replaced must not block its own fix.
    data = _parsed_config_file(path)
    if not isinstance(data, dict):
        raise ConfigError("config_error.mapping")
    data[key] = value
    save_config(data)


def set_interval_sec(sec: int) -> None:
    _set_top_level_int("interval_sec", int(sec))


def set_flash_ttl_sec(sec: int) -> None:
    _set_top_level_int("flash_ttl_sec", flash_ttl(sec))
