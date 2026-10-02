"""A name with a leading underscore belongs to its module: no other module of src/tow imports it.

A helper two modules share gets a public name (and, for the web, a home outside the route
modules: ``tow.web.views``, ``site_store``, ``site_form``). Private *modules* (``_context``,
``_common``) are fine to import within their package; their names follow the same rule.
"""

from __future__ import annotations

import ast
from pathlib import Path

SRC = Path(__file__).resolve().parents[1] / "src"

# (importing file, "module.name"): kept until the module's owner moves it - may only shrink.
ALLOWED: set[tuple[str, str]] = set()


def _module_of(path: Path) -> str:
    parts = list(path.relative_to(SRC).with_suffix("").parts)
    return ".".join(parts[:-1] if parts[-1] == "__init__" else parts)


def _is_module(dotted: str) -> bool:
    base = SRC.joinpath(*dotted.split("."))
    return base.with_suffix(".py").is_file() or (base / "__init__.py").is_file()


def _private(name: str) -> bool:
    return name.startswith("_") and not name.startswith("__")


def _crossings(path: Path) -> list[tuple[str, str, int]]:
    """(file, "module.name", line) for each private name of another module this file uses."""
    module = _module_of(path)
    package = module if path.name == "__init__.py" else module.rpartition(".")[0]
    tree = ast.parse(path.read_text(encoding="utf-8"))
    rel = path.relative_to(SRC).as_posix()
    found: list[tuple[str, str, int]] = []
    aliases: dict[str, str] = {}  # local name -> tow module it stands for
    for node in ast.walk(tree):
        if isinstance(node, ast.ImportFrom):
            base = node.module or ""
            if node.level:
                anchor = package.split(".")[: len(package.split(".")) - (node.level - 1)]
                base = ".".join([*anchor, *([base] if base else [])])
            if not base.startswith("tow"):
                continue
            for alias in node.names:
                full = f"{base}.{alias.name}"
                if _is_module(full):
                    aliases[alias.asname or alias.name] = full
                elif _private(alias.name) and base != module:
                    found.append((rel, full, node.lineno))
        elif isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.startswith("tow") and alias.asname:
                    aliases[alias.asname] = alias.name
    found.extend(
        (rel, f"{aliases[node.value.id]}.{node.attr}", node.lineno)
        for node in ast.walk(tree)
        if isinstance(node, ast.Attribute)
        and isinstance(node.value, ast.Name)
        and node.value.id in aliases
        and _private(node.attr)
        and aliases[node.value.id] != module
        and not _is_module(f"{aliases[node.value.id]}.{node.attr}")
    )
    return found


def test_no_module_uses_another_modules_private_names():
    found = [item for path in sorted(SRC.rglob("*.py")) for item in _crossings(path)]
    unexpected = sorted(f"{rel}:{line} uses {name}" for rel, name, line in found if (rel, name) not in ALLOWED)
    assert unexpected == [], "give the shared name a public name (or a shared module):\n" + "\n".join(unexpected)


def test_the_scan_sees_a_crossing(tmp_path, monkeypatch):
    package = tmp_path / "tow"
    package.mkdir()
    (package / "__init__.py").write_text("", encoding="utf-8")
    (package / "a.py").write_text("def _helper():\n    pass\n", encoding="utf-8")
    (package / "b.py").write_text("from tow.a import _helper\nfrom tow import a\n\na._helper()\n", encoding="utf-8")
    monkeypatch.setattr(f"{__name__}.SRC", tmp_path)
    assert _crossings(package / "b.py") == [("tow/b.py", "tow.a._helper", 1), ("tow/b.py", "tow.a._helper", 4)]
