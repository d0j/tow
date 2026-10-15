"""No field TOW writes into config.yaml, state.json or the download history is named like a
credential: the restore point's check refuses such a field outside the encrypted secrets, and
every restore point, the update from Settings and every restore fail with it (1.31.0's mirror
``token``, 1.23.0's ``content_token``)."""

from __future__ import annotations

import ast
import typing
from pathlib import Path

import pytest

import tow
from tow import records
from tow.bundle.secret_scan import credential_name
from tow.undo import records as undo_records

SOURCE = Path(tow.__file__).parent

# Credential-named keys the code writes that never reach the scanned stores, each with where it
# goes. A key added here needs that proof; a key no longer written is taken out.
ALLOWED = {
    ("content.py", "token"): "a prepared torrent's id in the answer to the page (stored as content_ref)",
    ("storage/secrets.py", "token"): "the Fernet ciphertext of secrets.enc and secrets-undo.enc",
    ("protection/store.py", "cookies"): "a site clearance inside protection.enc (encrypted)",
    ("trackers/generic.py", "cookies"): "the keyword arguments of an HTTP request",
    ("trackers/generic.py", "cookies_by_origin"): "a site login's cookies in secrets.enc",
    ("web/routes_topic_login.py", "cookies_by_origin"): "a site login's cookies in secrets.enc",
    ("web/routes_clients.py", "password"): "a torrent client's login in secrets.enc",
    ("web/site_actions.py", "password"): "a site login in secrets.enc",
    ("web/site_store.py", "password"): "a site login in secrets.enc",
    ("web/services.py", "api_key"): "the catalog key of a protection provider's status on the page",
    ("undo/kinds.py", "password"): "an undo's log event; the export keeps only EXPORT_EVENT_KEYS",
}


def _typed_dicts(module) -> list[type]:
    return [value for value in vars(module).values() if isinstance(value, type) and typing.is_typeddict(value)]


@pytest.mark.parametrize("module", [records, undo_records], ids=lambda module: module.__name__)
def test_no_field_of_a_stored_record_is_named_like_a_credential(module):
    shapes = _typed_dicts(module)
    assert shapes, "the module declares the records it stores"
    named = sorted(
        f"{shape.__name__}.{field}"
        for shape in shapes
        for field in typing.get_type_hints(shape)
        if credential_name(field)
    )
    assert named == [], f"rename these fields (the backup check refuses them): {named}"


def _written_keys(tree: ast.AST) -> set[str]:
    """String keys of dict literals, ``x["k"] = ...``, ``setdefault("k")`` and ``update(k=...)``."""
    keys: set[str] = set()
    for node in ast.walk(tree):
        if isinstance(node, ast.Dict):
            keys.update(key.value for key in node.keys if isinstance(key, ast.Constant) and isinstance(key.value, str))
        elif isinstance(node, ast.Subscript) and isinstance(node.ctx, ast.Store):
            if isinstance(node.slice, ast.Constant) and isinstance(node.slice.value, str):
                keys.add(node.slice.value)
        elif isinstance(node, ast.Call) and isinstance(node.func, ast.Attribute):
            if node.func.attr == "setdefault" and node.args:
                first = node.args[0]
                if isinstance(first, ast.Constant) and isinstance(first.value, str):
                    keys.add(first.value)
            elif node.func.attr == "update":
                keys.update(keyword.arg for keyword in node.keywords if keyword.arg)
    return keys


def test_every_credential_named_key_the_code_writes_is_known_not_to_be_stored_in_plain():
    found: set[tuple[str, str]] = set()
    for path in SOURCE.rglob("*.py"):
        module = path.relative_to(SOURCE).as_posix()
        if module == "bundle/secret_scan.py":
            continue
        tree = ast.parse(path.read_text(encoding="utf-8"), filename=str(path))
        found.update((module, key) for key in _written_keys(tree) if credential_name(key))
    unknown = sorted(found - set(ALLOWED))
    assert unknown == [], (
        f"a credential-named key is written here: {unknown}. Give the field another name, or (only "
        "if it goes into an encrypted store or a page, never into config.yaml, state.json or the "
        "history) add it to ALLOWED with where it goes"
    )
    assert sorted(set(ALLOWED) - found) == [], "an ALLOWED key is no longer written: take it out"
