"""Every event kind the code can write to the log, derived from the code alone (an AST walk), with
where each is written - independent of what reads the log (the message matrix, the labels).

The writers are ``tow.log.log_event`` and ``append_event`` (the only writers of ``tow.jsonl``);
then, to a fixpoint, every function whose parameter flows into a writer's kind (a wrapper:
``_record``, ``record_event``, ``_change_clients``...), and a writer passed as a value
(``record=_record``: inside the callee's package, calls to ``record`` write; or
``run_in_threadpool(log_event, "kind", ...)``). A kind argument is resolved from a constant, both
sides of ``a if c else b`` / ``a or b``, ``+`` and f-strings, ``str(...)``, the assignments of a
local name in its function (and loop targets over literal sequences), a module-level constant
or the values of a module-level dict. A site that cannot be resolved so is either in
``MANUAL`` (each value re-checked as a literal in the evidence files) or makes ``scan`` fail:
a new dynamic kind cannot slip past the tests that use it.
"""

from __future__ import annotations

import ast
import re
from collections import defaultdict
from dataclasses import dataclass, field
from functools import cache
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
WRITERS = ("log_event", "append_event")
# (file, function, kind expression) -> (values, evidence files): sites whose kind comes from data
# that only literals elsewhere put there.
MANUAL: dict[tuple[str, str, str], tuple[tuple[str, ...], tuple[str, ...]]] = {
    # check/run.py: pending_reconcile_records holds (event_kind, fields) collected from
    # tow.progress: work.events.append(...) in reconcile.py, events.append(...) in presence.py.
    ("src/tow/check/run.py", "_run_check", "event_kind"): (
        ("new_file", "revision_updated", "file_completed", "episode_completed", "client_removed", "client_restored"),
        ("src/tow/progress/reconcile.py", "src/tow/progress/presence.py"),
    ),
    # undo/engine.py: ``name, fields = restore.event``; Restore(event=(...)) only in undo/kinds.py.
    ("src/tow/undo/engine.py", "_commit", "name"): (
        ("settings_access_undo", "settings_clients_undo"),
        ("src/tow/undo/kinds.py",),
    ),
}

Function = ast.FunctionDef | ast.AsyncFunctionDef
# A writer: the index of its kind argument, its keyword, the files it is called in (None: any),
# and whether it is a parameter that holds a writer (``record=_record``).
Writer = tuple[int, str, frozenset[str] | None, bool]


@dataclass
class Scan:
    kinds: dict[str, list[str]] = field(default_factory=dict)  # kind -> ["file:line", ...]
    unresolved: list[str] = field(default_factory=list)  # "file:line function: what"


def _callee(func: ast.AST) -> str | None:
    if isinstance(func, ast.Name):
        return func.id
    if isinstance(func, ast.Attribute):
        return func.attr
    return None


def _params(fn: Function) -> list[str]:
    return [arg.arg for arg in (*fn.args.posonlyargs, *fn.args.args, *fn.args.kwonlyargs)]


def _is(node: ast.AST | None, name: str) -> bool:
    return isinstance(node, ast.Name) and node.id == name


class _Scanner:
    def __init__(self) -> None:
        files = sorted((ROOT / "src" / "tow").rglob("*.py")) + sorted((ROOT / "scripts").glob("*.py"))
        self.sources = {path.relative_to(ROOT).as_posix(): path.read_text(encoding="utf-8") for path in files}
        self.trees = {name: ast.parse(text) for name, text in self.sources.items()}
        self.parents = {
            name: {child: node for node in ast.walk(tree) for child in ast.iter_child_nodes(node)}
            for name, tree in self.trees.items()
        }
        self.defined: dict[str, int] = defaultdict(int)
        self.constants: dict[str, dict[str, ast.AST]] = {}
        self.writers: dict[str, Writer] = dict.fromkeys(WRITERS, (0, "kind", None, False))
        self.sites: list[tuple[str, ast.Call, ast.AST | None, str]] = []
        for file, tree in self.trees.items():
            self.constants[file] = {}
            for stmt in tree.body:
                if isinstance(stmt, ast.Assign) and len(stmt.targets) == 1 and isinstance(stmt.targets[0], ast.Name):
                    self.constants[file][stmt.targets[0].id] = stmt.value
                elif isinstance(stmt, ast.AnnAssign) and isinstance(stmt.target, ast.Name) and stmt.value:
                    self.constants[file][stmt.target.id] = stmt.value
            for node in ast.walk(tree):
                if isinstance(node, Function):
                    self.defined[node.name] += 1
                elif isinstance(node, ast.ImportFrom):
                    for alias in node.names:
                        if alias.name in WRITERS and alias.asname:
                            self.writers[alias.asname] = self.writers[alias.name]

    def functions(self, file: str, node: ast.AST) -> list[Function]:
        """The functions around ``node``, innermost first."""
        out = []
        up = self.parents[file].get(node)
        while up is not None:
            if isinstance(up, Function):
                out.append(up)
            up = self.parents[file].get(up)
        return out

    def writer(self, file: str, name: str | None) -> Writer | None:
        found = self.writers.get(name or "")
        if found is None or (found[2] is not None and file not in found[2]):
            return None
        return found

    def kind_arguments(self, file: str, node: ast.Call, inside: list[Function]) -> list[ast.AST | None]:
        """The kind expressions a call writes (several when it passes a writer on)."""
        hits: list[ast.AST | None] = []
        found = self.writer(file, _callee(node.func))
        if found is not None and not (file == "src/tow/log.py" and inside and inside[0].name in WRITERS):
            by_name = [kw.value for kw in node.keywords if kw.arg == found[1]]
            positional = node.args[found[0]] if found[0] < len(node.args) else None
            hits.append(by_name[0] if by_name else None if isinstance(positional, ast.Starred) else positional)
        for i, arg in enumerate(node.args):  # f(log_event, "kind", ...)
            passed = self.writer(file, _callee(arg)) if isinstance(arg, ast.Name | ast.Attribute) else None
            if passed is not None and not passed[3] and i + 1 < len(node.args):
                hits.append(node.args[i + 1])
        return hits

    def passed_writers(self, file: str, node: ast.Call) -> bool:
        """f(record=_record): calls to ``record`` in that package write. True when one is new."""
        grew = False
        for kw in node.keywords:
            passed = self.writer(file, _callee(kw.value)) if isinstance(kw.value, ast.Name | ast.Attribute) else None
            if passed is not None and kw.arg and kw.arg not in self.writers:
                package = file.rsplit("/", 1)[0] + "/"
                scope = frozenset(f for f in self.trees if f.startswith(package) and f.count("/") == file.count("/"))
                self.writers[kw.arg] = (0, "kind", scope, True)
                grew = True
        return grew

    def wrapper(self, file: str, expr: ast.AST | None, inside: list[Function]) -> bool | None:
        """The kind is a parameter of an enclosing function, which then writes too: True when it
        is a new writer, False when a known one, None when the kind is no parameter."""
        if not isinstance(expr, ast.Name):
            return None
        for fn in inside:
            names = _params(fn)
            if expr.id not in names:
                continue
            keyword_only = expr.id in [arg.arg for arg in fn.args.kwonlyargs]
            index = 10**6 if keyword_only else names.index(expr.id) - (names[0] in ("self", "cls"))
            if fn.name in self.writers:
                return False
            private = fn.name.startswith("_") and not fn.name.startswith("__")
            nested = fn is not inside[-1] or self.defined[fn.name] > 1
            self.writers[fn.name] = (index, expr.id, frozenset({file}) if private or nested else None, False)
            return True
        return None

    def collect(self) -> bool:
        """One pass over every call; True when a new writer was found (pass again)."""
        grew = False
        self.sites.clear()
        for file, tree in self.trees.items():
            for node in ast.walk(tree):
                if not isinstance(node, ast.Call):
                    continue
                inside = self.functions(file, node)
                grew = self.passed_writers(file, node) or grew
                for expr in self.kind_arguments(file, node, inside):
                    new = self.wrapper(file, expr, inside)
                    if new is None:
                        self.sites.append((file, node, expr, inside[0].name if inside else "<module>"))
                    grew = bool(new) or grew
        return grew

    def resolve(self, file: str, expr: ast.AST | None, at: ast.AST, depth: int = 0) -> set[str] | None:
        """Every text ``expr`` can be, or None when the code alone does not tell."""
        if expr is None or depth > 6:
            return None
        if isinstance(expr, ast.Constant):
            return {expr.value} if isinstance(expr.value, str) else None
        if isinstance(expr, ast.IfExp | ast.BoolOp | ast.BinOp | ast.JoinedStr | ast.Call):
            return self.compound(file, expr, at, depth)
        if isinstance(expr, ast.Subscript) and isinstance(expr.value, ast.Name):
            table = self.constants[file].get(expr.value.id)
            return self.union(file, list(table.values), at, depth) if isinstance(table, ast.Dict) else None
        if isinstance(expr, ast.Name):
            return self.name(file, expr.id, at, depth)
        return None

    def union(self, file: str, parts: list[ast.AST | None], at: ast.AST, depth: int) -> set[str] | None:
        resolved = [self.resolve(file, part, at, depth + 1) for part in parts]
        if any(values is None for values in resolved):
            return None
        return set().union(*(values or set() for values in resolved))

    def compound(self, file: str, expr: ast.AST, at: ast.AST, depth: int) -> set[str] | None:
        if isinstance(expr, ast.IfExp):
            return self.union(file, [expr.body, expr.orelse], at, depth)
        if isinstance(expr, ast.BoolOp):
            return self.union(file, list(expr.values), at, depth)
        if isinstance(expr, ast.Call):  # str(x)
            is_str = _callee(expr.func) == "str" and len(expr.args) == 1
            return self.resolve(file, expr.args[0], at, depth + 1) if is_str else None
        parts: list[ast.AST] | None = None
        if isinstance(expr, ast.BinOp) and isinstance(expr.op, ast.Add):
            parts = [expr.left, expr.right]
        elif isinstance(expr, ast.JoinedStr):
            parts = [value.value if isinstance(value, ast.FormattedValue) else value for value in expr.values]
        if parts is None:
            return None
        out = {""}
        for part in parts:
            values = self.resolve(file, part, at, depth + 1)
            if values is None:
                return None
            out = {a + b for a in out for b in values}
        return out

    def name(self, file: str, name: str, at: ast.AST, depth: int) -> set[str] | None:
        """A local name: every assignment and loop over a literal sequence in its function; else
        a module-level constant."""
        for fn in self.functions(file, at):
            values: set[str] = set()
            found = False
            for node in ast.walk(fn):
                got = self.bound(file, name, node, depth)
                if got is False:
                    continue
                if got is None or got is True:
                    return None
                values |= got
                found = True
            if name in _params(fn):
                return None
            if found:
                return values
        return self.resolve(file, self.constants[file].get(name), at, depth + 1)

    def bound(self, file: str, name: str, node: ast.AST, depth: int) -> set[str] | bool | None:
        """What ``node`` binds ``name`` to: its values, None (unknown) or False (not bound here)."""
        if isinstance(node, ast.Assign):
            if any(isinstance(t, ast.Tuple) and any(_is(e, name) for e in t.elts) for t in node.targets):
                return None
            if any(_is(t, name) for t in node.targets):
                return self.resolve(file, node.value, node, depth + 1)
            return False
        if not isinstance(node, ast.For | ast.comprehension):
            return False
        target = node.target
        index = -1 if _is(target, name) else None
        if isinstance(target, ast.Tuple):
            index = next((i for i, e in enumerate(target.elts) if _is(e, name)), None)
        if index is None:
            return False
        sequences = [node.iter.body, node.iter.orelse] if isinstance(node.iter, ast.IfExp) else [node.iter]
        sequences = [self.constants[file].get(q.id, q) if isinstance(q, ast.Name) else q for q in sequences]
        if not all(isinstance(q, ast.Tuple | ast.List | ast.Set) for q in sequences):
            return None
        items: list[ast.AST | None] = [e for q in sequences for e in q.elts]  # type: ignore[attr-defined]
        if index >= 0:
            items = [e.elts[index] if isinstance(e, ast.Tuple | ast.List) else None for e in items]
        return self.union(file, items, node, depth)

    def manual(self, file: str, function: str, expr: ast.AST, result: Scan) -> set[str] | None:
        known = MANUAL.get((file, function, ast.get_source_segment(self.sources[file], expr) or ""))
        if known is None:
            return None
        for value in known[0]:
            pattern = "[\"']" + re.escape(value) + "[\"']"
            if not any(re.search(pattern, self.sources.get(evidence, "")) for evidence in known[1]):
                result.unresolved.append(f"{file} {function}: {value!r} is no literal in {known[1]}")
        return set(known[0])

    def run(self) -> Scan:
        for _ in range(20):
            if not self.collect():
                break
        result = Scan()
        kinds: dict[str, list[str]] = defaultdict(list)
        for file, call, expr, function in self.sites:
            values = self.resolve(file, expr, call)
            if values is None and expr is not None:
                values = self.manual(file, function, expr, result)
            if values is None:
                said = ast.get_source_segment(self.sources[file], expr) if expr is not None else "?"
                result.unresolved.append(f"{file}:{call.lineno} {function}: {said}")
                continue
            for value in values:
                kinds[value].append(f"{file}:{call.lineno}")
        result.kinds = dict(kinds)
        return result


@cache
def scan() -> Scan:
    """The kinds the code logs and where; ``unresolved`` names every site it could not read."""
    return _Scanner().run()
