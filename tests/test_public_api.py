"""The public names of the modules the rest of TOW builds on, frozen with their signatures.

A refactor that moves code between modules must keep these names working (a re-export is fine)
or change the callers in the same commit. The snapshot is ``tests/golden/public_api.json``: a
module's public names are its ``__all__``, else the names its own source binds at the top level
without a leading underscore (functions, classes, constants); a class adds its own public
methods as ``Class.method``. Each name maps to ``inspect.signature`` for a callable, else to its
kind. After an intended change, regenerate it and review the diff:

    uv run --frozen python tests/test_public_api.py
"""

from __future__ import annotations

import ast
import dataclasses
import importlib
import inspect
import json
import re
import sys
from pathlib import Path
from types import ModuleType
from typing import Any

SNAPSHOT = Path(__file__).resolve().parent / "golden" / "public_api.json"
MODULES = (
    "tow.bundle",
    "tow.cli",
    "tow.clients.spec",
    "tow.notifiers",
    "tow.progress",
    "tow.snapshots",
    "tow.store",
    "tow.supervisor",
    "tow.supervisor.core",
    "tow.supervisor.schedule",
)


def _bound_at_top(statements: list[ast.stmt]) -> set[str]:
    """Names the statements bind at module level (also inside a top-level if/try), imports aside."""
    names: set[str] = set()
    for node in statements:
        if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef)):
            names.add(node.name)
        elif isinstance(node, ast.Assign):
            for target in node.targets:
                names |= {n.id for n in ast.walk(target) if isinstance(n, ast.Name)}
        elif isinstance(node, (ast.AnnAssign, ast.AugAssign)) and isinstance(node.target, ast.Name):
            names.add(node.target.id)
        elif isinstance(node, ast.TypeAlias) and isinstance(node.name, ast.Name):
            names.add(node.name.id)
        elif isinstance(node, (ast.If, ast.Try, ast.With)):
            nested = [*node.body, *getattr(node, "orelse", []), *getattr(node, "finalbody", [])]
            for handler in getattr(node, "handlers", []):
                nested += handler.body
            names |= _bound_at_top(nested)
    return names


def public_names(module: ModuleType) -> list[str]:
    declared = getattr(module, "__all__", None)
    if declared is not None:
        return sorted(declared)
    source = Path(inspect.getfile(module)).read_text(encoding="utf-8")
    return sorted(name for name in _bound_at_top(ast.parse(source).body) if not name.startswith("_"))


def _stable(text: str) -> str:
    """The same text on every OS and run: no addresses, one spelling of a path."""
    text = re.sub(r" at 0x[0-9A-Fa-f]+", " at 0x...", text)
    return re.sub(r"\b(?:Windows|Posix)Path\(", "Path(", text)


def _signature(obj: Any) -> str | None:
    try:
        return _stable(str(inspect.signature(obj)))
    except TypeError, ValueError:
        return None


def describe(obj: Any) -> str:
    if inspect.ismodule(obj):
        return "module"
    if inspect.isclass(obj):
        signature = _signature(obj)
        return f"class{signature}" if signature is not None else "class"
    if callable(obj):
        signature = _signature(obj)
        return f"def{signature}" if signature is not None else f"callable {type(obj).__name__}"
    return f"value {type(obj).__name__}"


def _member(cls: type, attr: str) -> str | None:
    """A public method or property of the class itself (not a dataclass field's default)."""
    raw = inspect.getattr_static(cls, attr, None)
    if isinstance(raw, property):
        return "property"
    if isinstance(raw, (staticmethod, classmethod)) or inspect.isfunction(raw):
        return describe(getattr(cls, attr))
    return None


def _class_members(name: str, cls: type) -> dict[str, str]:
    fields = {field.name for field in dataclasses.fields(cls)} if dataclasses.is_dataclass(cls) else set()
    members = {}
    for attr in vars(cls):
        if not attr.startswith("_") and attr not in fields and (kind := _member(cls, attr)) is not None:
            members[f"{name}.{attr}"] = kind
    return members


def module_api(module: ModuleType) -> dict[str, str]:
    api: dict[str, str] = {}
    for attr in public_names(module):
        obj = getattr(module, attr)
        api[attr] = describe(obj)
        if inspect.isclass(obj) and obj.__module__ == module.__name__:
            api.update(_class_members(attr, obj))
    return dict(sorted(api.items()))


def current() -> dict[str, dict[str, str]]:
    return {name: module_api(importlib.import_module(name)) for name in MODULES}


def _render(api: dict[str, dict[str, str]]) -> str:
    return json.dumps(api, indent=1, ensure_ascii=False, sort_keys=True) + "\n"


def _resolve(module: ModuleType, attr: str) -> str | None:
    """The description of a frozen name now, wherever it is defined; None: it is gone."""
    head, _, member = attr.partition(".")
    if not hasattr(module, head):
        return None
    obj = getattr(module, head)
    return (_member(obj, member) if inspect.isclass(obj) else None) if member else describe(obj)


def problems(module: ModuleType, frozen: dict[str, str]) -> list[str]:
    name = module.__name__
    found = []
    for attr, was in sorted(frozen.items()):
        now = _resolve(module, attr)
        if now is None:
            found.append(f"{name}.{attr} is gone (was {was})")
        elif now != was:
            found.append(f"{name}.{attr} changed:\n    was {was}\n    now {now}")
    found += [f"{name}.{attr} is new: {kind}" for attr, kind in module_api(module).items() if attr not in frozen]
    return found


def test_public_api_is_the_snapshot():
    frozen = json.loads(SNAPSHOT.read_text(encoding="utf-8"))
    assert sorted(frozen) == sorted(MODULES), "the snapshot covers other modules: regenerate it"
    found = [item for name in MODULES for item in problems(importlib.import_module(name), frozen[name])]
    assert found == [], (
        "the public API changed; if intended, regenerate and review the diff "
        "(uv run --frozen python tests/test_public_api.py):\n" + "\n".join(found)
    )


def test_the_top_level_scan_sees_every_binding():
    tree = ast.parse(
        "import os\n"
        "from typing import Any\n"
        "A = 1\n"
        "B, (C, D) = 2, (3, 4)\n"
        "E: int = 5\n"
        "type F = int\n"
        "def g(): H = 1\n"
        "class I: J = 1\n"
        "if A:\n    K = 1\nelse:\n    L = 1\n"
        "try:\n    M = 1\nexcept ImportError:\n    N = 1\nfinally:\n    O = 1\n"
    )
    assert _bound_at_top(tree.body) == set("ABCDEFgIKLMNO")


def test_a_frozen_name_moved_and_imported_back_still_counts():
    def run(x: int) -> int:
        return x

    @dataclasses.dataclass
    class Job:
        name: str
        hook: Any = lambda: None

        def go(self) -> None:
            pass

    module = ModuleType("tow.fake")
    module.__all__ = ["Job"]  # type: ignore[attr-defined]
    module.run = run  # type: ignore[attr-defined]  # moved away, re-exported: no longer in __all__
    module.Job = Job  # type: ignore[attr-defined]
    Job.__module__ = "tow.fake"
    frozen = {"run": "def(x: 'int') -> 'int'", "Job": describe(Job), "Job.go": "def(self) -> 'None'"}
    assert problems(module, frozen) == []
    assert module_api(module) == {"Job": describe(Job), "Job.go": "def(self) -> 'None'"}  # no field default
    del module.run  # type: ignore[attr-defined]
    assert problems(module, {**frozen, "Job.stop": "def(self)"}) == [
        "tow.fake.Job.stop is gone (was def(self))",
        "tow.fake.run is gone (was def(x: 'int') -> 'int')",
    ]


def test_the_description_is_the_same_on_every_os():
    assert _stable("(path: 'Path' = WindowsPath('a'), x=<object object at 0x01AF>)") == _stable(
        "(path: 'Path' = PosixPath('a'), x=<object object at 0x7f00>)"
    )


if __name__ == "__main__":
    SNAPSHOT.parent.mkdir(exist_ok=True)
    SNAPSHOT.write_text(_render(current()), encoding="utf-8", newline="\n")
    print(f"wrote {SNAPSHOT.relative_to(Path.cwd()) if SNAPSHOT.is_relative_to(Path.cwd()) else SNAPSHOT}")
    sys.exit(0)
