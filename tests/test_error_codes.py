"""Every typed error the code raises: its text exists in every language and its class never
depends on the language (the audit of 1.17 found ~70 messages whose colour changed with it)."""

from __future__ import annotations

import ast
import functools
from pathlib import Path

import pytest

from tow import errors, i18n
from tow.log import error_class

SRC = Path(__file__).parents[1] / "src" / "tow"
# Calls whose first argument is an error code (a catalog key).
_CONSTRUCTORS = frozenset(
    {
        "TowError",
        "ClientError",
        "ClientSettingError",
        "ClientUnavailableError",
        "TrackerError",
        "TrackerSettingError",
        "SelectionError",
        "SelectionPendingError",
        "MirrorFetchError",
        "AuthConfigurationError",
        "ManagedClient._fail",
        "_fail",
    }
)
# Calls that take an error code in another place: (name, argument index or keyword).
_CODE_ARGUMENTS = {"_client_operation": 2, "_wait": 2, "_wait_for_ownership": ("visibility_error", "ownership_error")}


def _name(node: ast.Call) -> str:
    func = node.func
    return func.id if isinstance(func, ast.Name) else func.attr if isinstance(func, ast.Attribute) else ""


def _constant(node: ast.AST | None) -> str | None:
    return node.value if isinstance(node, ast.Constant) and isinstance(node.value, str) else None


def raised_codes() -> dict[str, set[str | None]]:
    """code -> the classes raise sites give it (None: the class comes from the code or the type)."""
    found: dict[str, set[str | None]] = {}
    for path in SRC.rglob("*.py"):
        for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
            if not isinstance(node, ast.Call):
                continue
            name = _name(node)
            codes: list[str | None] = []
            if name in _CONSTRUCTORS and node.args:
                codes.append(_constant(node.args[0]))
            where = _CODE_ARGUMENTS.get(name)
            if isinstance(where, int) and len(node.args) > where:
                codes.append(_constant(node.args[where]))
            elif isinstance(where, tuple):
                codes.extend(_constant(kw.value) for kw in node.keywords if kw.arg in where)
            cls = next((_constant(kw.value) for kw in node.keywords if kw.arg == "cls"), None)
            for code in codes:
                if code and "." in code:
                    found.setdefault(code, set()).add(cls)
    return found


@functools.cache
def _calls() -> dict[str, set[str | None]]:
    return raised_codes()


def test_the_scan_sees_the_raise_sites():
    codes = _calls()
    assert len(codes) > 100
    for code in ("selection.too_long", "tracker.no_download_link", "client.managed.not_visible", "check.low_disk"):
        assert code in codes
    assert "client.qbittorrent.webapi_too_old" in codes  # a plugin's own error


def test_every_error_code_has_its_text_in_every_language():
    missing = sorted(f"{code} ({lang})" for code in _calls() for lang in i18n.codes() if not i18n.has(code, lang))
    assert missing == [], "add these keys to every language file: " + ", ".join(missing)


@pytest.mark.parametrize("code", sorted(raised_codes()))
def test_an_errors_class_is_the_same_in_every_language(code):
    params = dict.fromkeys(("tracker", "error", "reason", "state", "path", "file", "topic", "status"), "x")
    for site_cls in _calls()[code]:
        error = errors.TowError(code, cls=site_cls, **params)
        seen = set()
        for lang in i18n.codes():
            i18n.use(lang)
            seen.add(error_class(error))
            seen.add(error_class(error.record()))
            if site_cls is None:  # a class from the code: a stored text with its code reads the same
                seen.add(error_class(str(error), code=code))
        assert seen == {error.error_class}, (code, site_cls, seen)


def test_the_class_table_names_real_codes():
    keys = i18n.keys("en")
    for entry, cls in errors.CLASSES.items():
        assert cls in errors.STATUS_CLASSES
        if entry.endswith("."):
            assert any(key.startswith(entry) for key in keys), entry
        else:
            assert entry in keys, entry
