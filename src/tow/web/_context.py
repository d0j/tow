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

from collections.abc import Callable
from contextvars import ContextVar
from typing import Any

from tow.store import SecretStoreError, write_generation
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
