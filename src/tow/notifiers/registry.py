"""Which messengers exist, their saved settings and recipients (no network here)."""

from __future__ import annotations

import functools
import hashlib
import importlib
import json
import logging
import pkgutil
import re
from typing import Any, cast

from tow import i18n
from tow.i18n import t
from tow.notifiers.base import Notifier, notifier_problems, text, ui_language

_SKIP = frozenset({"base", "registry", "outbox", "view"})


_LOG = logging.getLogger("tow.notifiers")
# Messenger modules that failed to load: (module name, reason). The others work without them.
SKIPPED: list[tuple[str, str]] = []


@functools.cache
def _discover() -> tuple[tuple[str, Notifier], ...]:
    import tow.notifiers as package

    found: dict[str, Notifier] = {}
    SKIPPED.clear()
    for info in pkgutil.iter_modules(package.__path__):
        if info.name.startswith("_") or info.name in _SKIP:
            continue
        # One broken messenger (a missing dependency, a typo) must not take the others down.
        try:
            module = importlib.import_module(f"tow.notifiers.{info.name}")
            if not getattr(module, "KIND", None):
                continue  # a helper module, not a messenger
            if missing := notifier_problems(module):
                raise TypeError(f"not a messenger module, missing {', '.join(missing)}")
            found[str(module.KIND)] = cast(Notifier, module)
        except Exception as exc:  # noqa: BLE001 - a broken plugin is skipped and named, never fatal
            SKIPPED.append((info.name, f"{type(exc).__name__}: {exc}"))
            _LOG.warning("messenger module %s skipped: %s: %s", info.name, type(exc).__name__, exc)
    return tuple(sorted(found.items(), key=lambda item: _order(item[1])))


def _order(module: Notifier) -> int:
    try:
        return int(getattr(module, "ORDER", 100))
    except TypeError, ValueError:
        return 100


def kinds() -> dict[str, Notifier]:
    """Every messenger module, in display order (scanned once per process)."""
    return dict(_discover())


def get(kind: str) -> Notifier:
    module = kinds().get(kind)
    if module is None:
        raise KeyError(f"unknown messenger: {kind}")
    return module


def title(module: Notifier, lang: str | None = None) -> str:
    """The messenger's name: its TITLE (a product name), or ``notifier.<kind>.title`` when the
    language files have one (a name with words in it, like ntfy's)."""
    key = f"notifier.{module.KIND}.title"
    return i18n.translate(key, lang or ui_language()) if i18n.has(key) else str(module.TITLE)


def raw_settings(secrets: dict[str, Any], kind: str) -> dict[str, Any] | None:
    storage = getattr(get(kind), "STORAGE", None)
    raw = secrets.get(storage) if storage else (secrets.get("notifiers") or {}).get(kind)
    return raw if isinstance(raw, dict) else None


def settings_of(secrets: dict[str, Any], kind: str) -> dict[str, Any] | None:
    """The saved settings of a messenger, or None when it is not connected."""
    raw = raw_settings(secrets, kind)
    if raw is None:
        return None
    required = [field.name for field in get(kind).FIELDS if field.required]
    return raw if all(raw.get(name) for name in required) else None


def connected(secrets: dict[str, Any]) -> list[tuple[str, Notifier, dict[str, Any]]]:
    out = []
    for kind, module in kinds().items():
        settings = settings_of(secrets, kind)
        if settings is not None and settings.get("enabled", True) is not False:
            out.append((kind, module, settings))
    return out


def targets(secrets: dict[str, Any]) -> list[tuple[str, str, Notifier, dict[str, Any]]]:
    """(target key, kind, module, settings) per recipient: a Telegram chat is its own target,
    so one chat that never pressed Start does not make the others receive repeats."""
    out = []
    for kind, module, settings in connected(secrets):
        split = getattr(module, "targets", None)
        for name, target_settings in split(settings) if callable(split) else [("", settings)]:
            out.append((f"{kind}:{name}" if name else kind, kind, module, target_settings))
    return out


def fingerprint(settings: dict[str, Any]) -> str:
    """Changes when the owner changes the settings (a blocked queue is retried then)."""
    return hashlib.sha256(json.dumps(settings, sort_keys=True, default=str).encode("utf-8")).hexdigest()[:16]


def validate(kind: str, form: dict[str, str], current: dict[str, Any] | None) -> tuple[dict[str, Any], list[str]]:
    """Form values -> settings, with plain-language errors. An empty secret keeps the saved one."""
    module = get(kind)
    lang = ui_language()
    values: dict[str, Any] = {}
    errors: list[str] = []
    for field in module.FIELDS:
        raw = str(form.get(field.name) or "").strip()
        label = text(field.label, lang)
        if field.kind == "secret" and not raw and current and current.get(field.name):
            values[field.name] = current[field.name]
            continue
        if field.kind == "list":
            items = [part.strip() for part in re.split(r"[,\s;]+", raw) if part.strip()]
            if field.required and not items:
                errors.append(t("notifier.common.required", lang, field=label))
            elif field.pattern and not all(re.fullmatch(field.pattern, item) for item in items):
                errors.append(f"{label}: {text(field.error, lang)}")
            values[field.name] = items
            continue
        if not raw:
            if field.required:
                errors.append(t("notifier.common.required", lang, field=label))
            elif field.default:
                values[field.name] = field.default
            continue
        if field.pattern and not re.fullmatch(field.pattern, raw):
            errors.append(f"{label}: {text(field.error, lang)}")
        values[field.name] = raw
    return values, errors


def store(secrets: dict[str, Any], kind: str, values: dict[str, Any]) -> None:
    storage = getattr(get(kind), "STORAGE", None)
    if storage:
        secrets.setdefault(storage, {}).update(values)
    else:
        secrets.setdefault("notifiers", {})[kind] = dict(values)


def remove(secrets: dict[str, Any], kind: str) -> None:
    storage = getattr(get(kind), "STORAGE", None)
    if storage:
        secrets.pop(storage, None)
    else:
        (secrets.get("notifiers") or {}).pop(kind, None)
