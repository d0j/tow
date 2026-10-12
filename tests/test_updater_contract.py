"""What the updater and the bundle builder read from the tree, read from the real tree.

``scripts/update.py`` reads the target's ``STATE_SCHEMA_VERSION`` from ``src/tow/store.py`` as
text, with a regular expression: a store.py it cannot read that way makes it skip the "can the
target read this data" refusal. ``scripts/build-bundle.py`` refuses a bundle without its
``REQUIRED`` files: a moved file there breaks only the release build.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
UPDATER = ROOT / "scripts" / "update.py"
BUILDER = ROOT / "scripts" / "build-bundle.py"


def _function(tree: ast.AST, name: str) -> ast.FunctionDef:
    return next(node for node in ast.walk(tree) if isinstance(node, ast.FunctionDef) and node.name == name)


def _schema_regex() -> re.Pattern[str]:
    """The pattern and flags of the ``re.search`` that refuse_unreadable_data reads the format with."""
    method = _function(ast.parse(UPDATER.read_text(encoding="utf-8")), "refuse_unreadable_data")
    for node in ast.walk(method):
        if (
            isinstance(node, ast.Call)
            and isinstance(node.func, ast.Attribute)
            and node.func.attr == "search"
            and node.args
            and isinstance(node.args[0], ast.Constant)
            and str(node.args[0].value).startswith("^STATE_SCHEMA_VERSION")
        ):
            flags = 0
            for arg in node.args[2:]:
                assert isinstance(arg, ast.Attribute), ast.unparse(arg)
                flags |= getattr(re, arg.attr)
            return re.compile(node.args[0].value, flags)
    raise AssertionError("refuse_unreadable_data no longer reads STATE_SCHEMA_VERSION with re.search")


def test_the_updater_reads_the_real_state_format():
    from tow import store

    match = _schema_regex().search((ROOT / "src" / "tow" / "store.py").read_text(encoding="utf-8"))
    assert match, "scripts/update.py cannot read STATE_SCHEMA_VERSION from src/tow/store.py"
    assert int(match[1]) == store.STATE_SCHEMA_VERSION


def _required() -> tuple[str, ...]:
    tree = ast.parse(BUILDER.read_text(encoding="utf-8"))
    for node in tree.body:
        if isinstance(node, ast.Assign) and any(isinstance(t, ast.Name) and t.id == "REQUIRED" for t in node.targets):
            value = ast.literal_eval(node.value)
            assert isinstance(value, tuple)
            return value
    raise AssertionError("scripts/build-bundle.py has no REQUIRED")


def test_every_app_file_the_bundle_requires_is_in_the_tree():
    app = [name.removeprefix("app/") for name in _required() if name.startswith("app/")]
    assert any(name.startswith("src/") for name in app)
    assert [name for name in app if not (ROOT / name).is_file()] == []
