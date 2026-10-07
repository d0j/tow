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

from tow.clients.factory import client_configuration, client_configurations, client_secret_block
from tow.clients.spec import get
from tow.config import SAFE_ID, load_config, web_address
from tow.errors import TowError
from tow.guess import canon_watch_url
from tow.i18n import t
from tow.store import load_secrets, load_state, persistence_lock
from tow.store_transaction import RollbackError, TransactionError, transaction
from tow.trackers import load_trackers, match_tracker


class MonitorrentImportError(TowError):
    """A safe, localized import failure; implementation errors never disclose secrets."""


def import_monitorrent(db_path: Path, *, apply: bool = False, client_id: str | None = None) -> dict[str, Any]:
    path = Path(db_path)
    if not path.is_file():
        raise MonitorrentImportError("monitorrent.missing")
    try:
        with contextlib.closing(sqlite3.connect(path.resolve().as_uri() + "?mode=ro", uri=True)) as con:
            con.row_factory = sqlite3.Row
            # A text that is not UTF-8 spoils its row only, not the whole import.
            con.text_factory = _text
            # All queries see one snapshot even if Monitorrent changes its WAL concurrently.
            con.execute("BEGIN")
            rows = _read_topics(con)
            found = _read_credentials(con)
    except (sqlite3.Error, OSError, ValueError, TypeError) as exc:
        raise MonitorrentImportError("monitorrent.invalid_database") from exc
    with persistence_lock():
        cfg = load_config()
        topics, unusable = _usable_topics(rows, load_trackers(cfg))
        try:
            destination = client_configuration(cfg, client_id)
            if not destination["enabled"]:
                raise MonitorrentImportError("client.factory.disabled", id=destination["id"])
            spec = get(str(destination["kind"]))
            if spec is None or not spec.ready:
                raise MonitorrentImportError("client.factory.not_implemented", kind=destination["kind"])
        except TowError as exc:
            raise MonitorrentImportError(exc.code, **exc.params) from exc
        destination_id = str(destination["id"])
        state = load_state(quarantine=False)
        new_topics, skipped = _merge_plan(state, topics)
        for topic in new_topics:
            topic["client_id"] = destination_id
        credential_warning = _credential_warning(cfg, destination, found)
        skipped_credentials = ["qbittorrent"] if credential_warning else []
        if not apply:
            preview = {
                "preview": True,
                "apply_required": True,
                "client_id": destination_id,
                "topics_found": len(rows),
                "topics_new": len(new_topics),
                "topics_already_watched": skipped,
                "topics_unusable": unusable,
                "credentials_found": sorted(found),
                "credentials_skipped": skipped_credentials,
            }
            if credential_warning:
                preview["warnings"] = [t(credential_warning)]
            return preview
        from tow.restore_points import create_restore_point

        try:
            point = create_restore_point()  # the import can be undone in Settings -> restore points
        except (OSError, RuntimeError, ValueError) as exc:
            raise MonitorrentImportError("monitorrent.backup_failed") from exc
        secrets = load_secrets()
        compatible = {key: value for key, value in found.items() if key not in skipped_credentials}
        filled, kept = _fill_credentials(secrets, compatible, cfg, destination_id)
        state["topics"] = [*(state.get("topics") or []), *new_topics]
        try:
            with transaction() as txn:
                if filled:
                    txn.save_secrets(secrets)
                txn.save_state(state)
                if load_state(quarantine=False) != state or (filled and load_secrets() != secrets):
                    raise RuntimeError("Monitorrent import read-back mismatch")
        except RollbackError as exc:
            raise MonitorrentImportError("monitorrent.rollback_failed") from exc
        except TransactionError as exc:
            raise MonitorrentImportError("monitorrent.write_failed") from exc
        except (OSError, RuntimeError, ValueError) as exc:
            raise MonitorrentImportError("monitorrent.prepare_failed") from exc
    result = {
        "preview": False,
        "client_id": destination_id,
        "restore_point": point["id"],
        "topics_added": len(new_topics),
        "topics_already_watched": skipped,
        "topics_unusable": unusable,
        "credentials_filled": sorted(filled),
        "credentials_kept": sorted(kept),
        "credentials_skipped": skipped_credentials,
    }
    if credential_warning:
        result["warnings"] = [t(credential_warning)]
    if point.get("cleanup_warning"):
        result["cleanup_warning"] = point["cleanup_warning"]
    return result


def _credential_warning(
    cfg: dict[str, Any], destination: dict[str, Any], found: dict[str, dict[str, Any]]
) -> str | None:
    """Never fill a secret block also read by an incompatible client adapter."""
    if "qbittorrent" not in found:
        return None
    if destination["kind"] != "qbittorrent":
        return "monitorrent.qbittorrent_skipped"
    if cfg.get("clients"):
        ref = str(destination.get("secrets_ref") or destination["id"])
        for other in client_configurations(cfg):
            if str(other.get("secrets_ref") or other["id"]) == ref and other["kind"] != destination["kind"]:
                return "monitorrent.shared_credentials"
    return None


def _rows(con: sqlite3.Connection, sql: str) -> list[sqlite3.Row]:
    try:
        return list(con.execute(sql))
    except sqlite3.OperationalError as exc:
        if str(exc).startswith(("no such table:", "no such column:")):
            return []  # an older/newer Monitorrent without this optional table/column
        raise


class _Undecodable:
    """A text of the database that is not UTF-8."""


def _text(raw: bytes) -> str | _Undecodable:
    try:
        return raw.decode("utf-8")
    except UnicodeDecodeError:
        return _Undecodable()


def _plain(value: Any) -> str:
    """A text field as a string ("" for NULL or a text that could not be read)."""
    return value if isinstance(value, str) else ""


def _read_topics(con: sqlite3.Connection) -> list[dict[str, Any]]:
    """Every row of Monitorrent's topics (with its hash where Monitorrent keeps one), unchecked."""
    hashes: dict[Any, str] = {}
    for table in ("kinozal_topics", "rutororg_topics"):
        for row in _rows(con, f"SELECT id, hash FROM {table}"):
            if isinstance(row["hash"], str) and row["hash"]:
                hashes[row["id"]] = row["hash"].upper()
    return [
        {**dict(row), "hash": hashes.get(row["id"])}
        for row in _rows(con, "SELECT id, display_name, url, download_dir FROM topics")
    ]


def _usable_topics(rows: list[dict[str, Any]], trackers: dict[str, Any]) -> tuple[list[dict[str, Any]], int]:
    """The rows that become topics the way a link added by hand does (the topic page's link, a
    site TOW reads, a plain id), and how many could not; one bad row never stops the others."""
    topics = [topic for row in rows if (topic := _topic_of(row, trackers)) is not None]
    return topics, len(rows) - len(topics)


def _topic_of(row: dict[str, Any], trackers: dict[str, Any]) -> dict[str, Any] | None:
    raw_id = row.get("id")
    if isinstance(raw_id, bool) or not isinstance(raw_id, int):
        return None
    if any(isinstance(row.get(key), _Undecodable) for key in ("display_name", "url", "download_dir")):
        return None
    url = canon_watch_url(_plain(row.get("url")))
    topic_id = f"mr-{raw_id}"
    if not web_address(url) or not SAFE_ID.fullmatch(topic_id):
        return None
    try:
        if match_tracker(trackers, url) is None:
            return None  # no site TOW reads: it could never be checked
    except TowError:
        return None
    return {
        "id": topic_id,
        # Without a name the link stands in; the first check names the topic after its torrent.
        "title": _plain(row.get("display_name")).strip() or url,
        "url": url,
        "save_path": _plain(row.get("download_dir")),
        "hash": row.get("hash"),
        "selection": {"mode": "all", "value": ""},
        "tracking_mode": "watch",
    }


def _read_credentials(con: sqlite3.Connection) -> dict[str, dict[str, Any]]:
    found: dict[str, dict[str, Any]] = {}
    if q := next(iter(_rows(con, "SELECT host, port, username, password FROM qbittorrent_credentials")), None):
        found["qbittorrent"] = {
            "host": _plain(q["host"]),
            "port": int(q["port"] or 8080),
            "username": _plain(q["username"]),
            "password": _plain(q["password"]),
        }
    if k := next(iter(_rows(con, "SELECT c_uid, c_pass, username, password FROM kinozal_credentials")), None):
        found["kinozal"] = {
            "uid": _plain(k["c_uid"]),
            "pass": _plain(k["c_pass"]),
            "username": _plain(k["username"]),
            "password": _plain(k["password"]),
        }
    tg = next(iter(_rows(con, "SELECT chat_ids, access_token FROM telegram_settings")), None)
    if tg and _plain(tg["access_token"]):
        chats = [c.strip() for c in _plain(tg["chat_ids"]).split(",") if c.strip()]
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


def _fill_credentials(
    secrets: dict[str, Any], found: dict[str, dict[str, Any]], cfg: dict[str, Any], client_id: str
) -> tuple[list[str], list[str]]:
    """Fill only empty slots; what TOW already has is kept (it was set up on purpose)."""
    filled: list[str] = []
    kept: list[str] = []
    if "qbittorrent" in found:
        block = client_secret_block(cfg, secrets, client_id, ensure=True)
        spec = get("qbittorrent")
        default_port = spec.default_port if spec is not None else 8080
        configured_port = block.get("port") not in (None, "", default_port, str(default_port))
        if configured_port or any(block.get(key) for key in ("host", "username", "password")):
            kept.append("qbittorrent")
        else:
            block.update(found["qbittorrent"])
            filled.append("qbittorrent")
    if "kinozal" in found:
        trackers = secrets.setdefault("trackers", {})
        if any((trackers.get("kinozal") or {}).get(key) for key in ("uid", "pass", "username", "password")):
            kept.append("kinozal")
        else:
            trackers["kinozal"] = found["kinozal"]
            filled.append("kinozal")
    if "telegram" in found:
        if any((secrets.get("telegram") or {}).get(key) for key in ("token", "chat_ids")):
            kept.append("telegram")
        else:
            secrets["telegram"] = found["telegram"]
            filled.append("telegram")
    return filled, kept
