"""Client presence and superseding: the torrent gone from its client or back, and which
revision's history items are live."""

from __future__ import annotations

from typing import Any

from tow.clients.spec import TorrentClientAdapter
from tow.errors import Msg
from tow.progress.labels import label_fields, latest_item_label, set_latest_event
from tow.records import HistoryItem, HistoryRecord


def track_client_presence(
    record: HistoryRecord, info: Any, client_adapter: TorrentClientAdapter, now: str, events: list[str]
) -> bool:
    """Record client_removed/client_restored transitions; False when the torrent is gone."""
    client_fields = {
        "at": now,
        "source": "tow",
        "client_id": client_adapter.client_id,
        "client_kind": client_adapter.client_kind,
    }
    if not info:
        if record.get("client_present") is True:
            record["client_present"] = False
            events.append("client_removed")
            set_latest_event(
                record, {"kind": "client_removed", **label_fields(latest_item_label(record)), **client_fields}
            )
        return False
    if record.get("client_present") is False:
        events.append("client_restored")
        set_latest_event(
            record,
            {"kind": "client_restored", **label_fields(Msg("progress.back_in_client")), **client_fields},
        )
    record["client_present"] = True
    return True


def mark_superseded(items: dict[str, HistoryItem], current_source_hash: str, *, has_files: bool) -> None:
    """Items of other revisions are superseded; the current revision's are live again."""
    for historical_item in items.values():
        historical_hash = str(historical_item.get("source_hash") or "").casefold()
        if not historical_hash or historical_hash != current_source_hash:
            historical_item["superseded"] = True
        elif has_files:
            historical_item["superseded"] = False
