"""Edit conflicts: the values a form showed when it was rendered (its *base*) and the
three-way merge of its save with what is saved now.

A form carries its base in a hidden field ``base``: the form's editable values, normalized,
bound to the kind of form and the entity it edits. A save changes only what the owner changed
(the submitted value differs from the base) and applies it to what is saved *now*, so another
device's edit of another field survives. A field changed on both sides to different values is a
conflict: nothing is saved, and the form comes back with the owner's draft (never a password)
and both values, until the owner chooses one (``resolve_<group>``). A save without a valid base
(a page of an older TOW, a damaged or hand-made form) is refused: "reload this form".

Fields that depend on each other (a client's address and its password, a site's mirrors and its
sign-in mirrors, a selection's mode and rule) form one *group*, which merges as one value.

Never a stored secret in a base: a password or a token is an opaque comparison token
(``secret_token``) - a keyed digest whose key lives only in this process, so it is useless for
guessing and expires when the server restarts. A secret field is submitted as an intent: keep
(empty), replace (typed) or remove (``<field>_intent=remove``).

Nothing here touches the disk: the base is in the page, a refused draft in this process's memory
(as a refused add's draft, ``tow.web.views``) behind a random token in the address.
"""

from __future__ import annotations

import hashlib
import hmac
import json
import secrets
from collections.abc import Callable, Mapping
from dataclasses import dataclass, field
from typing import Any

from fastapi import Depends, Request
from fastapi.responses import RedirectResponse

from tow.web.messages import TokenStore, flash_redirect

SCHEMA = 1
MAX_BASE_CHARS = 16_384
MAX_VALUE_CHARS = 4_096
MAX_LIST_ITEMS = 64
CHOICES = ("current", "mine")

# Secret comparison tokens: keyed by this process only (never written anywhere).
_KEY = secrets.token_bytes(32)
_KEY_ID = secrets.token_hex(4)

_CONFLICTS = TokenStore(ttl=900, limit=64)

Value = str | int | bool | None | tuple[str, ...]


@dataclass(frozen=True)
class Spec:
    """One kind of form: its groups of fields (a group merges as one value), the fields that are
    secret comparison tokens, and how a field compares (default: equality)."""

    kind: str
    groups: Mapping[str, tuple[str, ...]]
    secret: frozenset[str] = frozenset()
    same: Mapping[str, Callable[[Any, Any], bool]] = field(default_factory=dict)

    @property
    def fields(self) -> tuple[str, ...]:
        return tuple(name for names in self.groups.values() for name in names)

    def group_same(self, group: str, left: Mapping[str, Any], right: Mapping[str, Any]) -> bool:
        return all(self.same.get(name, _equal)(left.get(name), right.get(name)) for name in self.groups[group])


def _equal(left: Any, right: Any) -> bool:
    return bool(left == right)


@dataclass(frozen=True)
class Base:
    """A decoded base: the values the form showed and the groups still waiting for the owner's
    choice (a conflict shown with this base)."""

    kind: str
    ident: str
    values: dict[str, Value]
    pending: tuple[str, ...] = ()


@dataclass(frozen=True)
class Merge:
    """A save merged with what is saved now: ``values`` - each field as the save means it (the
    owner's value for a group they changed, the current one otherwise); ``changed`` - the groups
    the save changes; ``conflicts`` - the groups changed on both sides (or still unresolved)."""

    values: dict[str, Any]
    changed: frozenset[str]
    conflicts: tuple[str, ...]


def secret_token(kind: str, ident: str, name: str, value: object) -> str:
    """An opaque token of a stored secret (or of none) for one form's field: equal tokens mean
    the same secret; the token says nothing about the secret without this process's key."""
    stored = value if isinstance(value, str) and value else None
    message = json.dumps([SCHEMA, kind, ident, name, stored], ensure_ascii=False).encode("utf-8")
    return f"{_KEY_ID}.{hmac.new(_KEY, message, hashlib.sha256).hexdigest()[:32]}"


def digest(value: object) -> str:
    """A short stable digest of a JSON-able value (a selection's file list): equal only when the
    values are equal."""
    raw = json.dumps(value, sort_keys=True, ensure_ascii=False, separators=(",", ":"), default=str)
    return hashlib.sha256(raw.encode("utf-8")).hexdigest()[:32]


def _plain(value: Any) -> Value:
    if isinstance(value, (list, tuple)):
        return tuple(str(item) for item in value)
    if value is None or isinstance(value, (bool, int, str)):
        return value
    return str(value)


def encode(spec: Spec, ident: str, values: Mapping[str, Any], pending: tuple[str, ...] = ()) -> str:
    """The hidden ``base`` field's value for a form of ``spec`` about ``ident``."""
    fields = {name: _plain(values.get(name)) for name in spec.fields}
    record = {"v": SCHEMA, "k": spec.kind, "id": ident, "f": fields, "p": list(pending)}
    return json.dumps(record, ensure_ascii=False, separators=(",", ":"), default=list)


def _valid_value(value: Any) -> bool:
    if value is None or isinstance(value, (bool, int)):
        return True
    if isinstance(value, str):
        return len(value) <= MAX_VALUE_CHARS
    if isinstance(value, list):
        return len(value) <= MAX_LIST_ITEMS and all(
            isinstance(item, str) and len(item) <= MAX_VALUE_CHARS for item in value
        )
    return False


def decode(spec: Spec, ident: str, raw: str | None) -> Base | None:
    """The base a save sent, or None when it is missing, damaged, about another form or entity,
    of another schema, or holds a secret token this process did not issue (expired)."""
    if not raw or len(raw) > MAX_BASE_CHARS:
        return None
    try:
        record = json.loads(raw)
    except ValueError, RecursionError:
        return None
    if not isinstance(record, dict) or set(record) != {"v", "k", "id", "f", "p"}:
        return None
    if record["v"] != SCHEMA or record["k"] != spec.kind or record["id"] != ident:
        return None
    values, pending = record["f"], record["p"]
    if not isinstance(values, dict) or set(values) != set(spec.fields):
        return None
    if not all(_valid_value(value) for value in values.values()):
        return None
    if not isinstance(pending, list) or not all(isinstance(group, str) and group in spec.groups for group in pending):
        return None
    for name in spec.secret:
        token = values[name]
        if not isinstance(token, str) or not token.startswith(f"{_KEY_ID}."):
            return None
    plain = {name: _plain(value) for name, value in values.items()}
    return Base(spec.kind, ident, plain, tuple(dict.fromkeys(pending)))


def refuse_missing(spec: Spec, ident: str, current: Mapping[str, Any]) -> Base | None:
    """A save without a base is refused (the form must be reloaded)."""
    return None


# What a save without any base gets: refused. Kept as a module name so a test can stand in for a
# form opened just before its save (the suite's older form posts send no base).
_missing: Callable[[Spec, str, Mapping[str, Any]], Base | None] = refuse_missing


def read(spec: Spec, ident: str, raw: str | None, current: Mapping[str, Any]) -> Base | None:
    """The save's base (None: refuse, "reload this form"). A route called as a function (not by a
    request) sends no form at all: that is no base either."""
    if not isinstance(raw, str) or not raw:
        return _missing(spec, ident, current)
    return decode(spec, ident, raw)


def merge(
    spec: Spec,
    base: Base,
    current: Mapping[str, Any],
    submitted: Mapping[str, Any],
    resolutions: Mapping[str, str] | None = None,
) -> Merge:
    """The three-way merge, group by group. ``submitted`` holds only the fields the form sent
    (an omitted field is unchanged); a group pending since a conflict needs the owner's choice:
    ``current`` keeps what is saved, ``mine`` saves the owner's value."""
    resolutions = resolutions if isinstance(resolutions, Mapping) else {}
    values: dict[str, Any] = {}
    changed: set[str] = set()
    conflicts: list[str] = []
    for group, names in spec.groups.items():
        mine = {name: (submitted[name] if name in submitted else base.values[name]) for name in names}
        if group in base.pending:
            choice = resolutions.get(group)
            if choice == "current":
                mine = {name: base.values[name] for name in names}
            elif choice != "mine":
                conflicts.append(group)
                values.update({name: current.get(name) for name in names})
                continue
        if spec.group_same(group, mine, base.values):  # not changed by the owner: what is saved now
            values.update({name: current.get(name) for name in names})
            continue
        if not spec.group_same(group, current, base.values) and not spec.group_same(group, current, mine):
            conflicts.append(group)
            values.update({name: current.get(name) for name in names})
            continue
        values.update(mine)
        changed.add(group)
    return Merge(values, frozenset(changed), tuple(conflicts))


def secret_intent(form: Mapping[str, Any], name: str) -> tuple[str, str]:
    """A secret field as the form meant it: ("keep", ""), ("replace", typed) or ("remove", "").
    An explicit ``<name>_intent`` wins; otherwise a typed value replaces, an empty one keeps."""
    if not isinstance(form, Mapping):  # a route called as a function: no form sent
        return "keep", ""
    typed = str(form.get(name) or "")
    intent = str(form.get(f"{name}_intent") or "").strip().lower()
    if intent == "remove":
        return "remove", ""
    if typed.strip():
        return "replace", typed
    return "keep", ""


def submitted_secret(spec: Spec, ident: str, name: str, form: Mapping[str, Any]) -> str | None:
    """The comparison token of what a secret field asks for (None: keep, not sent as a change)."""
    intent, typed = secret_intent(form, name)
    if intent == "keep":
        return None
    return secret_token(spec.kind, ident, name, typed if intent == "replace" else None)


async def resolutions(request: Request) -> dict[str, str]:
    """The owner's choices for the groups a conflict showed (``resolve_<group>``)."""
    form = await request.form()
    out = {}
    for key, value in form.items():
        if key.startswith("resolve_") and isinstance(value, str) and value in CHOICES:
            out[key.removeprefix("resolve_")[:64]] = value
    return out


async def form_fields(request: Request) -> dict[str, str]:
    """Every text field the form sent (a secret's intent and the base included)."""
    form = await request.form()
    return {key: value for key, value in form.items() if isinstance(value, str)}


# The dependencies a route declares (``resolutions: dict[str, str] = edit_base.RESOLUTIONS``).
RESOLUTIONS: Any = Depends(resolutions)
FIELDS: Any = Depends(form_fields)


@dataclass(frozen=True)
class Row:
    """One conflicting group as the form shows it: its name, what is saved now, what was typed."""

    group: str
    label: str
    current: str
    mine: str


def keep_conflict(
    spec: Spec,
    ident: str,
    *,
    base: str,
    draft: Mapping[str, Any],
    rows: list[Row],
    retype: tuple[str, ...] = (),
) -> str:
    """The token (``?conflict=``) of a refused save's conflict: the new base (what is saved now,
    the conflicting groups pending), the owner's draft without a secret, the rows to show, and
    the secret fields to type again."""
    never = {"base", "password", *spec.secret, *(f"{name}_intent" for name in spec.secret)}
    clean = {
        str(key): str(value)
        for key, value in draft.items()
        if value is not None and key not in never and not key.startswith("resolve_")
    }
    record = {
        "kind": spec.kind,
        "ident": ident,
        "base": base,
        "draft": clean,
        "rows": [row.__dict__ for row in rows],
        "retype": list(retype),
    }
    return _CONFLICTS.put({"record": json.dumps(record, ensure_ascii=False)})


def conflict(request: Request | None, kind: str, ident: str) -> dict[str, Any] | None:
    """The conflict ``?conflict=`` names for this form (None: none, expired or another form's)."""
    if request is None:
        return None
    token = request.query_params.get("conflict") or ""
    stored = _CONFLICTS.get(token) if token else None
    if not stored:
        return None
    try:
        record = json.loads(stored["record"])
    except KeyError, ValueError:
        return None
    if not isinstance(record, dict) or record.get("kind") != kind or record.get("ident") != ident:
        return None
    return record


def any_conflict(request: Request) -> dict[str, Any] | None:
    """The conflict ``?conflict=`` names, whichever form it is about (a page of many forms)."""
    token = request.query_params.get("conflict") or ""
    stored = _CONFLICTS.get(token) if token else None
    try:
        record = json.loads(stored["record"]) if stored else None
    except KeyError, ValueError:
        return None
    return record if isinstance(record, dict) else None


def _with_query(target: str, name: str, value: str) -> str:
    path, mark, fragment = target.partition("#")
    return f"{path}{'&' if '?' in path else '?'}{name}={value}{mark}{fragment}"


def touched_draft(spec: Spec, merged: Merge, draft: Mapping[str, Any]) -> dict[str, Any]:
    """The draft a conflict brings back: only the groups the owner changed or that conflict. A
    group the owner left alone shows what is saved now (the new base's value): kept from the old
    draft, it would read as the owner's change and put back what another device saved."""
    kept = merged.changed | set(merged.conflicts)
    untouched = {name for group, names in spec.groups.items() if group not in kept for name in names}
    return {key: value for key, value in draft.items() if key not in untouched}


def conflict_redirect(
    spec: Spec,
    ident: str,
    target: str,
    *,
    now: Mapping[str, Any],
    merged: Merge,
    draft: Mapping[str, Any],
    rows: list[Row],
    retype: tuple[str, ...] = (),
) -> RedirectResponse:
    """Back to the form at ``target`` with the conflict: the owner's draft, both values and a
    new base (saved now, the conflicting groups waiting for a choice). Nothing was saved."""
    token = keep_conflict(
        spec,
        ident,
        base=encode(spec, ident, now, merged.conflicts),
        draft=touched_draft(spec, merged, draft),
        rows=rows,
        retype=retype,
    )
    return flash_redirect(_with_query(target, "conflict", token), "web.edit.conflict", "warn")


def reload_redirect(target: str) -> RedirectResponse:
    """A save without a valid base: nothing saved, the form opens as saved now."""
    return flash_redirect(target, "web.edit.reload", "err")


def clear() -> None:
    """Forget every kept conflict (tests)."""
    _CONFLICTS.clear()
