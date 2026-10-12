from __future__ import annotations

import functools
import logging
from collections.abc import Callable, Sequence
from dataclasses import dataclass, replace
from typing import Any, ClassVar, Protocol, runtime_checkable

from tow import i18n, plugins

# The client of a configuration that names none. config.DEFAULTS holds the same kind as a
# literal: config sits below the clients and does not import them (tests/test_client_registry.py).
DEFAULT_KIND = "qbittorrent"
# The modules of tow.clients that are not clients (tests/test_layers.py reads it too).
HELPERS = frozenset({"address", "spec", "factory", "managed", "files"})
_LOG = logging.getLogger("tow.clients")
# Client modules that failed to load: (module name, reason). The others work without them.
SKIPPED: list[tuple[str, str]] = []


# The optional abilities a client declares in ``capabilities`` (True: it has it). A missing
# ability is a False flag, never a missing method: every method below exists on every client.
CAPABILITIES = (
    "inspect",  # inspect_torrent reports the torrent
    "list_files",  # ... with its files
    "completion_time",  # ... and when it completed
    "add",
    "stopped_add",  # a torrent can be added stopped
    "file_selection",  # files can be chosen before it starts
    "priority_readback",  # the chosen files can be read back
    "start_stop",
    "magnet_metadata",  # materialize_magnet works (else it refuses)
    "metadata_preview",  # preview_magnet never adds or changes a normal torrent task
)


@runtime_checkable
class TorrentClientAdapter(Protocol):
    """What TOW calls on a torrent client (``from_secrets`` of a client module returns one).

    ``tow.clients.managed.ManagedClient`` implements all of it on top of a few primitives;
    ``tests/test_plugin_contracts.py`` checks every client module against it.
    """

    client_id: str
    client_kind: str
    capabilities: ClassVar[dict[str, bool]]
    # Set by the factory from the client's settings: an optional category and extra labels for
    # torrents TOW adds (G8), and the preview mode of a dry run (no change in the client).
    add_category: str
    add_tags: Sequence[str]
    read_only: bool

    def ping(self) -> str: ...

    def has_hash(self, infohash: str) -> bool: ...

    def inspect_torrent(self, infohash: str) -> dict[str, Any] | None: ...

    def add_torrent_selected(
        self,
        content: bytes,
        save_path: str | None,
        infohash: str,
        selected_indices: list[int] | tuple[int, ...],
        *,
        start: bool = True,
    ) -> dict[str, Any]: ...

    def configure_torrent_selection(
        self,
        content: bytes,
        infohash: str,
        selected_indices: list[int] | tuple[int, ...],
        *,
        ensure_started: bool = False,
        keep_stopped: bool = False,
    ) -> dict[str, Any]: ...

    def materialize_magnet(
        self,
        magnet_url: str,
        save_path: str | None,
        infohash: str,
    ) -> bytes: ...

    def preview_magnet(self, magnet_url: str) -> bytes: ...

    def stop_owned_torrent(self, infohash: str) -> dict[str, Any]: ...

    def start_owned_torrent(self, infohash: str) -> dict[str, Any]: ...

    def set_location(self, infohash: str, save_path: str) -> str: ...


# The language of what the owner sees (i18n.current), and a catalog key in it or a plain text as
# it is (i18n.text_or_key): kept here for the client modules and the settings page.
ui_language = i18n.current


def text(value: str, lang: str | None = None) -> str:
    return i18n.text_or_key(value, lang)


@dataclass(frozen=True)
class ClientField:
    """One field of the client's settings card (stored in the client's secret block).

    ``label`` and ``placeholder`` are language-file keys (or plain text); ``ClientSpec.fields``
    gives them translated."""

    name: str  # host | port | username | password
    label: str
    placeholder: str = ""


# Address, port, login and password: what most clients' web interfaces ask for.
DEFAULT_FIELDS = (
    ClientField("host", "client.fields.host", "127.0.0.1"),
    ClientField("port", "client.fields.port"),
    ClientField("username", "client.fields.username"),
    ClientField("password", "client.fields.password"),
)


@dataclass(frozen=True)
class ClientSpec:
    """A client module as the settings page sees it. The module's STEPS, NOTE and field labels
    are keys of the language files (``client.<kind>.*``); ``steps``, ``note`` and ``fields``
    return them in the owner's language."""

    kind: str
    title: str
    secrets_key: str
    ready: bool
    default_port: int
    load: Callable[[dict[str, Any]], TorrentClientAdapter]
    field_keys: tuple[ClientField, ...] = DEFAULT_FIELDS
    step_keys: tuple[str, ...] = ()
    note_key: str = ""
    short: str = ""  # the header label, e.g. "qBit"
    order: int = 100

    @property
    def fields(self) -> tuple[ClientField, ...]:
        lang = ui_language()
        return tuple(
            replace(field, label=text(field.label, lang), placeholder=text(field.placeholder, lang))
            for field in self.field_keys
        )

    @property
    def steps(self) -> tuple[str, ...]:
        lang = ui_language()
        return tuple(text(step, lang) for step in self.step_keys)

    @property
    def note(self) -> str:
        return text(self.note_key)


@functools.cache
def _discover() -> tuple[tuple[str, ClientSpec], ...]:
    """The client modules by ORDER, then kind (``cache_clear()`` scans again)."""
    import tow.clients as pkg

    found, skipped = plugins.scan(pkg, helpers=HELPERS, load=_spec_of, label="torrent client", logger=_LOG)
    SKIPPED[:] = skipped
    out = {spec.kind: spec for spec in found}
    return tuple(sorted(out.items(), key=lambda item: (item[1].order, item[0])))


def _spec_of(mod: Any) -> ClientSpec | None:
    kind = getattr(mod, "KIND", None)
    load = getattr(mod, "from_secrets", None)
    if not kind or not callable(load):
        return None
    return ClientSpec(
        kind=str(kind),
        title=str(getattr(mod, "TITLE", kind)),
        secrets_key=str(getattr(mod, "SECRETS_KEY", kind)),
        ready=bool(getattr(mod, "READY", False)),
        default_port=int(getattr(mod, "DEFAULT_PORT", 8080)),
        load=load,
        field_keys=tuple(getattr(mod, "FIELDS", DEFAULT_FIELDS)),
        step_keys=tuple(getattr(mod, "STEPS", ())),
        note_key=str(getattr(mod, "NOTE", "")),
        short=str(getattr(mod, "SHORT", "") or getattr(mod, "TITLE", kind)),
        order=int(getattr(mod, "ORDER", 100)),
    )


_FIELD_NAMES = frozenset({"host", "port", "username", "password"})


def client_module_problems(module: object) -> list[str]:
    """What is wrong with the constants of a client module (empty: nothing).

    ``TITLE``, ``SECRETS_KEY``, ``DEFAULT_PORT`` and ``ORDER`` must be declared; ``FIELDS`` may
    be left out (the address, port, login and password). Discovery does not refuse a module for
    these - it falls back to a default - so ``tests/test_plugin_contracts.py`` checks them."""
    found = []
    for name in ("TITLE", "SECRETS_KEY"):
        value = getattr(module, name, None)
        if not isinstance(value, str) or not value.strip():
            found.append(f"{name} (not a text)")
    port = getattr(module, "DEFAULT_PORT", None)
    if not isinstance(port, int) or isinstance(port, bool) or not 1 <= port <= 65535:
        found.append("DEFAULT_PORT (not a port number)")
    order = getattr(module, "ORDER", None)
    if not isinstance(order, int) or isinstance(order, bool):
        found.append("ORDER (not a whole number)")
    fields = getattr(module, "FIELDS", DEFAULT_FIELDS)
    if not isinstance(fields, tuple) or not all(isinstance(field, ClientField) for field in fields):
        found.append("FIELDS (not a tuple of ClientField)")
    else:
        names = [field.name for field in fields]
        if len(set(names)) != len(names) or not set(names) <= _FIELD_NAMES or not {"host", "port"} <= set(names):
            found.append(f"FIELDS (host and port, and only {', '.join(sorted(_FIELD_NAMES))}, each once)")
    return sorted(found)


def discover() -> dict[str, ClientSpec]:
    """Every client module, in display order (scanned once per process)."""
    return dict(_discover())


def ready() -> list[ClientSpec]:
    return [s for s in discover().values() if s.ready]


def get(kind: str) -> ClientSpec | None:
    return discover().get(kind)
