"""The Settings forms as the three-way merge sees them (``tow.web.edit_base``): the values each
form showed and what its save asks for.

- Checks: the check interval and the messages' time, each on its own.
- Language and update checks: one value each (the theme is an instant choice, saved in the
  background as it is made: the last one made is the theme).
- A torrent client's card: its address, port, user name and password are one group (a saved
  password must never go to an address it was not typed for): a credential card.
- A messenger's card: all its fields are one group (a token belongs to its chat): a credential
  card. Passwords and tokens are opaque comparison tokens, never in the page.
"""

from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

from tow.config import as_bool, flash_ttl, interval_sec_of, whole_number
from tow.i18n import AUTO, canonical, setting
from tow.notifiers import kinds as notifier_kinds
from tow.notifiers import storage_scope
from tow.web.edit_base import Row, Spec, secret_intent, secret_token
from tow.web.text import t

INTERVALS = Spec("intervals", {"interval": ("interval_min",), "messages": ("flash_ttl_min",)})
LANGUAGE = Spec("language", {"language": ("language",)})
UPDATES = Spec("updates", {"updates": ("check_updates",)})
CLIENT = Spec("client", {"connection": ("host", "port", "username", "password")}, secret=frozenset({"password"}))


def shown_minutes(seconds: int) -> int:
    """What the Checks form shows for a setting kept in seconds (whole minutes, at least 1)."""
    return max(1, seconds // 60)


def intervals_now(cfg: Mapping[str, Any]) -> dict[str, Any]:
    return {
        "interval_min": shown_minutes(interval_sec_of(dict(cfg))),
        "flash_ttl_min": shown_minutes(flash_ttl(cfg.get("flash_ttl_sec") or 60)),
    }


def intervals_wanted(interval_min: str, flash_ttl_min: str) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for name, raw in (("interval_min", interval_min), ("flash_ttl_min", flash_ttl_min)):
        number = whole_number(raw)
        out[name] = number if number is not None else f"invalid:{raw.strip()[:32]}"
    return out


def language_now(cfg: Mapping[str, Any]) -> dict[str, Any]:
    return {"language": setting(dict(cfg))}


def language_wanted(auto: str, language: str) -> dict[str, Any]:
    chosen = canonical(language)
    return {"language": AUTO if as_bool(auto) or chosen is None else chosen}


def updates_now(cfg: Mapping[str, Any]) -> dict[str, Any]:
    return {"check_updates": as_bool(cfg.get("check_updates", True))}


def client_now(client_id: str, block: Mapping[str, Any], default_port: int) -> dict[str, Any]:
    """A client's card as saved now (``block``: its secrets block)."""
    port = block.get("port")
    return {
        "host": str(block.get("host") or "").strip(),
        "port": port if type(port) is int else default_port,
        "username": str(block.get("username") or "").strip(),
        "password": secret_token("client", client_id, "password", block.get("password")),
    }


def client_wanted(client_id: str, host: str, port: int, username: str, fields: Mapping[str, str]) -> dict[str, Any]:
    out: dict[str, Any] = {"host": host.strip(), "port": port, "username": username.strip()}
    intent, typed = secret_intent(fields, "password")
    if intent != "keep":
        out["password"] = secret_token("client", client_id, "password", typed if intent == "replace" else None)
    return out


def notifier_spec(kind: str) -> Spec:
    """A messenger's card: its fields, one group; its secrets as tokens."""
    module = notifier_kinds()[kind]
    names = tuple(field.name for field in module.FIELDS)
    secret = frozenset(field.name for field in module.FIELDS if field.kind == "secret")
    return Spec(f"notifier-{kind}", {"card": names}, secret=secret)


def notifier_raw(secrets: Mapping[str, Any], kind: str) -> dict[str, Any]:
    raw: Any = secrets
    for name in storage_scope(kind):
        raw = raw.get(name) if isinstance(raw, dict) else None
    return raw if isinstance(raw, dict) else {}


def _items(value: object) -> str:
    if isinstance(value, list):
        return ", ".join(str(item) for item in value)
    return ", ".join(part.strip() for part in re.split(r"[,\s;]+", str(value or "")) if part.strip())


def notifier_now(kind: str, secrets: Mapping[str, Any]) -> dict[str, Any]:
    """A messenger's card as saved now."""
    raw = notifier_raw(secrets, kind)
    out: dict[str, Any] = {}
    for field in notifier_kinds()[kind].FIELDS:
        value = raw.get(field.name)
        if field.kind == "secret":
            out[field.name] = secret_token(f"notifier-{kind}", kind, field.name, value)
        elif field.kind == "list":
            out[field.name] = _items(value)
        else:
            out[field.name] = str(value or field.default or "").strip()
    return out


def notifier_wanted(kind: str, fields: Mapping[str, str]) -> dict[str, Any]:
    out: dict[str, Any] = {}
    for field in notifier_kinds()[kind].FIELDS:
        raw = str(fields.get(field.name) or "")
        if field.kind == "secret":
            intent, typed = secret_intent(fields, field.name)
            if intent != "keep":
                value = typed.strip() if intent == "replace" else None
                out[field.name] = secret_token(f"notifier-{kind}", kind, field.name, value)
        elif field.kind == "list":
            out[field.name] = _items(raw)
        else:
            out[field.name] = raw.strip() or str(field.default or "")
    return out


def notifier_draft(kind: str, fields: Mapping[str, str]) -> dict[str, str]:
    """What a refused save of a messenger's card brings back: its fields but the secrets."""
    module = notifier_kinds()[kind]
    return {field.name: str(fields.get(field.name) or "") for field in module.FIELDS if field.kind != "secret"}


def _plain(values: Mapping[str, Any], secret: frozenset[str]) -> str:
    shown = [str(value) for name, value in values.items() if name not in secret and str(value)]
    return ", ".join(shown) or t("web.edit.value.empty")


def card_rows(spec: Spec, label: str, now: Mapping[str, Any], wanted: Mapping[str, Any]) -> list[Row]:
    """A credential card's conflict: its values but passwords and tokens, saved now and typed."""
    secret_changed = any(name in wanted and wanted[name] != now[name] for name in spec.secret)
    mine = _plain({**now, **wanted}, spec.secret)
    if secret_changed:
        mine += f" · {t('web.edit.value.secret_set')}"
    group = next(iter(spec.groups))
    return [Row(group, label, _plain(now, spec.secret), mine)]


def value_rows(
    spec: Spec, groups: tuple[str, ...], labels: Mapping[str, str], now: Mapping[str, Any], wanted: Mapping[str, Any]
) -> list[Row]:
    """A plain form's conflict, group by group (one field each)."""
    out = []
    for group in groups:
        name = spec.groups[group][0]
        out.append(Row(group, labels[group], _shown(now.get(name)), _shown(wanted.get(name))))
    return out


def _shown(value: object) -> str:
    if isinstance(value, bool):
        return t("web.edit.value.on" if value else "web.edit.value.off")
    text = str(value if value is not None else "")
    return text.removeprefix("invalid:") or t("web.edit.value.empty")
