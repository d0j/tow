"""One-time import of topics and credentials from a Monitorrent database.

N8: a preview unless ``apply``; a restore point before anything is written; topics
de-duplicated by id and by URL; credentials only fill empty slots (the configured
client's block for qBittorrent) and never overwrite what TOW already has; the
database is always closed.
"""

from __future__ import annotations

import contextlib
import sqlite3
from pathlib import Path
from typing import Any

from tow.store import (
    load_secrets,
    load_state,
    persistence_lock,
    save_secrets,
    save_state,
)


class MonitorrentImportError(RuntimeError):
    pass


def import_monitorrent(db_path: Path, *, apply: bool = False) -> dict[str, Any]:
    path = Path(db_path)
    if not path.is_file():
        raise MonitorrentImportError(f"Monitorrent database not found: {path.name}")
    with contextlib.closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)) as con:
        con.row_factory = sqlite3.Row
        topics = _read_topics(con)
        found = _read_credentials(con)
    if not apply:
        state = load_state()
        new_topics, skipped = _merge_plan(state, topics)
        return {
            "preview": True,
            "apply_required": True,
            "topics_found": len(topics),
            "topics_new": len(new_topics),
            "topics_already_watched": skipped,
            "credentials_found": sorted(found),
        }
    from tow.restore_points import create_restore_point

    with persistence_lock():
        point = create_restore_point()  # the import can be undone in Settings -> restore points
        state = load_state()
        new_topics, skipped = _merge_plan(state, topics)
        secrets = load_secrets()
        filled, kept = _fill_credentials(secrets, found)
        if filled:
            save_secrets(secrets)
        state["topics"] = [*(state.get("topics") or []), *new_topics]
        save_state(state)
    return {
        "preview": False,
        "restore_point": point["id"],
        "topics_added": len(new_topics),
        "topics_already_watched": skipped,
        "credentials_filled": sorted(filled),
        "credentials_kept": sorted(kept),
    }


def _rows(con: sqlite3.Connection, sql: str) -> list[sqlite3.Row]:
    try:
        return list(con.execute(sql))
    except sqlite3.Error:
        return []  # an older/newer Monitorrent without this table


def _read_topics(con: sqlite3.Connection) -> list[dict[str, Any]]:
    hashes: dict[int, str] = {}
    for table in ("kinozal_topics", "rutororg_topics"):
        for row in _rows(con, f"SELECT id, hash FROM {table}"):
            if row["hash"]:
                hashes[int(row["id"])] = str(row["hash"]).upper()
    return [
        {
            "id": f"mr-{row['id']}",
            "title": row["display_name"],
            "url": row["url"],
            "save_path": row["download_dir"],
            "hash": hashes.get(int(row["id"])),
            "selection": {"mode": "all", "value": ""},
            "tracking_mode": "watch",
        }
        for row in _rows(con, "SELECT id, display_name, url, download_dir FROM topics")
        if row["url"]
    ]


def _read_credentials(con: sqlite3.Connection) -> dict[str, dict[str, Any]]:
    found: dict[str, dict[str, Any]] = {}
    if q := next(iter(_rows(con, "SELECT host, port, username, password FROM qbittorrent_credentials")), None):
        found["qbittorrent"] = {
            "host": q["host"],
            "port": int(q["port"] or 8080),
            "username": q["username"] or "",
            "password": q["password"] or "",
        }
    if k := next(iter(_rows(con, "SELECT c_uid, c_pass, username, password FROM kinozal_credentials")), None):
        found["kinozal"] = {
            "uid": k["c_uid"] or "",
            "pass": k["c_pass"] or "",
            "username": k["username"] or "",
            "password": k["password"] or "",
        }
    tg = next(iter(_rows(con, "SELECT chat_ids, access_token FROM telegram_settings")), None)
    if tg and tg["access_token"]:
        chats = [c.strip() for c in str(tg["chat_ids"] or "").split(",") if c.strip()]
        found["telegram"] = {"token": tg["access_token"], "chat_ids": chats}
    return found


def _merge_plan(state: dict[str, Any], topics: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], int]:
    """Topics not watched yet - by id AND by URL - and how many were skipped."""
    existing = state.get("topics") or []
    ids = {str(t.get("id")) for t in existing}
    urls = {str(t.get("url")) for t in existing}
    new: list[dict[str, Any]] = []
    for topic in topics:
        if topic["id"] in ids or topic["url"] in urls:
            continue
        ids.add(topic["id"])
        urls.add(topic["url"])
        new.append(topic)
    return new, len(topics) - len(new)


def _fill_credentials(secrets: dict[str, Any], found: dict[str, dict[str, Any]]) -> tuple[list[str], list[str]]:
    """Fill only empty slots; what TOW already has is kept (it was set up on purpose)."""
    from tow.clients.factory import client_secret_block
    from tow.config import load_config

    filled: list[str] = []
    kept: list[str] = []
    if "qbittorrent" in found:
        block = client_secret_block(load_config(), secrets, ensure=True)
        if block.get("host"):
            kept.append("qbittorrent")
        else:
            block.update(found["qbittorrent"])
            filled.append("qbittorrent")
    if "kinozal" in found:
        trackers = secrets.setdefault("trackers", {})
        if (trackers.get("kinozal") or {}).get("uid") or (trackers.get("kinozal") or {}).get("username"):
            kept.append("kinozal")
        else:
            trackers["kinozal"] = found["kinozal"]
            filled.append("kinozal")
    if "telegram" in found:
        if (secrets.get("telegram") or {}).get("token"):
            kept.append("telegram")
        else:
            secrets["telegram"] = found["telegram"]
            filled.append("telegram")
    return filled, kept
