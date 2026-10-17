"""The Settings forms as the three-way merge sees them (``tow.web.edit_base``): the values each
form showed and what its save asks for.

- Checks: the check interval and the messages' time, each on its own.
- Language and update checks: one value each (the theme is an instant choice, saved in the
  background as it is made: the last one made is the theme).
- A torrent client's card: its address, port, user name and password are one group (a saved
  password must never go to an address it was not typed for): a credential card.
- A messenger's card: all its fields are one group (a token belongs to its chat): a credential
  card. Passwords and tokens are opaque comparison tokens, never in the page.
- Site access (protection): its switch and each limit on their own; its connections one group
  (their order ties them together), compared as a digest - a stale form never brings back a
  connection removed elsewhere. Two forms that each only added connections are both kept
  (``appended_connections``); anything else changed on both sides is a conflict.
- Nightly backups: the automatic switch, the retention and each folder, one value each.
- Autostart: the switch and "without signing in", each on its own (read from the OS).
- Network access and its password: one indivisible card, guarded by its revision (an opaque
  token of the switch, the address and the password's record): a form opened before another
  device changed it saves nothing.
"""

from __future__ import annotations

import json
import re
from collections.abc import Mapping
from typing import Any

from fastapi.responses import RedirectResponse

from tow.backup_retention import retention_settings
from tow.config import as_bool, flash_ttl, interval_sec_of, whole_number
from tow.i18n import AUTO, canonical, setting
from tow.notifiers import kinds as notifier_kinds
from tow.notifiers import storage_scope
from tow.web import edit_base
from tow.web.edit_base import Row, Spec, digest, secret_intent, secret_token
from tow.web.text import t

INTERVALS = Spec("intervals", {"interval": ("interval_min",), "messages": ("flash_ttl_min",)})
LANGUAGE = Spec("language", {"language": ("language",)})
UPDATES = Spec("updates", {"updates": ("check_updates",)})
CLIENT = Spec("client", {"connection": ("host", "port", "username", "password")}, secret=frozenset({"password"}))
PROTECTION = Spec(
    "protection",
    {
        "enabled": ("enabled",),
        "timeout": ("timeout_sec",),
        "interval": ("min_interval_sec",),
        "attempts": ("max_attempts",),
        "providers": ("providers",),
    },
)
BACKUP_AUTOMATIC = Spec("backup-automatic", {"automatic": ("backup_enabled",)})
BACKUP_RETENTION = Spec("backup-retention", {"retention": ("backup_days",)})
BACKUP_FOLDER = Spec("backup-folder", {"folder": ("path",)})  # one per kind of copy (its ident)
AUTOSTART = Spec("autostart", {"autostart": ("enabled",), "without_login": ("without_login",)})
ACCESS = Spec("access", {"card": ("revision",)}, secret=frozenset({"revision"}))


def merged(
    spec: Spec,
    target: str,
    base: str,
    now: dict[str, Any],
    wanted: dict[str, Any],
    resolutions: dict[str, str],
    labels: dict[str, str],
    draft: dict[str, str],
    *,
    ident: str | None = None,
    rows: list[Row] | None = None,
) -> edit_base.Merge | RedirectResponse:
    """A Settings form merged with what is saved now: the merge, or the refusal (a missing or
    invalid base: "reload"; a conflict: the form again with the draft and both values)."""
    ident = spec.kind if ident is None else ident
    got = edit_base.read(spec, ident, base, now)
    if got is None:
        return edit_base.reload_redirect(target)
    result = edit_base.merge(spec, got, now, wanted, resolutions)
    if result.conflicts:
        shown = rows if rows is not None else value_rows(spec, result.conflicts, labels, now, wanted)
        shown = [row for row in shown if row.group in result.conflicts]
        return edit_base.conflict_redirect(spec, ident, target, now=now, merged=result, draft=draft, rows=shown)
    return result


def guard_refusal(spec: Spec, ident: str, target: str, base: str, now: dict[str, Any]) -> RedirectResponse | None:
    """An indivisible card's guard: None when the form shows the card as saved now; otherwise
    nothing is saved and the form opens again as saved now ("reload", or "changed on another
    device meanwhile")."""
    got = edit_base.read(spec, ident, base, now)
    if got is None:
        return edit_base.reload_redirect(target)
    if any(not spec.group_same(group, got.values, now) for group in spec.groups):
        return edit_base.changed_redirect(target)
    return None


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


def protection_now(settings: Mapping[str, Any]) -> dict[str, Any]:
    """Site access as its form shows it (``settings``: validated): the connections as one digest."""
    return {
        "enabled": bool(settings.get("enabled")),
        "timeout_sec": settings.get("timeout_sec"),
        "min_interval_sec": settings.get("min_interval_sec"),
        "max_attempts": settings.get("max_attempts"),
        "providers": digest(list(settings.get("providers") or [])),
    }


MAX_CONNECTIONS = 16


def _prefix_of(items: list[Any], base: str) -> int | None:
    """The length of the prefix of ``items`` whose digest is ``base`` (None: none is)."""
    for length in range(min(len(items), MAX_CONNECTIONS) + 1):
        if digest(items[:length]) == base:
            return length
    return None


def appended_connections(base: str, saved: list[Any], sent: list[Any]) -> list[Any] | None:
    """The connections to save when the form only added some while another save also only added
    some: what is saved now, then the form's additions in its order. Proven, not assumed: a prefix
    of each list has the digest of the connections the form showed (``base``), the same prefix,
    and each list goes on from there with additions only. None (keep the conflict) for anything
    else: an edit, a removal or a new order on either side, an added id saved already or added
    twice, or more than ``MAX_CONNECTIONS`` together. The caller validates the result."""
    kept, shown = _prefix_of(saved, base), _prefix_of(sent, base)
    if kept is None or shown is None or kept != shown or saved[:kept] != sent[:shown]:
        return None
    added = sent[shown:]
    if not added or kept == len(saved):
        return None  # nothing added here, or nothing saved meanwhile: the ordinary merge decides
    ids = [str(item.get("id")) for item in [*saved, *added] if isinstance(item, Mapping)]
    if len(ids) != len(saved) + len(added) or len(set(ids)) != len(ids):
        return None
    union = [*saved, *added]
    return union if len(union) <= MAX_CONNECTIONS else None


def protection_draft(fields: Mapping[str, Any]) -> dict[str, str]:
    """What a refused save of Site access brings back: its fields, each connection's under
    ``providers:<id>:<field>`` (kept only when the connections are the owner's change)."""
    out = {name: str(fields.get(name) or "") for name in ("timeout_sec", "min_interval_sec", "max_attempts")}
    out["enabled"] = "1" if fields.get("enabled") == "1" else ""
    ids, enabled = list(fields.get("provider_id") or []), set(fields.get("provider_enabled") or [])
    columns = {name: list(fields.get(f"provider_{name}") or []) for name in ("kind", "url", "order")}
    for index, ident in enumerate(ids[:16]):
        for name, values in columns.items():
            if index < len(values):
                out[f"providers:{ident}:{name}"] = str(values[index])[:256]
        out[f"providers:{ident}:enabled"] = "1" if ident in enabled else ""
    return out


def _draft_connections(draft: Mapping[str, Any]) -> dict[str, dict[str, str]]:
    """The connections of a refused save's draft, in the form's order: ``{id: {field: value}}``."""
    out: dict[str, dict[str, str]] = {}
    for key, value in draft.items():
        head, _, rest = str(key).partition(":")
        ident, _, name = rest.rpartition(":")
        if head == "providers" and ident and name in {"kind", "url", "order", "enabled"}:
            out.setdefault(ident, {})[name] = str(value)
    return out


def protection_recovery(view: Mapping[str, Any], conflict: Mapping[str, Any] | None) -> dict[str, Any]:
    """Site access as its form draws it (``view``: ``services.protection_settings_view``); in a
    refused save's recovery form whose draft holds the connections, the draft's whole collection.

    The connections are one group: "mine" saves exactly what the form sends, so the form must
    send the owner's collection, not the one saved now with the draft's values laid over it (a
    suggested id another tab saved meanwhile drew that tab's service with this tab's address; a
    connection removed elsewhere vanished with its draft). A draft connection saved now as the
    same service is its saved card; any other one is drawn with its own service, as a new one."""
    connections = _draft_connections(conflict.get("draft") or {}) if conflict else {}
    if not conflict or conflict.get("kind") != "protection" or not connections:
        return dict(view)
    saved = {card["id"]: card for card in view["cards"] if not card["new"]}
    titles = {choice["kind"]: choice["title"] for choice in view["choices"]}
    cards = []
    for index, (ident, fields) in enumerate(connections.items(), 1):
        kind = fields.get("kind", "")
        card = saved.get(ident)
        if card is not None and card["kind"] == kind:
            cards.append(card)
            continue
        offered = kind in titles or not kind  # a service no longer offered keeps its kind, hidden
        cards.append(
            {
                "id": ident,
                "kind": kind,
                "url": fields.get("url", ""),
                "enabled": fields.get("enabled") == "1",
                "available": offered,
                "new": offered,
                "draft": True,
                "on_demand": False,
                "local_runtime_refused": False,
                "order": fields.get("order") or index,
                "title": titles.get(kind, kind),  # no service status: it has never been saved
            }
        )
    return {**view, "cards": cards}


def protection_rows(
    groups: tuple[str, ...],
    now: Mapping[str, Any],
    wanted: Mapping[str, Any],
    saved: Mapping[str, Any],
    sent: Mapping[str, Any],
) -> list[Row]:
    """Site access's conflict, group by group; the connections by their addresses."""
    labels = {
        "enabled": t("protection_ui.enabled"),
        "timeout": t("protection_ui.timeout"),
        "interval": t("protection_ui.interval"),
        "attempts": t("protection_ui.attempts"),
    }
    out = value_rows(PROTECTION, tuple(group for group in groups if group != "providers"), labels, now, wanted)
    if "providers" in groups:
        saved_text, sent_text = _connections(saved.get("providers")), _connections(sent.get("providers"))
        out.append(Row("providers", t("protection_ui.title"), saved_text, sent_text))
    return out


def _connections(providers: object) -> str:
    rows = providers if isinstance(providers, list) else []
    off = t("web.edit.value.off")
    shown = [str(item.get("url")) + ("" if item.get("enabled") else f" ({off})") for item in rows]
    return ", ".join(shown) or t("web.edit.value.empty")


def backup_automatic_now(cfg: Mapping[str, Any]) -> dict[str, Any]:
    return {"backup_enabled": as_bool(cfg.get("backup_enabled"), True)}


def backup_retention_now(cfg: Mapping[str, Any]) -> dict[str, Any]:
    return {"backup_days": retention_settings(dict(cfg))["days"]}


def backup_folder_now(value: str) -> dict[str, Any]:
    """A folder of copies as its form shows it ("" for the default folder)."""
    return {"path": value.strip().strip('"')}


def autostart_now(status: Mapping[str, Any]) -> dict[str, Any]:
    return {"enabled": bool(status.get("autostart")), "without_login": bool(status.get("without_login"))}


def access_now(cfg: Mapping[str, Any], record: object) -> dict[str, Any]:
    """The network access card's revision: an opaque token of its switch, its address and the
    password's record (never the record itself in the page)."""
    card = json.dumps(
        [as_bool(cfg.get("allow_lan")), str(cfg.get("bind") or "127.0.0.1"), record],
        sort_keys=True,
        ensure_ascii=False,
        default=str,
    )
    return {"revision": secret_token("access", "access", "revision", card)}


def access_guard(
    cfg: Mapping[str, Any], record: object, base: str, target: str = "/settings?open=access"
) -> RedirectResponse | None:
    """The network access card's guard (its switch, its address and the password together): None
    when the form shows the card as saved now, else the refusal."""
    return guard_refusal(ACCESS, "access", target, base, access_now(cfg, record))
