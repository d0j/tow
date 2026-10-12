"""Which module may import which: the layers of src/tow (docs/architecture.md, "Modules").

Every import counts - at the top, inside a function (lazy), under ``TYPE_CHECKING``, in a
``try``, relative, and ``importlib.import_module("tow....")`` with a literal name - because each
one ties the importer to the imported module. Each rule has its own ``ALLOWED`` set: the
crossings that existed when the rule was written. It may only shrink: a crossing that is gone
must leave it (``test_every_allowed_crossing_still_exists``), a new one fails the rule.
"""

from __future__ import annotations

import ast
from collections.abc import Callable
from pathlib import Path

import pytest

from tow.clients.spec import HELPERS as CLIENT_HELPERS
from tow.notifiers.registry import HELPERS as MESSENGER_HELPERS

SRC = Path(__file__).resolve().parents[1] / "src"

# Leaf modules: no TOW logic above them. They import only each other and tow.i18n.
FOUNDATION = (
    "tow.clock",
    "tow.errors",
    "tow.events",
    "tow.jsonish",
    "tow.net_errors",
    "tow.net_guard",
    "tow.paths",
    "tow.platform",  # the package and its backends
    "tow.plugins",  # the scan the client and messenger registries share
    "tow.ratelimit",
    "tow.records",
    "tow.torrent",
    "tow.yaml_guard",
)
# The stores, their lock and journaled writes: below everything that decides what to store.
STORAGE = (
    "tow.journal",
    "tow.site_journal",
    "tow.store",
    *(f"tow.{p.stem}" for p in (SRC / "tow").glob("*_transaction.py")),
)
# What a torrent client or a messenger module must never reach: the code that drives them.
ORCHESTRATION = (
    "tow.adopt",
    "tow.bundle",
    "tow.check",
    "tow.cli",
    "tow.delivery",
    "tow.doctor",
    "tow.lifecycle",
    "tow.notify",
    "tow.progress",
    "tow.restore_points",
    "tow.snapshots",
    "tow.supervisor",
    "tow.undo",
    "tow.watchdog",
    "tow.web",
    "tow.web_update",
)
# Plugin families: every module of the package is a plugin except these helpers (for clients and
# messengers, the very list their registries skip).
FAMILIES: dict[str, set[str]] = {
    "tow.autostart": set(),
    "tow.clients": set(CLIENT_HELPERS),
    "tow.notifiers": set(MESSENGER_HELPERS),
    "tow.platform": {"locks"},
    "tow.trackers.presets": set(),
}
# The job table is data the schedule reads; it imports nothing from TOW itself.
SCHEDULE_MAY_IMPORT = {"tow.config", "tow.supervisor.jobs", "tow.topic_timers"}


def _within(module: str, *names: str) -> bool:
    return any(module == name or module.startswith(f"{name}.") for name in names)


def _plugins(src: Path = SRC) -> set[str]:
    found = set()
    for family, helpers in FAMILIES.items():
        folder = src.joinpath(*family.split("."))
        found |= {
            f"{family}.{p.stem}" for p in folder.glob("*.py") if not p.stem.startswith("_") and p.stem not in helpers
        }
    return found


PLUGINS = _plugins()


def _web_leaf(importer: str, imported: str) -> bool:
    return _within(imported, "tow.web") and not _within(importer, "tow.web")


def _cli_entry(importer: str, imported: str) -> bool:
    if _within(importer, "tow.cli"):
        return imported == "tow.cli"  # its modules import each other, never the package itself
    return _within(imported, "tow.cli") and importer != "tow.__main__"


def _plugins_below_orchestration(importer: str, imported: str) -> bool:
    return _within(importer, "tow.clients", "tow.notifiers") and _within(imported, *ORCHESTRATION)


def _plugins_apart(importer: str, imported: str) -> bool:
    return importer in PLUGINS and imported in PLUGINS


def _foundation_below(importer: str, imported: str) -> bool:
    return _within(importer, *FOUNDATION) and not (_within(imported, *FOUNDATION) or imported == "tow.i18n")


def _storage_below(importer: str, imported: str) -> bool:
    lower = _within(imported, *FOUNDATION, *STORAGE) or imported == "tow.i18n"
    return _within(importer, *STORAGE) and not lower


def _schedule_pure(importer: str, imported: str) -> bool:
    return importer == "tow.supervisor.schedule" and imported not in SCHEDULE_MAY_IMPORT


def _job_table_is_data(importer: str, imported: str) -> bool:
    return importer == "tow.supervisor.jobs"


def _check_through_package(importer: str, imported: str) -> bool:
    return imported.startswith("tow.check.") and not _within(importer, "tow.check")


RULES: dict[str, Callable[[str, str], bool]] = {
    "nothing outside tow.web imports tow.web": _web_leaf,
    "only tow.__main__ imports tow.cli": _cli_entry,
    "clients and messengers never import what drives them": _plugins_below_orchestration,
    "plugins do not import each other": _plugins_apart,
    "foundation imports only foundation and tow.i18n": _foundation_below,
    "storage imports only storage, foundation and tow.i18n": _storage_below,
    "supervisor.schedule imports only config, topic_timers and the job table": _schedule_pure,
    "supervisor.jobs imports nothing from TOW": _job_table_is_data,
    "outside tow.check only the package tow.check is imported": _check_through_package,
}

# (importer, imported) per rule: what existed when the rule came - may only shrink.
ALLOWED: dict[str, set[tuple[str, str]]] = {
    "nothing outside tow.web imports tow.web": set(),
    "only tow.__main__ imports tow.cli": set(),
    "clients and messengers never import what drives them": set(),
    "plugins do not import each other": set(),
    # The POSIX backend asks the autostart which service manager this system has.
    "foundation imports only foundation and tow.i18n": {("tow.platform.posix", "tow.autostart")},
    # The store validates a topic's selection and timer; the store transaction writes config.yaml.
    "storage imports only storage, foundation and tow.i18n": {
        ("tow.store", "tow.selection"),
        ("tow.store", "tow.topic_timers"),
        ("tow.store_transaction", "tow.config"),
    },
    "supervisor.schedule imports only config, topic_timers and the job table": set(),
    "supervisor.jobs imports nothing from TOW": set(),
    # The web's services expose the disk-space wait and the notices of a check.
    "outside tow.check only the package tow.check is imported": {
        ("tow.web.services", "tow.check.notices"),
        ("tow.web.services", "tow.check.space"),
    },
}


def _module_of(path: Path, src: Path) -> str:
    parts = list(path.relative_to(src).with_suffix("").parts)
    return ".".join(parts[:-1] if parts[-1] == "__init__" else parts)


def _is_module(dotted: str, src: Path) -> bool:
    base = src.joinpath(*dotted.split("."))
    return base.with_suffix(".py").is_file() or (base / "__init__.py").is_file()


def _literal_import(node: ast.Call) -> str | None:
    """``importlib.import_module("tow.x")`` or ``__import__("tow.x")`` with a literal name."""
    func = node.func
    named = (isinstance(func, ast.Attribute) and func.attr == "import_module") or (
        isinstance(func, ast.Name) and func.id in {"import_module", "__import__"}
    )
    if named and node.args and isinstance(node.args[0], ast.Constant) and isinstance(node.args[0].value, str):
        return node.args[0].value
    return None


def _imports(path: Path, src: Path = SRC) -> list[tuple[str, int]]:
    """(tow module, line) for every import of a TOW module in the file, at any depth."""
    module = _module_of(path, src)
    package = module if path.name == "__init__.py" else module.rpartition(".")[0]
    found: list[tuple[str, int]] = []
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            found += [(alias.name, node.lineno) for alias in node.names if _within(alias.name, "tow")]
        elif isinstance(node, ast.ImportFrom):
            base = node.module or ""
            if node.level:
                anchor = package.split(".")[: len(package.split(".")) - (node.level - 1)]
                base = ".".join([*anchor, *([base] if base else [])])
            if _within(base, "tow"):
                for alias in node.names:
                    full = f"{base}.{alias.name}"
                    found.append((full if _is_module(full, src) else base, node.lineno))
        elif isinstance(node, ast.Call) and (name := _literal_import(node)) and _within(name, "tow"):
            found.append((name, node.lineno))
    return [(imported, line) for imported, line in found if imported != module]


def _edges(src: Path = SRC) -> list[tuple[str, str, str]]:
    """(importer, imported, "file:line") for every import in src/tow."""
    return [
        (_module_of(path, src), imported, f"{path.relative_to(src).as_posix()}:{line}")
        for path in sorted((src / "tow").rglob("*.py"))
        for imported, line in _imports(path, src)
    ]


EDGES = _edges()


@pytest.mark.parametrize("rule", list(RULES))
def test_imports_follow_the_layers(rule):
    broken = RULES[rule]
    allowed = ALLOWED[rule]
    unexpected = sorted(
        f"{where}: {importer} imports {imported}"
        for importer, imported, where in EDGES
        if broken(importer, imported) and (importer, imported) not in allowed
    )
    assert unexpected == [], f"{rule}:\n" + "\n".join(unexpected)


def test_every_allowed_crossing_still_exists():
    assert set(ALLOWED) == set(RULES)
    present = {(importer, imported) for importer, imported, _where in EDGES}
    stale = sorted(
        f"{rule}: {importer} -> {imported}"
        for rule, pairs in ALLOWED.items()
        for importer, imported in pairs
        if (importer, imported) not in present or not RULES[rule](importer, imported)
    )
    assert stale == [], "gone or no longer a crossing; remove from ALLOWED (it may only shrink):\n" + "\n".join(stale)


def test_the_groups_name_real_modules():
    named = [*FOUNDATION, *STORAGE, *ORCHESTRATION, *FAMILIES, *SCHEDULE_MAY_IMPORT]
    named += [f"{family}.{helper}" for family, helpers in FAMILIES.items() for helper in helpers]
    assert [name for name in named if not _is_module(name, SRC)] == []
    assert "tow.check_transaction" in STORAGE
    assert {"tow.clients.deluge", "tow.notifiers.telegram", "tow.trackers.presets.rutracker"} <= PLUGINS
    assert {"tow.autostart.windows", "tow.platform.posix"} <= PLUGINS


def test_the_scan_sees_every_form_of_import(tmp_path):
    package = tmp_path / "tow"
    names = ("__init__", "m1", "m2", "m3", "m4", "m6", "m8", "m9", "m10", "m11", "m12", "m13", "sub/__init__", "sub/m5")
    for name in names:
        (package / f"{name}.py").parent.mkdir(parents=True, exist_ok=True)
        (package / f"{name}.py").write_text("", encoding="utf-8")
    scanned = package / "sub" / "scanned.py"
    scanned.write_text(
        "from __future__ import annotations\n"  # 1: not TOW
        "import tow.m1\n"  # 2
        "import tow.m2 as two\n"  # 3
        "from tow import m3\n"  # 4: a module of a package
        "from tow.m4 import name\n"  # 5: a name of a module
        "from . import m5\n"  # 6: relative
        "from ..m6 import other\n"  # 7: relative, one level up
        "from typing import TYPE_CHECKING\n"  # 8
        "if TYPE_CHECKING:\n"  # 9
        "    from tow.m8 import Hint\n"  # 10
        "try:\n"  # 11
        "    import tow.m9\n"  # 12
        "except ImportError:\n"  # 13
        "    pass\n"  # 14
        "def lazy():\n"  # 15
        "    from tow.m10 import late\n"  # 16
        "    import importlib\n"  # 17
        "    importlib.import_module('tow.m11')\n"  # 18
        "    return __import__('tow.m12')\n"  # 19
        "class Holder:\n"  # 20
        "    def method(self):\n"  # 21
        "        from tow import m13, version\n",  # 22
        encoding="utf-8",
    )
    assert sorted(_imports(scanned, tmp_path), key=lambda item: item[1]) == [
        ("tow.m1", 2),
        ("tow.m2", 3),
        ("tow.m3", 4),
        ("tow.m4", 5),
        ("tow.sub.m5", 6),
        ("tow.m6", 7),
        ("tow.m8", 10),
        ("tow.m9", 12),
        ("tow.m10", 16),
        ("tow.m11", 18),
        ("tow.m12", 19),
        ("tow.m13", 22),
        ("tow", 22),
    ]
    assert ("tow.sub.scanned", "tow.m10", "tow/sub/scanned.py:16") in _edges(tmp_path)


def test_a_rule_catches_its_crossing():
    assert _web_leaf("tow.check.run", "tow.web.services")
    assert not _web_leaf("tow.web.views", "tow.web.services")
    assert _cli_entry("tow.web.services", "tow.cli")
    assert not _cli_entry("tow.__main__", "tow.cli")
    assert _cli_entry("tow.web.services", "tow.cli.render")
    assert _cli_entry("tow.cli.commands.service", "tow.cli")
    assert not _cli_entry("tow.cli.commands.service", "tow.cli.render")
    assert _plugins_below_orchestration("tow.clients.managed", "tow.check")
    assert not _plugins_below_orchestration("tow.clients.managed", "tow.check_steps")
    assert _plugins_apart("tow.notifiers.telegram", "tow.notifiers.discord")
    assert not _plugins_apart("tow.notifiers.telegram", "tow.notifiers.base")
    assert _foundation_below("tow.clock", "tow.store")
    assert not _foundation_below("tow.platform.windows", "tow.platform")
    assert _storage_below("tow.journal", "tow.check")
    assert not _storage_below("tow.check_transaction", "tow.journal")
    assert _schedule_pure("tow.supervisor.schedule", "tow.clock")
    assert not _schedule_pure("tow.supervisor.schedule", "tow.supervisor.jobs")
    assert _job_table_is_data("tow.supervisor.jobs", "tow.config")
    assert _check_through_package("tow.cli", "tow.check.run")
    assert not _check_through_package("tow.check.run", "tow.check.topic")
