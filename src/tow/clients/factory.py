from __future__ import annotations

import copy
from typing import Any

from tow.clients.spec import ClientSpec, TorrentClientAdapter, get, ready
from tow.config import as_bool
from tow.errors import TowError


class ClientSettingError(TowError, ValueError):
    """The client list cannot be changed that way, or is written wrongly (``client.factory.*``)."""


class ClientUnavailableError(TowError, RuntimeError):
    """The chosen client is not configured, is turned off or has no module (``client.factory.*``)."""


def client_configurations(cfg: dict[str, Any]) -> list[dict[str, Any]]:
    raw = cfg.get("clients")
    rows: list[dict[str, Any]] = []
    if isinstance(raw, dict):
        for client_id, value in raw.items():
            item = dict(value) if isinstance(value, dict) else {"kind": value}
            item.setdefault("id", str(client_id))
            rows.append(item)
    elif isinstance(raw, list):
        rows.extend(dict(value) for value in raw if isinstance(value, dict))

    if not rows:
        legacy = dict(cfg.get("client") or {})
        kind = str(legacy.get("kind") or "qbittorrent").lower()
        legacy.setdefault("kind", kind)
        legacy.setdefault("id", "default")
        legacy.setdefault("enabled", True)
        legacy.setdefault("default", True)
        rows.append(legacy)
    else:
        for row in rows:
            row["kind"] = str(row.get("kind") or "qbittorrent").lower()
            row.setdefault("id", row["kind"])
            row.setdefault("enabled", True)

    for row in rows:
        # "enabled: false" written as text must not count as true.
        row["enabled"] = as_bool(row.get("enabled", True), default=True)
        row["default"] = as_bool(row.get("default", False))
    seen: set[str] = set()
    for row in rows:
        client_id = str(row.get("id") or "").strip()
        if not client_id:
            raise ClientSettingError("client.factory.empty_id")
        key = client_id.casefold()
        if key in seen:
            raise ClientSettingError("client.factory.duplicate_id", id=client_id)
        seen.add(key)
        row["id"] = client_id
    return rows


def default_client_id(cfg: dict[str, Any]) -> str:
    rows = client_configurations(cfg)
    for row in rows:
        if row.get("default") and row.get("enabled", True):
            return str(row["id"])
    for row in rows:
        if row.get("enabled", True):
            return str(row["id"])
    return str(rows[0]["id"])


def client_configuration(cfg: dict[str, Any], client_id: str | None = None) -> dict[str, Any]:
    rows = client_configurations(cfg)
    wanted = client_id or default_client_id(cfg)
    for row in rows:
        if str(row.get("id")) == str(wanted):
            return row
    raise ClientUnavailableError("client.factory.not_configured", id=str(wanted))


def client_name(row: dict[str, Any]) -> str:
    """A torrent client as the owner knows it: its own title, else the program's name
    ("qBittorrent", never "qBit" or "default (qbittorrent)"); a second client of one kind
    without a title is told apart by its id. The pages and `tow status` say it the same way."""
    if row.get("title"):
        return str(row["title"])
    kind = str(row.get("kind") or "qbittorrent").lower()
    spec = get(kind)
    name = spec.title if spec is not None else kind
    client_id = str(row.get("id") or "")
    return name if client_id in ("", "default", kind) else f"{name} ({client_id})"


def client_secret_block(
    cfg: dict[str, Any],
    secrets: dict[str, Any],
    client_id: str | None = None,
    *,
    ensure: bool = False,
) -> dict[str, Any]:
    """Return one client secret block without exposing or merging instances."""
    configuration = client_configuration(cfg, client_id)
    if not cfg.get("clients"):
        spec = get(str(configuration.get("kind") or "qbittorrent").lower())
        key = spec.secrets_key if spec is not None else "qbittorrent"
        return secrets.setdefault(key, {}) if ensure else (secrets.get(key) or {})

    ref = str(configuration.get("secrets_ref") or configuration["id"])
    clients = secrets.get("clients")
    if not isinstance(clients, dict):
        if ensure:
            clients = {}
            secrets["clients"] = clients
        else:
            return {}
    block = clients.get(ref)
    if not isinstance(block, dict):
        if not ensure:
            return {}
        block = {}
        clients[ref] = block
    return block


def _secret_payload(
    cfg: dict[str, Any], secrets: dict[str, Any], configuration: dict[str, Any], spec: ClientSpec
) -> dict[str, Any]:
    if not cfg.get("clients"):
        return secrets
    block = client_secret_block(cfg, secrets, str(configuration["id"]))
    return {spec.secrets_key: copy.deepcopy(block)}


def from_secrets(
    cfg: dict[str, Any],
    secrets: dict[str, Any],
    client_id: str | None = None,
) -> TorrentClientAdapter:
    """The adapter of the chosen (default: the main) client, set up with its id, kind, and the
    optional category and extra tags for torrents TOW adds."""
    configuration = client_configuration(cfg, client_id)
    if not configuration.get("enabled", True):
        raise ClientUnavailableError("client.factory.disabled", id=str(configuration["id"]))
    kind = str(configuration.get("kind") or "qbittorrent").lower()
    spec = get(kind)
    if spec is None or not spec.ready:
        raise ClientUnavailableError("client.factory.not_implemented", kind=kind)
    adapter = spec.load(_secret_payload(cfg, secrets, configuration, spec))
    raw_tags = configuration.get("tags") or []
    tags = (
        [part.strip() for part in raw_tags.split(",")]
        if isinstance(raw_tags, str)
        else [str(tag).strip() for tag in raw_tags]
    )
    adapter.client_id = str(configuration["id"])
    adapter.client_kind = kind
    # G8: optional qBittorrent category and extra tags for torrents TOW adds.
    adapter.add_category = str(configuration.get("category") or "").strip()
    adapter.add_tags = [tag for tag in tags if tag]
    return adapter


def _stored_row(row: dict[str, Any]) -> dict[str, Any]:
    """A configuration row as written to config.yaml (defaults left out)."""
    out = {key: value for key, value in row.items() if key not in {"enabled", "default"}}
    if not row.get("enabled", True):
        out["enabled"] = False
    if row.get("default"):
        out["default"] = True
    return out


def add_client(cfg: dict[str, Any], secrets: dict[str, Any], kind: str) -> str:
    """Add an (empty) client of ``kind``; returns its id.

    The first extra client turns the single ``client:`` block into a ``clients:`` list; the
    existing client keeps its id, and its saved login is copied (the old block stays too).
    """
    spec = get(kind)
    if spec is None or not spec.ready:
        raise ClientSettingError("client.factory.unknown_kind")
    rows = client_configurations(cfg)
    if not cfg.get("clients"):
        legacy = client_secret_block(cfg, secrets, str(rows[0]["id"]))
        if legacy:
            secrets.setdefault("clients", {})[str(rows[0]["id"])] = copy.deepcopy(legacy)
        rows[0]["default"] = True
        cfg.pop("client", None)
    taken = {str(row["id"]).casefold() for row in rows}
    client_id, number = spec.kind, 2
    while client_id.casefold() in taken:
        client_id, number = f"{spec.kind}-{number}", number + 1
    rows.append({"id": client_id, "kind": spec.kind, "title": spec.title})
    cfg["clients"] = [_stored_row(row) for row in rows]
    return client_id


def remove_client(cfg: dict[str, Any], secrets: dict[str, Any], client_id: str, used_by: int) -> None:
    """Remove a client that no topic uses; raises ValueError with the reason otherwise."""
    rows = client_configurations(cfg)
    row = next((row for row in rows if str(row["id"]) == client_id), None)
    if row is None or not cfg.get("clients"):
        raise ClientSettingError("client.factory.cannot_remove")
    if len(rows) == 1:
        raise ClientSettingError("client.factory.only_client")
    if used_by:
        raise ClientSettingError("client.factory.in_use", count=used_by)
    if default_client_id(cfg) == client_id:
        raise ClientSettingError("client.factory.is_default")
    ref = str(row.get("secrets_ref") or row["id"])
    if not any(str(other.get("secrets_ref") or other["id"]) == ref for other in rows if other is not row):
        clients = secrets.get("clients")
        if isinstance(clients, dict):
            clients.pop(ref, None)
    cfg["clients"] = [_stored_row(other) for other in rows if other is not row]


def set_default_client(cfg: dict[str, Any], client_id: str) -> None:
    rows = client_configurations(cfg)
    if not cfg.get("clients") or not any(str(row["id"]) == client_id for row in rows):
        raise ClientSettingError("client.factory.no_such_client")
    for row in rows:
        row["default"] = str(row["id"]) == client_id
        if row["default"]:
            row["enabled"] = True
    cfg["clients"] = [_stored_row(row) for row in rows]


__all__ = [
    "add_client",
    "client_configuration",
    "client_configurations",
    "client_secret_block",
    "default_client_id",
    "from_secrets",
    "ready",
    "remove_client",
    "set_default_client",
]
