"""A test that replaces a module's name replaces something the code reads at run time.

``monkeypatch.setattr("tow.m.name", ...)``, ``monkeypatch.setattr(m, "name", ...)`` and
``setattr(m, "name", ...)`` on a TOW module change ``m.name``. That is live only when some code
looks the name up there when it runs: ``m`` reads it as a global inside a function, or another
module reads ``m.name`` (or imports it lazily, inside a function). Otherwise - the name is only
copied by a top-level ``from m import name``, or read at import time - the patch replaces nothing,
and a test that relies on it guards nothing. Exceptions that existed when this came are in
``ALLOWED``, which may only shrink. Targets built at run time (f-strings, variables) are not
checked; class attributes always are live (looked up on the class).
"""

from __future__ import annotations

import ast
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
SRC = ROOT / "src"
TESTS = ROOT / "tests"

# (test file, "module.name"): what replaced nothing when this came - may only shrink.
ALLOWED: dict[tuple[str, str], str] = {
    # A tripwire: fails if a backup-folder check ever probes with a write. Nothing in
    # tow.locations calls check_writable (only tow.web.services, under another name).
    ("tests/test_windows_device_paths.py", "tow.locations.check_writable"): "tripwire, never called in tow.locations",
    # tow.notifiers imports flush from the outbox at the top; the same test also patches
    # tow.notifiers.flush, which is the one send_all reads.
    ("tests/test_notifiers.py", "tow.notifiers.outbox.flush"): "redundant with tow.notifiers.flush",
}


def _is_module(dotted: str, src: Path) -> bool:
    base = src.joinpath(*dotted.split("."))
    return base.with_suffix(".py").is_file() or (base / "__init__.py").is_file()


def _module_of(path: Path, src: Path) -> str:
    parts = list(path.relative_to(src).with_suffix("").parts)
    return ".".join(parts[:-1] if parts[-1] == "__init__" else parts)


def _aliases(nodes: list[ast.AST], src: Path, package: str = "") -> dict[str, str]:
    """Local name -> TOW module it stands for, from the imports among the nodes."""
    found: dict[str, str] = {}
    for node in nodes:
        if isinstance(node, ast.Import):
            for alias in node.names:
                if alias.name.split(".")[0] == "tow":
                    found[alias.asname or "tow"] = alias.name if alias.asname else "tow"
        elif isinstance(node, ast.ImportFrom):
            base = node.module or ""
            if node.level:
                anchor = package.split(".")[: len(package.split(".")) - (node.level - 1)]
                base = ".".join([*anchor, *([base] if base else [])])
            if base.split(".")[0] == "tow":
                for alias in node.names:
                    if _is_module(f"{base}.{alias.name}", src):
                        found[alias.asname or alias.name] = f"{base}.{alias.name}"
    return found


def _dotted(expr: ast.expr) -> str | None:
    if isinstance(expr, ast.Name):
        return expr.id
    if isinstance(expr, ast.Attribute) and (head := _dotted(expr.value)):
        return f"{head}.{expr.attr}"
    return None


def _module_named(expr: ast.expr, aliases: dict[str, str], src: Path) -> str | None:
    text = _dotted(expr)
    if not text:
        return None
    first, _, rest = text.partition(".")
    if first not in aliases:
        return None
    full = aliases[first] + (f".{rest}" if rest else "")
    return full if _is_module(full, src) else None


def _split(target: str, src: Path) -> tuple[str, str] | None:
    """ "tow.m.name" -> ("tow.m", "name"); None for a class attribute or no TOW module."""
    parts = target.split(".")
    for cut in range(len(parts) - 1, 0, -1):
        if _is_module(".".join(parts[:cut]), src):
            rest = parts[cut:]
            return (".".join(parts[:cut]), rest[0]) if len(rest) == 1 else None
    return None


def _scopes(tree: ast.Module) -> list[tuple[list[ast.AST], list[ast.AST]]]:
    """(the nodes of a scope, the imports it sees): the module, then every top-level function and
    method with everything inside it (its imports over the module's)."""
    top = [node for node in tree.body if not isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef, ast.ClassDef))]
    module_nodes = [inner for node in top for inner in ast.walk(node)]
    scopes = [(module_nodes, module_nodes)]
    functions = [node for node in tree.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))]
    for cls in (node for node in tree.body if isinstance(node, ast.ClassDef)):
        functions += [node for node in cls.body if isinstance(node, (ast.FunctionDef, ast.AsyncFunctionDef))]
    for function in functions:
        nodes = list(ast.walk(function))
        scopes.append((nodes, module_nodes + nodes))
    return scopes


def patch_targets(tests: Path, src: Path) -> list[tuple[str, int, str, str]]:
    """(test file, line, module, name) for every patch of a TOW module's name in the tests."""
    found = []
    for path in sorted(tests.rglob("*.py")):
        tree = ast.parse(path.read_text(encoding="utf-8"))
        rel = path.relative_to(tests.parent).as_posix()
        for nodes, imports in _scopes(tree):
            aliases = _aliases(imports, src)
            found += [
                (rel, node.lineno, *target)
                for node in nodes
                if isinstance(node, ast.Call) and (target := _target(node, aliases, src))
            ]
    return found


def _target(call: ast.Call, aliases: dict[str, str], src: Path) -> tuple[str, str] | None:
    """(module, name) a setattr call patches, when both are written out."""
    func, args = call.func, call.args
    patcher = isinstance(func, ast.Attribute) and func.attr == "setattr"
    if not (patcher or (isinstance(func, ast.Name) and func.id == "setattr")) or not args:
        return None
    first = args[0]
    if patcher and len(args) == 2 and isinstance(first, ast.Constant) and isinstance(first.value, str):
        return _split(first.value, src)
    if len(args) >= 3 and isinstance(args[1], ast.Constant) and isinstance(args[1].value, str):
        module = _module_named(first, aliases, src)
        return (module, args[1].value) if module else None
    return None


class _Reads(ast.NodeVisitor):
    """What the code looks up when it runs: names inside function bodies, ``m.name`` there, and
    names imported inside a function. Defaults and decorators run at definition time."""

    def __init__(self, aliases: dict[str, str], src: Path) -> None:
        self.aliases, self.src = aliases, src
        self.depth = 0
        self.globals: set[str] = set()
        self.attributes: set[tuple[str, str]] = set()

    def _function(self, node: ast.FunctionDef | ast.AsyncFunctionDef | ast.Lambda) -> None:
        for early in [*getattr(node, "decorator_list", []), *node.args.defaults, *node.args.kw_defaults]:
            if early is not None:
                self.visit(early)
        self.depth += 1
        for statement in node.body if isinstance(node.body, list) else [node.body]:
            self.visit(statement)
        self.depth -= 1

    visit_FunctionDef = visit_AsyncFunctionDef = visit_Lambda = _function

    def visit_ImportFrom(self, node: ast.ImportFrom) -> None:
        if self.depth and node.module and not node.level and node.module.split(".")[0] == "tow":
            for alias in node.names:
                if not _is_module(f"{node.module}.{alias.name}", self.src):
                    self.attributes.add((node.module, alias.name))

    def visit_Name(self, node: ast.Name) -> None:
        if self.depth and isinstance(node.ctx, ast.Load):
            self.globals.add(node.id)

    def visit_Attribute(self, node: ast.Attribute) -> None:
        if (
            self.depth
            and isinstance(node.ctx, ast.Load)
            and (module := _module_named(node.value, self.aliases, self.src))
        ):
            self.attributes.add((module, node.attr))
        self.generic_visit(node)

    def visit_Call(self, node: ast.Call) -> None:
        func, args = node.func, node.args
        if self.depth and isinstance(func, ast.Name) and func.id == "getattr" and len(args) >= 2:
            module = _module_named(args[0], self.aliases, self.src)
            if module and isinstance(args[1], ast.Constant) and isinstance(args[1].value, str):
                self.attributes.add((module, args[1].value))
        self.generic_visit(node)


def runtime_reads(src: Path) -> tuple[dict[str, set[str]], set[tuple[str, str]]]:
    """(module -> globals its functions read, every (module, name) read as module.name)."""
    own: dict[str, set[str]] = {}
    attributes: set[tuple[str, str]] = set()
    for path in sorted((src / "tow").rglob("*.py")):
        module = _module_of(path, src)
        package = module if path.name == "__init__.py" else module.rpartition(".")[0]
        tree = ast.parse(path.read_text(encoding="utf-8"))
        reads = _Reads(_aliases(list(ast.walk(tree)), src, package), src)
        reads.visit(tree)
        own[module] = reads.globals
        attributes |= reads.attributes
    return own, attributes


def vacuous(tests: Path, src: Path) -> dict[tuple[str, str], list[int]]:
    """(test file, "module.name") -> lines of patches nothing reads at run time."""
    own, attributes = runtime_reads(src)
    found: dict[tuple[str, str], list[int]] = {}
    for rel, line, module, name in patch_targets(tests, src):
        if name not in own.get(module, set()) and (module, name) not in attributes:
            found.setdefault((rel, f"{module}.{name}"), []).append(line)
    return found


def test_every_patched_module_name_is_read_at_run_time():
    dead = vacuous(TESTS, SRC)
    unexpected = sorted(
        f"{rel}:{lines[0]} patches {target}" for (rel, target), lines in dead.items() if (rel, target) not in ALLOWED
    )
    assert unexpected == [], (
        "nothing reads this name at run time, so the patch changes nothing: patch the module that "
        "reads it (where it is looked up), or read it through its module:\n" + "\n".join(unexpected)
    )
    stale = sorted(f"{rel}: {target}" for rel, target in ALLOWED if (rel, target) not in dead)
    assert stale == [], "live now (or gone); remove from ALLOWED (it may only shrink):\n" + "\n".join(stale)


def test_the_scan_finds_many_patches():
    targets = patch_targets(TESTS, SRC)
    assert len(targets) > 1000
    assert ("tests/conftest.py", "tow.bundle", "KDF_ITERATIONS") in {(rel, m, n) for rel, _line, m, n in targets}


def test_the_export_cost_patch_of_the_test_guard_is_live():
    """tests/conftest.py lowers tow.bundle.KDF_ITERATIONS: the export functions read it when they run."""
    own, _attributes = runtime_reads(SRC)
    assert "KDF_ITERATIONS" in own["tow.bundle"]
    assert ("tests/conftest.py", "tow.bundle.KDF_ITERATIONS") not in vacuous(TESTS, SRC)


def test_the_scan_tells_live_from_vacuous(tmp_path):
    src, tests = tmp_path / "src", tmp_path / "tests"
    (src / "tow" / "sub").mkdir(parents=True)
    tests.mkdir()
    for name in ("__init__", "sub/__init__"):
        (src / "tow" / f"{name}.py").write_text("", encoding="utf-8")
    (src / "tow" / "a.py").write_text(
        "TOP = 1\n"
        "COPIED = TOP\n"  # read at import time only
        "def f(x=DEFAULT if False else 0):\n"  # a default: definition time
        "    return INNER\n",
        encoding="utf-8",
    )
    (src / "tow" / "sub" / "b.py").write_text(
        "from tow import a\n"
        "from tow.a import TOP\n"  # a copy at import: patching a.TOP does not reach it
        "def g():\n"
        "    from tow.a import LAZY\n"
        "    return a.ATTR, getattr(a, 'BY_NAME'), LAZY\n",
        encoding="utf-8",
    )
    (tests / "test_x.py").write_text(
        "import tow.a\n"
        "from tow import a\n"
        "def test_one(monkeypatch):\n"
        "    monkeypatch.setattr('tow.a.INNER', 1)\n"  # 4 live
        "    monkeypatch.setattr(a, 'ATTR', 1)\n"  # 5 live
        "    setattr(tow.a, 'BY_NAME', 1)\n"  # 6 live
        "    monkeypatch.setattr('tow.a.LAZY', 1)\n"  # 7 live
        "    monkeypatch.setattr('tow.a.TOP', 1)\n"  # 8 vacuous
        "    monkeypatch.setattr(a, 'DEFAULT', 1)\n"  # 9 vacuous
        "    monkeypatch.setattr('tow.a.f.__doc__', 'x')\n"  # a function's attribute: not a module name
        "def test_two(monkeypatch):\n"
        "    from tow.sub import b as a\n"  # this test's own 'a'
        "    monkeypatch.setattr(a, 'TOP', 2)\n"  # 13 vacuous (b.TOP)
        "    monkeypatch.setattr(f'tow.a.{1}', 2)\n",  # built at run time: not checked
        encoding="utf-8",
    )
    assert [line for _rel, line, _m, _n in patch_targets(tests, src)] == [4, 5, 6, 7, 8, 9, 13]
    assert vacuous(tests, src) == {
        ("tests/test_x.py", "tow.a.TOP"): [8],
        ("tests/test_x.py", "tow.a.DEFAULT"): [9],
        ("tests/test_x.py", "tow.sub.b.TOP"): [13],
    }
