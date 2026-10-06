"""Languages: one catalog file per language, English by default, the browser's language on request.

- ``src/tow/locales/<code>.json`` holds every text of one language as nested sections
  (``{"home": {"add": {"title": "..."}}}`` -> key ``home.add.title``) plus ``_meta``:
  ``{"name": "Russian", "native": "Русский", "plural": "east_slavic"}`` and, optionally, how the
  language writes a date (``"datetime": "%d.%m.%Y %H:%M:%S"``, short: ``"datetime_short": "%d.%m %H:%M"``)
  and a decimal point (``"decimal": ","``).
- ``en.json`` is the reference and the fallback: a key missing in another language shows the
  English text, a key missing everywhere shows the key itself (and the tests catch it).
- Adding a language = copying ``en.json`` to ``<code>.json`` and translating it; nothing else.
  A regional variant is ``pt-BR.json`` (any case: ``pt-br`` in config.yaml finds it).
- A damaged language file never breaks TOW: it is skipped with a warning (English: the keys show).
- Code and templates never hold user-facing text, only keys: ``t("home.add.title")``,
  ``t("settings.clients.count", n=3)`` for plurals, ``t("x.y", name=title)`` for values.
- A plugin (a messenger, a torrent client) keeps its texts under its own section
  (``notifier.whatsapp.*``, ``client.deluge.*``): its code and its wording change apart.

The UI language is ``language`` in config.yaml: ``auto`` (default: the browser's
``Accept-Language``) or a code. Messages to messengers use the explicit language, or the one
the owner's browser last asked for.
"""

from __future__ import annotations

import functools
import json
import logging
import math
import re
from collections.abc import Callable
from contextvars import ContextVar
from datetime import datetime
from pathlib import Path
from typing import Any

DEFAULT = "en"
AUTO = "auto"
LOCALES_DIR = Path(__file__).parent / "locales"
_CURRENT: ContextVar[str | None] = ContextVar("tow_language", default=None)
# A language tag (BCP 47 shape): "en", "pt-BR", "zh-Hant-TW", "sr-Latn".
_CODE = re.compile(r"^[A-Za-z]{2,3}(?:-[A-Za-z0-9]{1,8})*$")
_LOG = logging.getLogger("tow.i18n")

# How a language writes a date and a decimal number when its ``_meta`` does not say.
DATETIME_FORMAT = "%Y-%m-%d %H:%M:%S"
DATETIME_SHORT_FORMAT = "%Y-%m-%d %H:%M"
DECIMAL_POINT = "."
_DATE_FIELDS = ("datetime", "datetime_short")

# --- catalogs ---------------------------------------------------------------------------------

_PLURAL_FORMS = frozenset({"zero", "one", "two", "few", "many", "other"})


def _is_plural(value: dict[str, Any]) -> bool:
    return bool(value) and set(value) <= _PLURAL_FORMS and "other" in value


def _flatten(tree: dict[str, Any], prefix: str = "", *, source: str = "") -> dict[str, Any]:
    """Nested sections -> dotted keys. A value that is neither a text nor a plural is dropped
    (with a warning): one wrong entry costs that entry, not the language."""
    out: dict[str, Any] = {}
    for key, value in tree.items():
        if key.startswith("_"):
            continue
        name = f"{prefix}{key}"
        if isinstance(value, str):
            out[name] = value
        elif isinstance(value, dict) and _is_plural(value):
            if all(isinstance(form, str) for form in value.values()):
                out[name] = dict(value)
            else:
                _LOG.warning("language file %s: %s has a plural form that is not a text; skipped", source, name)
        elif isinstance(value, dict):
            out.update(_flatten(value, name + ".", source=source))
        else:
            _LOG.warning("language file %s: %s is not a text; skipped", source, name)
    return out


def meta_issues(meta: Any) -> tuple[str | None, list[str]]:
    """(why the language cannot be used, what is wrong but has a fallback) of a ``_meta`` block.

    No ``_meta`` object or no ``name``: the file is skipped. A missing ``native`` falls back to
    ``name``, a missing or unknown ``plural`` to ``one_other``, a bad ``datetime``/``decimal`` to
    the default - each with a warning.
    """
    if not isinstance(meta, dict):
        return "_meta must be an object with name, native and plural", []
    if not isinstance(meta.get("name"), str) or not meta["name"].strip():
        return "_meta.name is missing", []
    issues = []
    if not isinstance(meta.get("native"), str) or not meta["native"].strip():
        issues.append("_meta.native is missing (the name is shown)")
    if meta.get("plural") not in PLURAL_RULES:
        issues.append(f"_meta.plural must be one of {', '.join(sorted(PLURAL_RULES))} (one_other is used)")
    issues.extend(
        f"_meta.{field} must be a text (the default is used)"
        for field in (*_DATE_FIELDS, "decimal")
        if field in meta and not (isinstance(meta[field], str) and meta[field])
    )
    issues.extend(
        f"_meta.{field} may use only %d %m %Y %y %H %I %M %S %p and %% (the default is used)"
        for field in _DATE_FIELDS
        if isinstance(meta.get(field), str) and meta[field] and not _datetime_pattern_ok(meta[field])
    )
    return None, issues


_DATETIME_DIRECTIVE = re.compile(r"%(.)")
_DATETIME_DIRECTIVES = frozenset("dmYyHIMSp%")


def _datetime_pattern_ok(pattern: str) -> bool:
    """Digits only (no month or day names: strftime would write them in the machine's language)."""
    return bool(pattern) and set(_DATETIME_DIRECTIVE.findall(pattern)) <= _DATETIME_DIRECTIVES


def _usable_meta(meta: dict[str, Any]) -> dict[str, Any]:
    out = {"name": meta["name"].strip()}
    native = meta.get("native")
    out["native"] = native.strip() if isinstance(native, str) and native.strip() else out["name"]
    out["plural"] = meta["plural"] if meta.get("plural") in PLURAL_RULES else "one_other"
    for field in _DATE_FIELDS:
        if isinstance(meta.get(field), str) and _datetime_pattern_ok(meta[field]):
            out[field] = meta[field]
    if isinstance(meta.get("decimal"), str) and meta["decimal"]:
        out["decimal"] = meta["decimal"]
    return out


@functools.cache
def _files() -> dict[str, Path]:
    """Every language file by its code in lower case (``pt-br`` -> ``pt-BR.json``)."""
    found: dict[str, Path] = {}
    try:
        paths = sorted(LOCALES_DIR.glob("*.json"))
    except OSError:
        paths = []
    for path in paths:
        if _CODE.match(path.stem):
            found.setdefault(path.stem.lower(), path)
    return found


@functools.cache
def _read(code: str) -> tuple[dict[str, Any], dict[str, Any], bool]:
    """(flat texts, meta, usable) of one language file; a damaged file is (``{}``, ``{}``, False)."""
    path = _files().get(code.lower())
    if path is None:
        return {}, {}, False
    try:
        tree = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, UnicodeError, ValueError) as exc:
        _LOG.warning("language file %s cannot be read, skipped: %s", path.name, type(exc).__name__)
        return {}, {}, False
    if not isinstance(tree, dict):
        _LOG.warning("language file %s is not a JSON object, skipped", path.name)
        return {}, {}, False
    problem, issues = meta_issues(tree.get("_meta"))
    if problem:
        _LOG.warning("language file %s skipped: %s", path.name, problem)
        return {}, {}, False
    for issue in issues:
        _LOG.warning("language file %s: %s", path.name, issue)
    return _flatten(tree, source=path.name), _usable_meta(tree["_meta"]), True


def _load(code: str) -> tuple[dict[str, Any], dict[str, Any]]:
    """(flat texts, meta) of one language: everything of a language lives in one file."""
    texts, meta, _usable = _read(canonical(code) or code)
    return texts, meta


_VERSION = [0]


def version() -> int:
    """Changes with every ``reload()``: part of the key of caches built from catalog texts."""
    return _VERSION[0]


def reload() -> None:
    """Forget loaded catalogs (tests, or after editing a language file)."""
    _VERSION[0] += 1
    _files.cache_clear()
    _read.cache_clear()
    available.cache_clear()
    _lookup.cache_clear()
    _prefixed.cache_clear()


@functools.cache
def available() -> tuple[tuple[str, str, str], ...]:
    """(code, English name, native name) of every usable language, English first.

    English is always there: with its file damaged, TOW shows the keys rather than nothing.
    """
    found = []
    for path in _files().values():
        _texts, meta, usable = _read(path.stem.lower())
        if usable:
            found.append((path.stem, str(meta["name"]), str(meta["native"])))
    if not any(code.lower() == DEFAULT for code, _name, _native in found):
        found.append((DEFAULT, "English", "English"))
    return tuple(sorted(found, key=lambda item: (item[0].lower() != DEFAULT, item[2].casefold())))


def codes() -> list[str]:
    return [code for code, _name, _native in available()]


@functools.cache
def _lookup() -> dict[str, str]:
    return {code.lower(): code for code in codes()}


def canonical(code: str | None) -> str | None:
    """The code as its file spells it (``PT-br`` -> ``pt-BR``), or None when there is no such language."""
    return _lookup().get(str(code or "").strip().lower())


# --- plural rules (CLDR) ----------------------------------------------------------------------


def _east_slavic(n: int) -> str:  # ru, uk, be
    if n % 10 == 1 and n % 100 != 11:
        return "one"
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return "few"
    return "many"


def _west_slavic(n: int) -> str:  # cs, sk
    return "one" if n == 1 else "few" if 2 <= n <= 4 else "other"


def _polish(n: int) -> str:
    if n == 1:
        return "one"
    if 2 <= n % 10 <= 4 and not 12 <= n % 100 <= 14:
        return "few"
    return "many"


PLURAL_RULES: dict[str, Callable[[int], str]] = {
    "one_other": lambda n: "one" if n == 1 else "other",  # en, de, es, it, nl, sv, ...
    "french": lambda n: "one" if n in (0, 1) else "other",  # fr, pt-BR
    "east_slavic": _east_slavic,
    "west_slavic": _west_slavic,
    "polish": _polish,
    "none": lambda _n: "other",  # zh, ja, ko, tr, ...
}
# The forms a plural value must have under each rule (the tests hold every catalog to it).
PLURAL_FORMS = {
    "one_other": ("one", "other"),
    "french": ("one", "other"),
    "east_slavic": ("one", "few", "many", "other"),
    "west_slavic": ("one", "few", "other"),
    "polish": ("one", "few", "many", "other"),
    "none": ("other",),
}


def plural_rule(code: str) -> str:
    _texts, meta = _load(code)
    rule = str(meta.get("plural") or "one_other")
    return rule if rule in PLURAL_RULES else "one_other"


def _category(code: str, n: Any) -> str:
    """The plural category of ``n``: a fraction (1.5 GB) or a non-number is ``other`` (CLDR)."""
    try:
        number = float(n)
    except TypeError, ValueError:
        return "other"
    if not math.isfinite(number) or not number.is_integer():
        return "other"
    return PLURAL_RULES[plural_rule(code)](abs(int(number)))


def _plural(code: str, n: Any, forms: dict[str, Any]) -> str:
    return str(forms.get(_category(code, n)) or forms.get("other") or "")


# --- lookup -----------------------------------------------------------------------------------

_PLACEHOLDER = re.compile(r"\{([a-z_][a-z0-9_]*)\}")


def fill(text: str, params: dict[str, Any]) -> str:
    """``{name}`` placeholders filled from ``params``; anything else (a stray brace, ``{0}``,
    ``{x.attr}``, an unknown name) stays as written. Never raises: a translation typo shows, it
    does not break the page."""
    if not params or "{" not in text:
        return text

    def value(match: re.Match[str]) -> str:
        name = match.group(1)
        if name not in params:
            return match.group(0)
        try:
            return str(params[name])
        except Exception:  # noqa: BLE001 - a value that cannot be shown keeps its placeholder; a text never breaks a page
            return match.group(0)

    return _PLACEHOLDER.sub(value, text)


def translate(key: str, lang: str | None = None, /, **params: Any) -> str:
    """The text of ``key`` in ``lang`` (default: the current language), English as fallback.

    ``n=`` picks the plural form; other keyword values fill ``{name}`` placeholders.
    """
    for code in dict.fromkeys((lang or current(), DEFAULT)):
        value = _load(code)[0].get(key)
        if value is None:
            continue
        if isinstance(value, dict):
            value = _plural(code, params.get("n", 0), value)
        return fill(str(value), params)
    return key


t = translate


def has(key: str, lang: str = DEFAULT) -> bool:
    return key in _load(lang)[0]


def keys(lang: str = DEFAULT) -> set[str]:
    return set(_load(lang)[0])


def texts_with_prefix(prefix: str, lang: str | None = None) -> dict[str, str]:
    """Every text whose key starts with ``prefix`` (``js.`` for app.js), English filling gaps;
    built once per language and catalog version."""
    code = lang or current()
    return dict(_prefixed(prefix, code, version()))


@functools.cache
def _prefixed(prefix: str, code: str, _version: int) -> tuple[tuple[str, str], ...]:
    names = sorted(key for key in keys(DEFAULT) | keys(code) if key.startswith(prefix))
    return tuple((key, translate(key, code)) for key in names)


# --- dates and numbers ------------------------------------------------------------------------


def datetime_pattern(lang: str | None = None, *, short: bool = False) -> str:
    """The strftime pattern of ``lang``'s ``_meta.datetime`` (``datetime_short`` when ``short``);
    the page scripts get it too, so a date reads the same in a page and in its live updates."""
    field, default = ("datetime_short", DATETIME_SHORT_FORMAT) if short else ("datetime", DATETIME_FORMAT)
    return str(_load(lang or current())[1].get(field) or default)


def format_datetime(value: datetime, lang: str | None = None, *, short: bool = False) -> str:
    """A date and time the way ``lang`` writes it (its ``_meta.datetime``: digits only); ``short``:
    day, month and minutes (``_meta.datetime_short``) for a time of this year."""
    default = DATETIME_SHORT_FORMAT if short else DATETIME_FORMAT
    pattern = datetime_pattern(lang, short=short)
    try:
        return value.strftime(pattern)
    except ValueError:
        return value.strftime(default)


def format_decimal(value: float, digits: int = 1, lang: str | None = None) -> str:
    """``1.5`` as ``lang`` writes it (``1,5`` in Russian)."""
    point = str(_load(lang or current())[1].get("decimal") or DECIMAL_POINT)
    return f"{value:.{digits}f}".replace(".", point)


# --- which language ---------------------------------------------------------------------------


def current() -> str:
    """The language of this request or task; outside both (a background thread, a direct call)
    the owner's language - the one TOW's messages use."""
    return _CURRENT.get() or message_language()


def use(lang: str) -> None:
    """Set the language of this request / task (a ContextVar: threads and requests stay apart)."""
    _CURRENT.set(canonical(lang) or DEFAULT)


_Q = re.compile(r"^(?:[0-9]+(?:\.[0-9]*)?|\.[0-9]+)$")


def _accepted(accept_language: str | None) -> list[str]:
    """The language ranges of an ``Accept-Language`` header, best first (RFC 9110).

    A range with a malformed ``q`` is ignored; ``q`` above 1 counts as 1; ``q=0`` means "not this one".
    """
    ranked = []
    for index, part in enumerate(str(accept_language or "").split(",")):
        tag, *params = (piece.strip() for piece in part.split(";"))
        if not _CODE.match(tag):
            continue  # empty, "*" or not a language tag
        quality = 1.0
        valid = True
        for param in params:
            name, _, raw = param.partition("=")
            if name.strip().lower() != "q":
                continue
            raw = raw.strip()
            if not _Q.match(raw):
                valid = False
                break
            quality = min(1.0, float(raw))
        if valid and quality > 0:
            ranked.append((-quality, index, tag))
    return [tag for _q, _i, tag in sorted(ranked)]


def _lookup_tag(tag: str, supported: dict[str, str]) -> str | None:
    """RFC 4647 lookup: ``zh-Hant-TW`` tries ``zh-hant-tw``, ``zh-hant``, then ``zh``."""
    parts = tag.lower().split("-")
    while parts:
        found = supported.get("-".join(parts))
        if found:
            return found
        parts.pop()
        if len(parts) > 1 and len(parts[-1]) == 1:
            parts.pop()  # a singleton ("x", "u") never ends a range
    return None


def negotiate(accept_language: str | None, supported: list[str] | None = None) -> str:
    """The best supported language for an ``Accept-Language`` header (RFC 9110 q-values,
    RFC 4647 lookup, any case)."""
    pool = {code.lower(): code for code in (supported if supported is not None else codes())}
    for tag in _accepted(accept_language):
        found = _lookup_tag(tag, pool)
        if found:
            return found
    return pool.get(DEFAULT, DEFAULT)


def setting(cfg: dict[str, Any]) -> str:
    """``auto`` or a supported code, from config.yaml (anything else counts as ``auto``)."""
    return canonical(str(cfg.get("language") or AUTO)) or AUTO


def for_request(cfg: dict[str, Any], accept_language: str | None) -> str:
    chosen = setting(cfg)
    return negotiate(accept_language) if chosen == AUTO else chosen


def message_language(cfg: dict[str, Any] | None = None) -> str:
    """The language of messages to messengers and of texts a scheduled task writes: the chosen
    one, or - with ``auto`` - the language the owner's browser last asked for."""
    chosen = setting(cfg) if cfg is not None else setting({"language": _configured_language()})
    if chosen != AUTO:
        return chosen
    return canonical(_seen_language(_seen_path())) or DEFAULT


def terminal_language() -> str:
    """The language of a command the owner types: the chosen one, or - with ``auto`` - the
    operating system's (a browser elsewhere does not change what a terminal shows)."""
    chosen = setting({"language": _configured_language()})
    if chosen != AUTO:
        return chosen
    from tow import platform

    try:
        system = platform.current().ui_language()
    except Exception:  # noqa: BLE001 - an unknown system language is the default, never a failure
        system = None
    return negotiate(system) if system else DEFAULT


def _configured_language() -> str:
    """``language`` from config.yaml, read again only when the file changes (``t()`` outside a
    request asks for it on every call)."""
    try:
        from tow.paths import config_path

        path = config_path()
        stat = path.stat()
        stamp = (str(path), stat.st_mtime_ns, stat.st_size, stat.st_ino)
    except OSError, RuntimeError:
        return AUTO
    return _configured_language_of(stamp)


@functools.lru_cache(maxsize=4)
def _configured_language_of(_stamp: tuple[str, int, int, int]) -> str:
    from tow.config import load_config

    try:
        return str(load_config().get("language") or AUTO)
    except Exception as exc:  # noqa: BLE001 - a broken config must not take every text down: automatic
        _LOG.warning("language setting not read (automatic is used): %s", type(exc).__name__)
        return AUTO


def _seen_language(path: Path) -> str | None:
    try:
        stamp = path.stat().st_mtime_ns
    except OSError:
        return None
    return _read_seen(str(path), stamp)


@functools.lru_cache(maxsize=4)
def _read_seen(path: str, _stamp: int) -> str | None:
    try:
        value = json.loads(Path(path).read_text(encoding="utf-8")).get("language")
    except OSError, ValueError, AttributeError:
        return None
    return str(value) if value else None


def _seen_path() -> Path:
    from tow.paths import data_dir

    return data_dir() / "ui-language.json"


def remember_browser_language(lang: str) -> None:
    """Keep the language the owner's browser asks for (written only when it changes)."""
    path = _seen_path()
    if _seen_language(path) == lang:
        return
    from tow.store import atomic_write_text

    try:
        atomic_write_text(path, json.dumps({"language": lang}) + "\n")
    except OSError:
        return
