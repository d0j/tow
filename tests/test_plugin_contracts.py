"""Every plugin module TOW discovers satisfies its contract (a Protocol).

A torrent client module returns a ``TorrentClientAdapter``; a messenger module is a
``Notifier``. Adding or fixing one module must not need a change anywhere else: these tests
fail when a module forgets a member the rest of TOW calls.
"""

from __future__ import annotations

import importlib
import inspect
import pkgutil
from typing import get_protocol_members

import pytest

import tow.clients
import tow.notifiers
from tow.clients import spec
from tow.clients.spec import CAPABILITIES, ClientField, TorrentClientAdapter, client_module_problems
from tow.notifiers import registry
from tow.notifiers.base import Field, notifier_problems


def adapter_problems(adapter: object) -> list[str]:
    """The members of the client contract ``adapter`` lacks (empty: it is a complete client)."""
    return sorted(name for name in get_protocol_members(TorrentClientAdapter) if not hasattr(adapter, name))


def _client_classes():
    """Every adapter class a client module defines (its from_secrets returns one)."""
    for info in pkgutil.iter_modules(tow.clients.__path__):
        if info.name in spec.HELPERS or info.name.startswith("_"):
            continue
        module = importlib.import_module(f"tow.clients.{info.name}")
        if not getattr(module, "READY", False):
            continue
        hint = inspect.signature(module.from_secrets).return_annotation
        classes = [
            value
            for value in vars(module).values()
            if inspect.isclass(value) and value.__module__ == module.__name__ and hasattr(value, "inspect_torrent")
        ]
        assert classes, f"{info.name}: no adapter class"
        for cls in classes:
            yield info.name, cls, hint


CLIENTS = list(_client_classes())


def test_every_ready_client_module_is_discovered():
    discovered = set(spec.discover())
    assert {name for name, _cls, _hint in CLIENTS} <= discovered
    assert not spec.SKIPPED


@pytest.mark.parametrize(("name", "cls"), [(name, cls) for name, cls, _hint in CLIENTS])
def test_every_client_adapter_satisfies_the_contract(name, cls):
    adapter = cls.__new__(cls)  # no connection: only the members are looked at
    assert adapter_problems(adapter) == [], name
    assert isinstance(adapter, TorrentClientAdapter), name
    for member in get_protocol_members(TorrentClientAdapter):
        value = getattr(cls, member, None)
        if member in {"client_id", "client_kind", "capabilities", "add_category", "add_tags", "read_only"}:
            continue
        assert callable(value), f"{name}.{member} is not a method"


@pytest.mark.parametrize(("name", "cls"), [(name, cls) for name, cls, _hint in CLIENTS])
def test_client_capabilities_are_explicit_flags(name, cls):
    # An optional ability is a flag, never a missing method: every known flag is a bool.
    assert set(cls.capabilities) <= set(CAPABILITIES), name
    assert all(isinstance(value, bool) for value in cls.capabilities.values()), name
    for flag in ("inspect", "add", "stopped_add", "file_selection", "priority_readback", "start_stop"):
        assert cls.capabilities.get(flag) is True, f"{name}: {flag}"


@pytest.mark.parametrize(("name", "hint"), [(name, hint) for name, _cls, hint in CLIENTS])
def test_client_module_promises_the_contract(name, hint):
    assert hint in {"TorrentClientAdapter", TorrentClientAdapter}, name


def _client_modules():
    """Every client module (ready or not): the modules of the package with a KIND."""
    for info in pkgutil.iter_modules(tow.clients.__path__):
        if info.name in spec.HELPERS or info.name.startswith("_"):
            continue
        module = importlib.import_module(f"tow.clients.{info.name}")
        if getattr(module, "KIND", None):
            yield info.name, module


CLIENT_MODULES = list(_client_modules())


@pytest.mark.parametrize(("name", "module"), CLIENT_MODULES)
def test_every_client_module_declares_its_constants(name, module):
    assert client_module_problems(module) == [], name
    assert name == module.KIND


def test_a_client_module_with_wrong_constants_is_named():
    import types

    module = types.ModuleType("tow.clients.zz_draft")
    module.KIND = "zz_draft"
    assert client_module_problems(module) == [
        "DEFAULT_PORT (not a port number)",
        "ORDER (not a whole number)",
        "SECRETS_KEY (not a text)",
        "TITLE (not a text)",
    ]
    module.TITLE, module.SECRETS_KEY, module.DEFAULT_PORT, module.ORDER = "Draft", "zz_draft", 8080, 90
    assert client_module_problems(module) == []  # FIELDS may be left out
    for wrong in (70000, True, "8080"):
        module.DEFAULT_PORT = wrong
        assert client_module_problems(module) == ["DEFAULT_PORT (not a port number)"], wrong
    module.DEFAULT_PORT, module.ORDER = 8080, "10"
    assert client_module_problems(module) == ["ORDER (not a whole number)"]
    module.ORDER = 90
    module.FIELDS = [ClientField("host", "x"), ClientField("port", "y")]  # a list
    assert client_module_problems(module) == ["FIELDS (not a tuple of ClientField)"]
    for fields in (
        (ClientField("host", "x"),),  # no port
        (ClientField("host", "x"), ClientField("port", "y"), ClientField("token", "z")),
        (ClientField("host", "x"), ClientField("port", "y"), ClientField("port", "z")),
    ):
        module.FIELDS = fields
        assert [problem.split(" ")[0] for problem in client_module_problems(module)] == ["FIELDS"], fields


def test_a_module_without_the_contract_is_named():
    class Half:
        def ping(self):
            return "ok"

    problems = adapter_problems(Half())
    assert "inspect_torrent" in problems
    assert "set_location" in problems
    assert "ping" not in problems
    assert not isinstance(Half(), TorrentClientAdapter)


def _messenger_modules():
    for info in pkgutil.iter_modules(tow.notifiers.__path__):
        if info.name.startswith("_") or info.name in registry.HELPERS:
            continue
        yield info.name, importlib.import_module(f"tow.notifiers.{info.name}")


MESSENGERS = list(_messenger_modules())


def test_every_messenger_module_is_discovered():
    assert {module.KIND for _name, module in MESSENGERS} == set(registry.kinds())
    assert not registry.SKIPPED


@pytest.mark.parametrize(("name", "module"), MESSENGERS)
def test_every_messenger_module_satisfies_the_contract(name, module):
    assert notifier_problems(module) == [], name
    assert callable(module.send)
    assert list(inspect.signature(module.send).parameters) == ["settings", "text"], name
    assert name == module.KIND
    assert isinstance(module.TITLE, str)
    assert module.TITLE
    assert isinstance(module.ORDER, int)
    assert isinstance(module.MAX_LEN, int)
    assert module.MAX_LEN > 0
    assert all(isinstance(step, str) for step in module.STEPS)
    assert isinstance(module.NOTE, str)
    assert module.FIELDS
    assert all(isinstance(field, Field) for field in module.FIELDS)
    servers = getattr(module, "SERVER_FIELDS", ())
    assert isinstance(servers, tuple), name
    assert set(servers) <= {field.name for field in module.FIELDS}, name


def test_a_broken_messenger_module_is_skipped_with_its_reason(monkeypatch):
    import sys
    import types

    broken = types.ModuleType("tow.notifiers.zz_broken")
    broken.KIND = "zz_broken"  # no send, no FIELDS
    monkeypatch.setitem(sys.modules, "tow.notifiers.zz_broken", broken)
    real = pkgutil.iter_modules

    def with_broken(path):
        yield from real(path)
        yield pkgutil.ModuleInfo(None, "zz_broken", False)

    monkeypatch.setattr(pkgutil, "iter_modules", with_broken)
    registry._discover.cache_clear()
    try:
        assert "zz_broken" not in registry.kinds()
        assert any(name == "zz_broken" and "send" in reason for name, reason in registry.SKIPPED)
    finally:
        monkeypatch.undo()
        registry._discover.cache_clear()
