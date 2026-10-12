"""The issue kinds, found once, and the one list of issues they make from a snapshot."""

from __future__ import annotations

import dataclasses
import functools
import logging
from collections.abc import Callable
from dataclasses import dataclass
from types import ModuleType

from tow import plugins
from tow.issues.model import SEVERITIES, Issue, Message, Snapshot

_LOG = logging.getLogger("tow.issues")
# Kind modules that failed to load: (module name, reason). The other kinds work without them.
SKIPPED: list[tuple[str, str]] = []


@dataclass(frozen=True, slots=True)
class Kind:
    """One kind module: its name, its place among the kinds of the same severity, its rule."""

    name: str
    order: int
    collect: Callable[[Snapshot], list[Issue]]


def _kind(module: ModuleType) -> Kind | None:
    """The module as an issue kind; TypeError for one without ``KIND``, ``ORDER`` or ``collect``."""
    name = getattr(module, "KIND", None)
    order = getattr(module, "ORDER", None)
    rule = getattr(module, "collect", None)
    if not isinstance(name, str) or not name:
        raise TypeError("no KIND")
    if not isinstance(order, int) or isinstance(order, bool):
        raise TypeError("ORDER is not a whole number")
    if not callable(rule):
        raise TypeError("no collect(snapshot)")
    return Kind(name, order, rule)


@functools.cache
def known_kinds() -> tuple[Kind, ...]:
    """Every kind module of ``tow.issues.kinds``, by ORDER (scanned once per process)."""
    import tow.issues.kinds as package

    found, skipped = plugins.scan(package, helpers=(), load=_kind, label="issue kind", logger=_LOG)
    SKIPPED[:] = skipped
    return tuple(sorted(found, key=lambda kind: (kind.order, kind.name)))


def _merged(issues: list[Issue]) -> list[Issue]:
    """One issue per id, where the first one was: counts added, subjects joined, the target for
    several taken, and a ``count`` value of the message following the count."""
    out: dict[str, Issue] = {}
    for issue in issues:
        first = out.get(issue.id)
        if first is None:
            out[issue.id] = issue
            continue
        count = first.count + issue.count
        params = dict(first.message.params)
        if "count" in params:
            params["count"] = count
        out[issue.id] = dataclasses.replace(
            first,
            count=count,
            subjects=first.subjects + tuple(subject for subject in issue.subjects if subject not in first.subjects),
            message=Message(first.message.key, params),
            target=first.target_many or first.target,
        )
    return list(out.values())


def collect(snapshot: Snapshot, *, only: tuple[Kind, ...] | None = None) -> list[Issue]:
    """What needs the owner now: worst first, then by the kind's ORDER, then as the kind listed
    them; one issue per problem. A kind whose rule fails is left out and logged: the others are
    still said."""
    rank = {severity: index for index, severity in enumerate(SEVERITIES)}
    found: list[tuple[int, Issue]] = []
    for kind in known_kinds() if only is None else only:
        try:
            made = kind.collect(snapshot)
        except Exception as exc:  # noqa: BLE001 - one broken rule must not hide the other problems (logged)
            _LOG.warning("issue kind %s failed: %s: %s", kind.name, type(exc).__name__, exc)
            continue
        found += [(kind.order, issue) for issue in made]
    ordered = [issue for _order, issue in sorted(found, key=lambda item: (rank[item[1].severity], item[0]))]
    return _merged(ordered)
