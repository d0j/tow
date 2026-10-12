"""Finding plugin modules: the one scan the torrent clients and the messengers share.

A plugin family is a package with one module per plugin (``tow.clients``, ``tow.notifiers``) and
a few helper modules that are not plugins. ``scan`` imports every other module of the package and
asks the family's ``load`` what it is; each family keeps its own rules (what makes a module a
plugin, what makes it broken) and its own order. One broken module (a missing dependency, a typo)
never takes the others down: it is named in the skipped list and logged once.
"""

from __future__ import annotations

import importlib
import logging
import pkgutil
from collections.abc import Callable, Collection
from types import ModuleType


def scan[T](
    package: ModuleType,
    *,
    helpers: Collection[str],
    load: Callable[[ModuleType], T | None],
    label: str,
    logger: logging.Logger,
) -> tuple[list[T], list[tuple[str, str]]]:
    """(what ``load`` made of each plugin module, (module name, reason) of each broken one).

    Modules whose name starts with ``_`` and the ``helpers`` are not looked at. ``load`` returns
    None for a module that is not a plugin (it is left out quietly) and raises for a broken one;
    a module that fails to import is broken too. ``label`` names the family in the log line
    ("torrent client", "messenger").
    """
    found: list[T] = []
    skipped: list[tuple[str, str]] = []
    for info in pkgutil.iter_modules(package.__path__):
        if info.name.startswith("_") or info.name in helpers:
            continue
        try:
            item = load(importlib.import_module(f"{package.__name__}.{info.name}"))
        except Exception as exc:  # noqa: BLE001 - a broken plugin is skipped and named, never fatal
            skipped.append((info.name, f"{type(exc).__name__}: {exc}"))
            logger.warning("%s module %s skipped: %s: %s", label, info.name, type(exc).__name__, exc)
            continue
        if item is not None:
            found.append(item)
    return found, skipped
