"""What the settings page shows about the torrent clients (no network; the messengers' twin is
``tow.notifiers.view``)."""

from __future__ import annotations

from typing import Any

from tow.clients.factory import client_configurations, client_secret_block, default_client_id
from tow.clients.spec import get


def topics_using(topics: list[dict[str, Any]], client_id: str, default_id: str) -> int:
    """How many topics a client serves: those that name it, and for the main client those that
    name none (a client in use is not removed)."""
    return sum(1 for topic in topics if str(topic.get("client_id") or default_id) == client_id)


def cards(cfg: dict[str, Any], secrets: dict[str, Any], topics: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Everything the settings page shows for each configured client whose module is ready, in
    the owner's language. A client the page cannot offer (no module, ``READY = False``) has no card."""
    configurations = client_configurations(cfg)
    default_id = default_client_id(cfg)
    view = []
    for index, configuration in enumerate(configurations):
        spec = get(str(configuration.get("kind") or "").lower())
        if spec is None or not spec.ready:
            continue
        client_id = str(configuration["id"])
        block = client_secret_block(cfg, secrets, client_id)
        view.append(
            {
                "id": client_id,
                "form_id": f"client-{index}",
                "kind": spec.kind,
                "title": str(configuration.get("title") or spec.title),
                "host": block.get("host") or "",
                "port": block.get("port") or spec.default_port,
                "user": block.get("username") or "",
                "pass_set": bool(block.get("password")),
                "fields": [field.name for field in spec.fields],
                "labels": {field.name: field.label for field in spec.fields},
                "steps": list(spec.steps),
                "note": spec.note,
                "default": client_id == default_id,
                "multi": len(configurations) > 1,
                "used_by": topics_using(topics, client_id, default_id),
            }
        )
    return view
