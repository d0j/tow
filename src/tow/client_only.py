"""One native-client handover, never a watched topic or a download monitor."""

from __future__ import annotations

import base64
import contextlib
import copy
import hashlib
import json
import os
import ssl
import time
from typing import Any

import httpx
import requests

from tow import check, client_access, client_only_store, paths
from tow.clients import factory, files
from tow.clients.managed import AWAITING_INITIAL_CHECK, ClientVerificationPending
from tow.clients.spec import TorrentClientAdapter
from tow.clients.state import is_stopped, matches_hash
from tow.config import as_bool, load_config
from tow.errors import TowError, record_of
from tow.folders import paths_equal, seen_from_here, volume_at
from tow.selection import SelectionPlan, resolve_selection
from tow.store import CheckBusyError, check_run_lock, load_secrets, load_state, persistence_lock, save_state
from tow.topic_form import save_path_refusal
from tow.torrent import TorrentFile, TorrentMetadata, parse_torrent_metadata

Result = dict[str, Any]


class _Cancelled(Exception):
    pass


def _digest(value: Any) -> str:
    return hashlib.sha256(json.dumps(value, sort_keys=True, ensure_ascii=False).encode()).hexdigest()


def _install() -> str:
    return _digest(os.path.normcase(str(paths.root().resolve())))


def _result(row: dict[str, Any]) -> Result:
    names = (
        "operation_id",
        "status",
        "phase",
        "hash",
        "client_id",
        "title",
        "selected_file_count",
        "created_at",
        "finished_at",
        "error",
    )
    return {
        **{name: copy.deepcopy(row[name]) for name in names if name in row},
        "confirmed": row["status"] in {"added", "existing"},
    }


def get_result(operation_id: str, *, request_fingerprint: str | None = None) -> Result | None:
    if not client_only_store.valid_id(operation_id):
        raise TowError("client_only.request_changed")
    with persistence_lock():
        row = client_only_store.read_entries().get(operation_id)
        if row is not None and request_fingerprint is not None and row["request"] != request_fingerprint:
            raise TowError("client_only.request_changed")
        return _result(row) if row is not None else None


def finished() -> list[Result]:
    with persistence_lock():
        return [_result(row) for row in client_only_store.read_entries().values() if row["status"] != "pending"]


def pending() -> list[str]:
    with persistence_lock():
        ordered = sorted(client_only_store.read_entries().values(), key=lambda item: item["next_at"])
        return [row["operation_id"] for row in ordered if row["status"] == "pending"]


def cancel(operation_id: str) -> Result:
    with persistence_lock():
        entries = client_only_store.read_entries()
        row = entries.get(operation_id)
        if row is None:
            raise TowError("client_only.request_changed")
        if row["status"] == "pending":
            root = row.get("root_operation") or operation_id
            for related in entries.values():
                if (related.get("root_operation") or related["operation_id"]) != root or related["status"] != "pending":
                    continue
                related.update(
                    status="cancelled",
                    phase="cancelled",
                    finished_at=time.time(),
                    error=TowError("client_only.cancelled").record(),
                )
                for key in ("blob", "proof", "policy"):
                    related.pop(key, None)
            client_only_store.write_entries(entries)
        return _result(row)


def _settings(client_id: str) -> tuple[dict[str, Any], dict[str, Any], dict[str, str]]:
    cfg, secrets = load_config(), load_secrets()
    chosen = factory.client_configuration(cfg, client_id)
    if not chosen.get("enabled", True):
        raise TowError("client.factory.disabled", id=client_id)
    return cfg, secrets, client_access.context(cfg, secrets, client_id)


def _scope(cfg: dict[str, Any], context: dict[str, str], client_id: str) -> str:
    config = factory.client_configuration(cfg, client_id)
    return _digest([context, {key: config.get(key) for key in ("kind", "category", "tags")}])


def _identities(metadata: TorrentMetadata) -> list[str]:
    return list(
        dict.fromkeys(
            value.upper()
            for value in (
                metadata.client_hash,
                metadata.hash_v1,
                metadata.hash_v2,
                metadata.hash_v2[:40] if metadata.hash_v2 else None,
            )
            if value
        )
    )


def _new_record(
    blob: bytes,
    dest: str,
    client_id: str,
    policy: dict[str, Any],
    identity: str,
    request: str,
    preferred_season: int | None,
) -> dict[str, Any]:
    cfg, secrets, context = _settings(client_id)
    if preferred_season is not None and (type(preferred_season) is not int or preferred_season <= 0):
        raise TowError("selection.exact_invalid")
    if refusal := save_path_refusal(dest, allow_unc=as_bool(cfg.get("allow_unc_save_paths"))):
        raise TowError(refusal)
    metadata = parse_torrent_metadata(blob)
    if policy.get("mode") == "exact" and str(policy.get("source_hash") or "").upper() != metadata.infohash.upper():
        raise TowError("selection.exact_changed")
    plan = resolve_selection(metadata.files, policy, preferred_season=preferred_season)
    local = factory.on_this_computer(cfg, secrets, client_id) and seen_from_here(dest)
    now = time.time()
    return {
        "operation_id": identity,
        "status": "pending",
        "phase": "queued",
        "client_id": client_id,
        "context": context,
        "scope": _scope(cfg, context, client_id),
        "install": _install(),
        "save_path": dest,
        "local": local,
        "volume": volume_at(dest) if local else None,
        "payload": _digest([hashlib.sha256(blob).hexdigest(), dest, client_id, policy, preferred_season]),
        "request": request,
        "hash": metadata.client_hash.upper(),
        "hashes": _identities(metadata),
        "title": metadata.name,
        "selected_file_count": len(plan.selected_indices),
        "policy": copy.deepcopy(policy),
        "preferred_season": preferred_season,
        "blob": base64.b64encode(blob).decode(),
        "created_at": now,
        "deadline": now + client_only_store.LIFETIME,
        "next_at": now,
        "add_dispatched": False,
        "start_dispatched": False,
        "failures": 0,
    }


def send_to_client(
    blob: bytes,
    save_path: str,
    client_id: str,
    policy: dict[str, Any],
    operation_id: str,
    *,
    request_fingerprint: str = "",
    preferred_season: int | None = None,
) -> Result:
    if not client_only_store.valid_id(operation_id):
        raise TowError("client_only.request_changed")
    candidate = _new_record(blob, save_path, client_id, policy, operation_id, request_fingerprint, preferred_season)
    with persistence_lock():
        entries = client_only_store.read_entries()
        old = entries.get(operation_id)
        if old is not None:
            if old["payload"] != candidate["payload"] or old["request"] != request_fingerprint:
                raise TowError("client_only.request_changed")
            return _result(old)
        same = next(
            (
                row
                for row in entries.values()
                if row["deadline"] > time.time()
                and row["context"].get("endpoint") == candidate["context"].get("endpoint")
                and set(row["hashes"]).intersection(candidate["hashes"])
                and not row.get("confirmation_only")
                and (row["status"] == "pending" or (row["add_dispatched"] and row["status"] == "failed"))
            ),
            None,
        )
        if same is not None:
            if same["payload"] != candidate["payload"]:
                raise TowError("client_only.request_changed")
            candidate["confirmation_only"] = True
            candidate["root_operation"] = same.get("root_operation") or same["operation_id"]
        client_only_store.prune(entries, time.time())
        entries[operation_id] = candidate
        client_only_store.write_entries(entries)
    return advance(operation_id)


def _save(row: dict[str, Any]) -> None:
    with persistence_lock():
        entries = client_only_store.read_entries()
        if entries.get(row["operation_id"], {}).get("status") == "cancelled":
            raise _Cancelled
        entries[row["operation_id"]] = copy.deepcopy(row)
        client_only_store.write_entries(entries)


def _end(row: dict[str, Any], status: str, error: TowError | None = None) -> Result:
    row.update(status=status, phase="failed" if status == "failed" else "done", finished_at=time.time())
    row.pop("blob", None)
    row.pop("proof", None)
    row.pop("policy", None)
    if error is not None:
        row["error"] = error.record()
    else:
        row.pop("error", None)
    try:
        _save(row)
    except _Cancelled:
        return _cancelled_result(row)
    return _result(row)


def _connection(row: dict[str, Any]) -> TorrentClientAdapter:
    cfg, secrets, context = _settings(row["client_id"])
    if row["install"] != _install() or row["scope"] != _scope(cfg, context, row["client_id"]):
        raise TowError("client_only.context_changed")
    floor = client_access.server_wait(load_state(quarantine=False), context, now=time.time())
    if floor:
        raise client_access.DeferredClientError(
            floor["code"], retry_after=floor["not_before"] - time.time(), observation_id=floor["id"]
        )
    adapter = factory.from_secrets(cfg, secrets, row["client_id"])
    if not hasattr(adapter, "operation_guard"):
        raise TowError("check.client_cannot_select")
    adapter.operation_guard = lambda: _guard(row)
    return adapter


def _guard(row: dict[str, Any]) -> None:
    with persistence_lock():
        live = client_only_store.read_entries().get(row["operation_id"])
        if live is None or live["status"] == "cancelled":
            raise _Cancelled
        if not live["created_at"] <= time.time() < live["deadline"]:
            raise TowError("client_only.expired")
        if live["status"] != "pending" or row.get("confirmation_only"):
            raise TowError("client_only.context_changed")
        cfg, _secrets, context = _settings(row["client_id"])
        if row["install"] != _install() or row["scope"] != _scope(cfg, context, row["client_id"]):
            raise TowError("client_only.context_changed")
        if refusal := save_path_refusal(row["save_path"], allow_unc=as_bool(cfg.get("allow_unc_save_paths"))):
            raise TowError(refusal)
        if row["local"] and (
            (row["volume"] is not None and volume_at(row["save_path"]) != row["volume"])
            or not seen_from_here(row["save_path"])
        ):
            raise TowError("client_only.volume_unavailable")
        floor = client_access.server_wait(load_state(quarantine=False), context, now=time.time())
        if floor:
            raise client_access.DeferredClientError(
                floor["code"], retry_after=floor["not_before"] - time.time(), observation_id=floor["id"]
            )


def _dispatch(row: dict[str, Any], key: str) -> None:
    with persistence_lock():
        entries = client_only_store.read_entries()
        live = entries.get(row["operation_id"])
        if live is not None and live["status"] == "cancelled":
            raise _Cancelled
        if live is None or live["status"] != "pending" or live[key]:
            raise TowError("client_only.context_changed")
        if not live["created_at"] <= time.time() < live["deadline"]:
            raise TowError("client_only.expired")
        cfg, _secrets, context = _settings(row["client_id"])
        if row["scope"] != _scope(cfg, context, row["client_id"]):
            raise TowError("client_only.context_changed")
        floor = client_access.server_wait(load_state(quarantine=False), context, now=time.time())
        if floor:
            raise client_access.DeferredClientError(
                floor["code"], retry_after=floor["not_before"] - time.time(), observation_id=floor["id"]
            )
        if row["local"] and row["volume"] is not None and volume_at(row["save_path"]) != row["volume"]:
            raise TowError("client_only.volume_unavailable")
        row[key] = True
        if key == "start_dispatched":
            row["start_at"] = time.time()
        row["phase"] = "observing_add" if key == "add_dispatched" else "observing_start"
        entries[row["operation_id"]] = copy.deepcopy(row)
        client_only_store.write_entries(entries)


def _find(client: TorrentClientAdapter, row: dict[str, Any]) -> dict[str, Any] | None:
    for identity in row["hashes"]:
        info = client.inspect_torrent(identity)
        if info is not None:
            if not matches_hash(info, identity) or str(info.get("hash") or "").upper() not in row["hashes"]:
                raise TowError("check.unexpected_identity")
            if not paths_equal(str(info.get("save_path") or ""), row["save_path"]):
                raise TowError("client.managed.wrong_folder")
            if str(info.get("state") or "").casefold() in {"error", "missingfiles", "unknown", ""}:
                raise TowError("client.managed.error_state", state=str(info.get("state") or "unknown"))
            row["hash"] = str(info["hash"]).upper()
            row["native_preparing"] = (
                str(info.get("state") or "").casefold().startswith(("checking", "allocating", "metadl", "forcedmetadl"))
            )
            return info
    return None


def _storage_guard(
    row: dict[str, Any], source: tuple[TorrentFile, ...], selected: Any, *, name: str = "", is_multi: bool = False
) -> None:
    if not row["local"]:
        return
    if (row["volume"] is not None and volume_at(row["save_path"]) != row["volume"]) or not seen_from_here(
        row["save_path"]
    ):
        raise TowError("client_only.volume_unavailable")
    shortfall = check.free_space_problem(
        row["save_path"], source, selected, name=name, is_multi=is_multi, volume=row["volume"]
    )
    if shortfall is not None:
        raise shortfall.error()


def _proof(info: dict[str, Any], metadata: TorrentMetadata, plan: SelectionPlan) -> dict[str, Any]:
    rows = info.get("files")
    if not isinstance(rows, list) or not rows:
        raise TowError("client.managed.no_files")
    mapping = files.map_files(
        metadata.files, rows, metadata.name, fail=lambda _path: TowError("client.managed.wrong_selection")
    )
    wanted = {mapping[index] for index in plan.selected_indices}
    ignored = {
        item["index"]
        for item in rows
        if item.get("index") not in mapping.values() and files.padding_like(item.get("name"))
    }
    files.verify_selection(rows, rows, wanted, ignored=ignored, fail=lambda: TowError("client.managed.wrong_selection"))
    return {
        "hash": str(info["hash"]).upper(),
        "files": [{key: item.get(key) for key in ("index", "name", "size", "priority")} for item in rows],
        "wanted": sorted(wanted),
        "ignored": sorted(ignored),
    }


def _prepare(row: dict[str, Any], client: TorrentClientAdapter) -> Result | None:
    blob = base64.b64decode(row["blob"], validate=True)
    metadata = parse_torrent_metadata(blob)
    plan = resolve_selection(metadata.files, row["policy"], preferred_season=row.get("preferred_season"))
    info = _find(client, row)
    if row.get("confirmation_only"):
        if info is None:
            raise TowError("client.managed.missing" if row.get("seen_existing") else "client.managed.not_visible")
        _proof(info, metadata, plan)
        return _end(row, "existing")
    if info is not None and not row["add_dispatched"]:
        row.update(confirmation_only=True, seen_existing=True)
        _save(row)
        _proof(info, metadata, plan)
        return _end(row, "existing")
    if info is None and not row["add_dispatched"]:
        if not all(
            client.capabilities.get(name) is True
            for name in ("stopped_add", "file_selection", "priority_readback", "start_stop", "recheck_files")
        ):
            raise TowError("check.client_cannot_select")
        _storage_guard(row, metadata.files, plan.selected_indices, name=metadata.name, is_multi=metadata.is_multi)
        _dispatch(row, "add_dispatched")
        try:
            client.add_torrent_selected(
                blob, row["save_path"], metadata.client_hash, plan.selected_indices, start=False
            )
        except ClientVerificationPending as pending_error:
            if pending_error.receipt:
                pending_info = _find(client, row)
                row["hash"] = pending_error.infohash
                row["proof"] = copy.deepcopy(pending_error.receipt)
                row["native_intent"] = {key: (pending_info or {}).get(key) for key in ("force_start", "auto_tmm")}
                row["phase"] = "checking"
                row.pop("blob", None)
                _save(row)
            raise
        info = _find(client, row)
    if info is None:
        raise TowError("client.managed.not_visible")
    if "tow" not in {str(tag).casefold() for tag in info.get("tags") or []}:
        raise TowError("client.managed.not_owned")
    row["proof"] = _proof(info, metadata, plan)
    row["phase"] = "ready"
    row.pop("blob", None)
    _save(row)
    return None


def _finish(row: dict[str, Any], client: TorrentClientAdapter) -> Result:
    proof = row["proof"]
    if proof.get("phase") == AWAITING_INITIAL_CHECK:
        observed = _find(client, row)
        if observed is None:
            raise TowError("client.managed.missing")
        if row.get("native_intent") != {key: observed.get(key) for key in ("force_start", "auto_tmm")}:
            raise TowError("client_only.context_changed")
        files.verify_identities(
            proof["files"], observed.get("files") or [], fail=lambda: TowError("client_only.context_changed")
        )
        ignored = set(proof.get("ignored") or [])
        before = files.priorities(
            [item for item in proof["files"] if item["index"] not in ignored],
            fail=lambda: TowError("client_only.context_changed"),
        )
        after = files.priorities(
            [item for item in observed.get("files") or [] if item["index"] not in ignored],
            fail=lambda: TowError("client_only.context_changed"),
        )
        if before != after or (
            not is_stopped(observed) and not str(observed.get("state") or "").casefold().startswith("checking")
        ):
            raise TowError("client_only.context_changed")
        proof["start"] = False
        checked = client.finish_verification(row["hash"], row["save_path"], proof, start_stopped=False)
        row["proof"] = {key: value for key, value in proof.items() if key not in {"phase", "start"}}
        row["proof"]["files"] = [
            {key: item.get(key) for key in ("index", "name", "size", "priority")} for item in checked.get("files") or []
        ]
        _save(row)
    info = _find(client, row)
    if info is None:
        raise TowError("client.managed.missing")
    if not row["start_dispatched"] and is_stopped(info):
        native_files = tuple(TorrentFile(item["index"], item["name"], item["size"]) for item in info.get("files") or [])
        _storage_guard(row, native_files, row["proof"]["wanted"])
        fingerprint = files.recovery_fingerprint(info.get("files") or [])
        if client.has_active_file_overlap(
            row["hash"], expected_save_path=row["save_path"], expected_files_fingerprint=fingerprint
        ):
            raise TowError("client_only.overlap")
    client.finish_verification(
        row["hash"],
        row["save_path"],
        row["proof"],
        start_stopped=not row["start_dispatched"],
        on_start=lambda: _dispatch(row, "start_dispatched"),
    )
    return _end(row, "added")


def _record_error(row: dict[str, Any], error: Exception) -> None:
    if isinstance(error, client_access.DeferredClientError):
        return
    with persistence_lock():
        cfg, _secrets, context = _settings(row["client_id"])
        if row["scope"] == _scope(cfg, context, row["client_id"]):
            state = load_state(quarantine=False)
            before = copy.deepcopy(state.get("client_access"))
            if client_access.observe(state, context, error, now=time.time()) and state.get("client_access") != before:
                save_state(state)


def _defer(row: dict[str, Any], error: Exception) -> Result:
    row["failures"] += not isinstance(error, ClientVerificationPending)
    delay = 10 if isinstance(error, ClientVerificationPending) else min(300, 5 * 2 ** min(row["failures"], 6))
    delay = max(delay, client_access.retry_after(error, now=time.time()))
    row["next_at"] = min(row["deadline"], time.time() + delay)
    if isinstance(error, TowError):
        row["error"] = error.record()
    else:
        row["error"] = TowError("client_only.connection_wait").record()
    _save(row)
    return _result(row)


def _temporary(error: Exception) -> bool:
    if client_access.temporary_http(error):
        return True
    if client_access.permanent_http(error):
        return False
    causes: list[BaseException] = []
    current: BaseException | None = error
    while current is not None and len(causes) < 8 and current not in causes:
        causes.append(current)
        current = current.__cause__ or current.__context__
    if any(
        isinstance(item, (ssl.SSLError, requests.exceptions.SSLError, requests.exceptions.ProxyError, httpx.ProxyError))
        for item in causes
    ):
        return False
    return any(
        isinstance(
            item,
            (
                ConnectionError,
                TimeoutError,
                requests.exceptions.Timeout,
                requests.exceptions.ConnectionError,
                httpx.TimeoutException,
                httpx.NetworkError,
            ),
        )
        for item in causes
    )


def _cancelled_result(row: dict[str, Any]) -> Result:
    result = get_result(row["operation_id"])
    if result is None:
        raise TowError("client_only.request_changed")
    return result


def _failure(error: Exception) -> TowError:
    if isinstance(error, TowError):
        return error
    record = record_of(error)
    if record is not None:
        return TowError(record["code"], cls=record.get("cls"), **record.get("params", {}))
    status = client_access.http_status(error)
    if status is not None:
        return TowError("client.managed.http_error", code=status)
    return TowError("client_only.failed")


def _advance_locked(row: dict[str, Any]) -> Result:
    try:
        client = _connection(row)
        if "proof" not in row:
            result = _prepare(row, client)
            if result is not None:
                return result
        return _finish(row, client)
    except _Cancelled:
        return _cancelled_result(row)
    except ClientVerificationPending as error:
        try:
            return _defer(row, error)
        except _Cancelled:
            return _cancelled_result(row)
    except Exception as error:  # noqa: BLE001 - every native failure gets a finite, visible outcome
        with contextlib.suppress(OSError, RuntimeError, ValueError):
            _record_error(row, error)
        transient = _temporary(error)
        unknown_add = (
            (row["add_dispatched"] or row.get("confirmation_only"))
            and isinstance(error, TowError)
            and error.code
            in {
                "client.managed.not_visible",
                "client.managed.start_unconfirmed",
                "client.managed.verification_pending",
            }
        )
        unknown_add = unknown_add or (
            isinstance(error, TowError)
            and error.code == "client.managed.no_files"
            and row.get("native_preparing") is True
        )
        try:
            if transient or unknown_add:
                return _defer(row, error)
            return _end(row, "failed", _failure(error))
        except _Cancelled:
            return _cancelled_result(row)


def advance(operation_id: str) -> Result:
    try:
        with check_run_lock(wait=False):
            with persistence_lock():
                row = client_only_store.read_entries().get(operation_id)
            if row is None:
                raise TowError("client_only.request_changed")
            if row["status"] != "pending":
                return _result(row)
            if not row["created_at"] <= time.time() < row["deadline"]:
                return _end(row, "failed", TowError("client_only.expired"))
            if row["next_at"] > time.time():
                return _result(row)
            return _advance_locked(row)
    except CheckBusyError:
        result = get_result(operation_id)
        if result is None:
            raise TowError("client_only.request_changed") from None
        return result


def advance_pending(*, limit: int = 4) -> list[Result]:
    client_only_store.sync_compatibility()
    return [advance(operation_id) for operation_id in pending()[: max(0, min(limit, client_only_store.MAX_RECEIPTS))]]
