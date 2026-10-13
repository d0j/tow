"""The update worker, its lock module and the updater run outside TOW's environment.

``src/tow/update_worker.py`` and ``src/tow/platform/locks.py`` are copied out of ``app/`` and run
by the install's base Python while the app is replaced; ``scripts/update.py`` (with
``scripts/root_files.py``) runs the same way (AGENTS.md). None of them may import anything but the
standard library - not TOW, not a dependency - or each other, at any depth.
"""

from __future__ import annotations

import ast
import sys
from pathlib import Path

import pytest

ROOT = Path(__file__).resolve().parents[1]
STANDALONE = (
    ROOT / "src" / "tow" / "update_worker.py",
    ROOT / "src" / "tow" / "platform" / "locks.py",
    ROOT / "src" / "tow" / "platform" / "_local_service_worker.py",
    ROOT / "scripts" / "update.py",
    ROOT / "scripts" / "root_files.py",
)
OWN = {path.stem for path in STANDALONE}


def _imported(path: Path) -> list[tuple[str, int]]:
    """(top-level module, line) of every import in the file; a relative one is "." plus its name."""
    found: list[tuple[str, int]] = []
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.Import):
            found += [(alias.name.split(".")[0], node.lineno) for alias in node.names]
        elif isinstance(node, ast.ImportFrom):
            found.append(("." * node.level + (node.module or "").split(".")[0], node.lineno))
        elif isinstance(node, ast.Call) and node.args and isinstance(node.args[0], ast.Constant):
            func = node.func
            name = func.attr if isinstance(func, ast.Attribute) else func.id if isinstance(func, ast.Name) else ""
            if name in {"import_module", "__import__"} and isinstance(node.args[0].value, str):
                found.append((node.args[0].value.split(".")[0], node.lineno))
    return found


@pytest.mark.parametrize("path", STANDALONE, ids=lambda path: path.name)
def test_imports_only_the_standard_library(path):
    foreign = sorted(
        f"{path.relative_to(ROOT).as_posix()}:{line} imports {name}"
        for name, line in _imported(path)
        if name not in sys.stdlib_module_names and name not in OWN
    )
    assert foreign == [], "standard library only (it runs outside TOW's environment):\n" + "\n".join(foreign)


def test_the_scan_sees_every_form_of_import(tmp_path):
    probe = tmp_path / "probe.py"
    probe.write_text(
        "import os, tow.paths\n"
        "from yaml import safe_load\n"
        "from . import sibling\n"
        "def lazy():\n"
        "    import httpx\n"
        "    import importlib\n"
        "    importlib.import_module('cryptography.fernet')\n"
        "    return __import__('fastapi')\n",
        encoding="utf-8",
    )
    assert sorted(_imported(probe), key=lambda item: (item[1], item[0])) == [
        ("os", 1),
        ("tow", 1),
        ("yaml", 2),
        (".", 3),
        ("httpx", 5),
        ("importlib", 6),
        ("cryptography", 7),
        ("fastapi", 8),
    ]
    assert {"os", "importlib"} <= sys.stdlib_module_names
    assert not {"tow", "yaml", ".", "httpx", "cryptography", "fastapi"} & sys.stdlib_module_names
