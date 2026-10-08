"""What one request reads: the config, a state snapshot and the secrets, each at most once.

The middleware opens a context for every request (a ContextVar, so the route's worker thread and
the templates see the same one). Readers that only look - the header, the undo button, Home, the
settings page - take their data from it; a route that changes data still reads (under its lock)
on its own and never gets these shared objects, which callers must not modify.

A file written by this process during the request (``store.write_generation``) drops everything
read so far, so a page rendered after a save shows the save. Outside a request (a test calling a
helper, a background thread) every call reads afresh.
"""

from __future__ import annotations

import threading
from collections.abc import Callable, Mapping
from contextvars import ContextVar
from typing import Any

from tow.store import SecretStoreError, write_generation
from tow.trackers import GenericHttpTracker, load_trackers, match_tracker
from tow.web import services


class RequestContext:
    """Values read once per request, by name."""

    __slots__ = ("_generation", "_values")

    def __init__(self) -> None:
        self._generation = write_generation()
        self._values: dict[str, tuple[bool, Any]] = {}

    def get(self, name: str, load: Callable[[], Any], *, keep_errors: tuple[type[BaseException], ...] = ()) -> Any:
        generation = write_generation()
        if generation != self._generation:
            self._values.clear()
            self._generation = generation
        if name not in self._values:
            try:
                self._values[name] = (True, load())
            except keep_errors as exc:  # the same answer for the rest of the request (a broken secret store)
                self._values[name] = (False, exc)
        ok, value = self._values[name]
        if not ok:
            raise value
        return value


_CURRENT: ContextVar[RequestContext | None] = ContextVar("tow_request_context", default=None)


def begin() -> RequestContext:
    """A fresh context for the request the middleware is handling."""
    context = RequestContext()
    _CURRENT.set(context)
    return context


def current() -> RequestContext | None:
    return _CURRENT.get()


def memo[T](name: str, load: Callable[[], T], *, keep_errors: tuple[type[BaseException], ...] = ()) -> T:
    """``load()`` once per request (every time outside one)."""
    context = _CURRENT.get()
    if context is None:
        return load()
    value: T = context.get(name, load, keep_errors=keep_errors)
    return value


# The loaders are looked up in tow.web.services when called: tests replace them there.
def config() -> dict[str, Any]:
    return memo("config", lambda: services.load_config())


def state(*, quarantine: bool = True) -> dict[str, Any]:
    """A read-only snapshot of the state. ``quarantine=False`` (the middleware's pre-check) only
    matters when the file has to be read and turns out damaged: it is then left in place."""
    return memo(
        "state", (lambda: services.load_state()) if quarantine else (lambda: services.load_state(quarantine=False))
    )


def secrets() -> dict[str, Any]:
    """The decrypted secrets (read-only); a SecretStoreError is raised again on every call."""
    return memo("secrets", lambda: services.load_secrets(), keep_errors=(SecretStoreError,))


def secrets_or_none() -> dict[str, Any] | None:
    try:
        return secrets()
    except SecretStoreError:
        return None


def trackers() -> dict[str, GenericHttpTracker]:
    """The sites of this request's config, built once (each compiles its link pattern)."""
    return memo("trackers", lambda: load_trackers(config()))


# Which site a topic link belongs to (``match_tracker``: the first site, in config order, whose
# link pattern takes the link). Home and the header ask it for every topic, the header every few
# seconds: 2000 topics against 200 sites were 400 000 pattern runs per page. The answer depends
# only on the sites' names and patterns in order, so it is kept across requests for exactly that
# list (its ``signature``): another list - a site added, removed, renamed, reordered or with a
# new pattern - starts afresh, and a new or edited topic link is simply not known yet.
_MATCH_LOCK = threading.Lock()
_MATCH_LIMIT = 50_000
_matches: tuple[object, dict[str, Any]] = ((), {})


def _site_signature() -> tuple[tuple[Any, Any], ...] | None:
    """The sites' names and link patterns in order (None: not usable as a key)."""

    def build() -> tuple[tuple[Any, Any], ...] | None:
        sites = config().get("trackers") or {}
        if not isinstance(sites, Mapping):
            return None
        signature = tuple(
            (name, spec.get("url_regex") if isinstance(spec, Mapping) else None) for name, spec in sites.items()
        )
        try:
            hash(signature)
        except TypeError:
            return None
        return signature

    return memo("site_signature", build)


def site_name(url: object) -> Any:
    """The name of the site ``url`` belongs to, or None: ``match_tracker`` over this request's
    sites, remembered for the same sites (see above)."""
    global _matches
    signature = _site_signature()
    if signature is None or not isinstance(url, str):
        tracker = match_tracker(trackers(), url)  # type: ignore[arg-type]  # fails as it always did
        return tracker.name if tracker else None
    with _MATCH_LOCK:
        known_signature, known = _matches
        if known_signature != signature or len(known) > _MATCH_LIMIT:
            known = {}
            _matches = (signature, known)
        if url in known:
            return known[url]
    tracker = match_tracker(trackers(), url)  # a pattern that runs too long raises: not remembered
    name = tracker.name if tracker else None
    with _MATCH_LOCK:
        if _matches[0] == signature:
            _matches[1][url] = name
    return name


def tracker_of(url: object) -> GenericHttpTracker | None:
    """The site ``url`` belongs to (``match_tracker`` over this request's sites)."""
    name = site_name(url)
    return None if name is None else trackers()[name]
