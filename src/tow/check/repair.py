"""Bounded continuation of one explicit request to recover a torrent's selected files."""

from __future__ import annotations

import shutil
import time
from typing import TYPE_CHECKING, Any

from tow import file_repair
from tow.check.client_ops import claimed_by_other_topic
from tow.check.rows import set_error
from tow.clients.spec import TorrentClientAdapter
from tow.clients.state import is_stopped
from tow.errors import Msg, TowError
from tow.i18n import t
from tow.log import error_fields, owner_language
from tow.records import Topic

if TYPE_CHECKING:
    from tow.check.topic import CheckRun

CONFIRM_SEC = 120
BACKOFF_SEC = 60
_RUNNING = frozenset(
    {"downloading", "uploading", "seeding", "stalleddl", "stalledup", "queueddl", "queuedup", "forceddl", "forcedup"}
)
PHASE_KEYS = {
    "requested": "repair.phase.requested",
    "checking": "repair.phase.checking",
    "resuming": "repair.phase.resuming",
    "downloading": "repair.phase.downloading",
}


def waiting_topics(state: dict[str, Any]) -> list[Topic]:
    return file_repair.waiting_topics(state)


def _checking(info: dict[str, Any]) -> bool:
    return str(info.get("state") or "").casefold().startswith("checking")


def _running(info: dict[str, Any]) -> bool:
    return str(info.get("state") or "").casefold() in _RUNNING and not is_stopped(info)


def _show(row: dict[str, Any], receipt: dict[str, Any]) -> None:
    row.update(ok=False, changed=False, status="repair_pending", repair_pending=True, repair_phase=receipt["phase"])
    if receipt.get("blocked"):
        row.update(status="failed")
        row.pop("skipped", None)
        set_error(row, TowError(str(receipt["blocked"]), **dict(receipt.get("blocked_params") or {})))
    else:
        row["skipped"] = t(PHASE_KEYS[receipt["phase"]], owner_language())
        for key in ("error", "error_record", "error_class"):
            row.pop(key, None)


def _save(topic: Topic, receipt: dict[str, Any], **changes: Any) -> bool:
    candidate = {**receipt, **changes, "updated_at": time.time()}
    if not file_repair.persist(topic, candidate, expected_id=receipt["id"]):
        return False
    receipt.clear()
    receipt.update(candidate)
    return True


def _record_repair(run: CheckRun, topic: Topic, receipt: dict[str, Any], kind: str, **extra: Any) -> None:
    run.record(
        kind,
        operation_id=f"repair-{receipt['id']}",
        component="client/filesystem",
        topic=topic.get("id"),
        topic_id=topic.get("id"),
        title=topic.get("title"),
        hash=receipt["hash"],
        client_id=receipt["client"],
        integration_id=receipt["client"],
        n=receipt["count"],
        how=run.how,
        **extra,
    )


def _block(topic: Topic, run: CheckRun, row: dict[str, Any], receipt: dict[str, Any], error: TowError) -> None:
    fields = error_fields(error)
    if _save(
        topic, receipt, phase="blocked", blocked=error.code, blocked_params=fields.get("error_params") or {}, next_at=0
    ):
        _record_repair(run, topic, receipt, "files_repair_failed", **fields)
    _show(row, receipt)


def _preconditions(
    topic: Topic, run: CheckRun, client: TorrentClientAdapter, receipt: dict[str, Any], info: dict[str, Any]
) -> dict[str, int]:
    file_repair.validate_info(topic, receipt, info)
    if receipt["client"] in getattr(run, "remote_clients", frozenset()):
        raise TowError("repair.volume_unavailable")
    evidence = file_repair.evidence(info, receipt)
    if evidence["unknown"]:
        raise TowError("repair.volume_unavailable")
    return evidence


def _command_guard(
    topic: Topic, run: CheckRun, client: TorrentClientAdapter, receipt: dict[str, Any], info: dict[str, Any]
) -> dict[str, int]:
    evidence = _preconditions(topic, run, client, receipt, info)
    if claimed_by_other_topic(run.state, topic, str(receipt["hash"])):
        raise TowError("repair.changed")
    if client.has_active_file_overlap(
        str(receipt["hash"]), expected_save_path=str(receipt["path"]), expected_files_fingerprint=str(receipt["files"])
    ):
        raise TowError("repair.overlap")
    try:
        free = shutil.disk_usage(str(receipt["path"])).free
    except OSError as error:
        raise TowError("repair.volume_unavailable") from error
    if evidence["remaining"] and free < evidence["remaining"] + 512 * 1024 * 1024:
        raise TowError("repair.space", bytes=evidence["remaining"])
    return evidence


def _verify(
    topic: Topic, run: CheckRun, receipt: dict[str, Any], client: TorrentClientAdapter, info: dict[str, Any]
) -> None:
    observed = _preconditions(topic, run, client, receipt, info)
    if not (observed["stale_claims"] or observed["partial_claims"]):
        if _running(info):
            _save(topic, receipt, phase="downloading", running_existing=True, running_seen=True, check_required=False)
        elif is_stopped(info):
            if _save(topic, receipt, check_required=False):
                _resume(topic, run, receipt, client, info)
        else:
            raise TowError("repair.client_busy")
        return  # the client already knows every absent file is at zero; no cache invalidation needed
    if claimed_by_other_topic(run.state, topic, str(receipt["hash"])):
        raise TowError("repair.changed")
    now = time.time()
    if not is_stopped(info):
        if not _running(info):
            raise TowError("repair.client_busy")
        if receipt.get("stop_dispatched"):
            if now - receipt["stop_at"] >= CONFIRM_SEC:
                raise TowError("repair.dispatch_unconfirmed")
            return
        if not _save(topic, receipt, stop_dispatched=True, stop_at=now):
            return
        client.stop_owned_torrent(str(receipt["hash"]))
        info = client.inspect_torrent(str(receipt["hash"])) or {}
        _preconditions(topic, run, client, receipt, info)
        if not is_stopped(info):
            return
    _command_guard(topic, run, client, receipt, info)
    if not _save(
        topic,
        receipt,
        phase="checking",
        recheck_dispatched=True,
        check_at=now,
        before_progress=file_repair.progress_signature(info),
    ):
        return
    client.recheck_owned_torrent(
        str(receipt["hash"]), expected_save_path=str(receipt["path"]), expected_files_fingerprint=str(receipt["files"])
    )
    _record_repair(run, topic, receipt, "files_repair_started")
    after = client.inspect_torrent(str(receipt["hash"])) or {}
    file_repair.validate_info(topic, receipt, after)
    if _checking(after):
        _save(topic, receipt, saw_checking=True)


def _resume(
    topic: Topic, run: CheckRun, receipt: dict[str, Any], client: TorrentClientAdapter, info: dict[str, Any]
) -> None:
    if receipt.get("resume_dispatched"):
        return
    _command_guard(topic, run, client, receipt, info)
    if not _save(topic, receipt, phase="resuming", resume_dispatched=True, resume_at=time.time()):
        return
    try:
        after = client.resume_repaired_torrent(
            str(receipt["hash"]),
            expected_save_path=str(receipt["path"]),
            expected_files_fingerprint=str(receipt["files"]),
        )
    except TowError as error:
        if error.code != "client.managed.repair_start_unconfirmed":
            raise
        return  # native start was sent; subsequent passes observe without ever resending
    file_repair.validate_info(topic, receipt, after)
    if _running(after) and _save(topic, receipt, phase="downloading", running_seen=True):
        _record_repair(run, topic, receipt, "files_repair_resumed")


def _checking_step(
    topic: Topic, run: CheckRun, receipt: dict[str, Any], client: TorrentClientAdapter, info: dict[str, Any]
) -> None:
    if _checking(info):
        if not receipt.get("saw_checking"):
            _save(topic, receipt, saw_checking=True)
        return
    observed = file_repair.progress_signature(info) != receipt["before_progress"]
    if not observed:
        if time.time() - receipt["check_at"] >= CONFIRM_SEC:
            raise TowError("repair.dispatch_unconfirmed")
        return
    evidence = _preconditions(topic, run, client, receipt, info)
    if not any(evidence[key] for key in ("absent", "unknown", "incomplete")):
        file_repair.persist(topic, None, expected_id=receipt["id"])
    elif is_stopped(info):
        _resume(topic, run, receipt, client, info)
    elif _running(info) and _save(topic, receipt, phase="downloading", running_seen=True):
        _record_repair(run, topic, receipt, "files_repair_resumed")
    else:
        raise TowError("repair.client_busy")


def _follow(
    topic: Topic, run: CheckRun, receipt: dict[str, Any], client: TorrentClientAdapter, info: dict[str, Any]
) -> None:
    state = str(info.get("state") or "").casefold()
    if _checking(info) or state == "allocating":
        if receipt["phase"] == "resuming" and time.time() - receipt["resume_at"] >= CONFIRM_SEC:
            raise TowError("repair.resume_unconfirmed")
        return
    if state in {"error", "missingfiles"}:
        raise TowError("client.managed.error_state", state=state)
    if state == "moving":
        raise TowError("repair.client_busy")
    if not (_running(info) or is_stopped(info)):
        raise TowError("client.managed.start_unconfirmed")
    evidence = _preconditions(topic, run, client, receipt, info)
    if receipt["phase"] == "blocked" and not _running(info):
        return  # migration lifts only this false failure with fresh positive start evidence
    if not any(evidence[key] for key in ("absent", "unknown", "incomplete")):
        file_repair.persist(topic, None, expected_id=receipt["id"])
        return
    if _running(info):
        if receipt["phase"] in {"resuming", "blocked"} and _save(
            topic, receipt, phase="downloading", running_seen=True, blocked="", blocked_params={}, next_at=0
        ):
            _record_repair(run, topic, receipt, "files_repair_resumed")
        return
    if is_stopped(info):
        if receipt["phase"] == "downloading":
            raise TowError("repair.client_stopped")
        if time.time() - receipt["resume_at"] >= CONFIRM_SEC:
            raise TowError("repair.resume_unconfirmed")
        return
    raise TowError("repair.client_busy")


def _step(topic: Topic, run: CheckRun, row: dict[str, Any], receipt: dict[str, Any]) -> None:
    now = time.time()
    if now < receipt["requested_at"] or now >= receipt["deadline"]:
        raise TowError("repair.deadline")
    if now < receipt["next_at"]:
        return
    client_id, client = run.get_client_for(topic)
    if client is None:
        raise OSError("torrent client unavailable")
    if client_id != receipt["client"]:
        raise TowError("repair.changed")
    info = client.inspect_torrent(str(receipt["hash"])) or {}
    file_repair.validate_info(topic, receipt, info)
    if receipt["phase"] == "requested":
        if _checking(info):
            raise TowError("repair.client_busy")
        _verify(topic, run, receipt, client, info)
    elif receipt["phase"] == "checking":
        _checking_step(topic, run, receipt, client, info)
    else:
        _follow(topic, run, receipt, client, info)


def recheck(topic: Topic, run: CheckRun, row: dict[str, Any]) -> bool:
    receipt = file_repair.receipt_of(topic)
    if not receipt:
        if topic.get("repair_pending"):
            row.update(ok=False, changed=False, status="failed", repair_pending=True)
            set_error(row, TowError("repair.unavailable"))
            return True  # malformed authority never falls through to an ordinary applying check
        return False
    if not file_repair.current(topic, receipt):
        if run.apply and file_repair.cancel(str(topic.get("id") or ""), expected_id=receipt["id"]):
            topic.pop("repair_pending", None)
        return False
    _show(row, receipt)
    observing_start = file_repair.start_confirmation_pending(topic)
    if receipt.get("blocked") and not observing_start:
        return False  # a terminal repair never disables ordinary tracker observation
    if not run.apply:
        return not observing_start
    try:
        _step(topic, run, row, receipt)
    except TowError as error:
        _block(topic, run, row, receipt, error)
    except OSError, RuntimeError, ValueError, TypeError:
        attempts = min(file_repair.MAX_ATTEMPTS, receipt["attempts"] + 1)
        if attempts >= file_repair.MAX_ATTEMPTS:
            _block(topic, run, row, receipt, TowError("repair.failed", reason=Msg("repair.unavailable")))
        else:
            _save(topic, receipt, attempts=attempts, next_at=time.time() + BACKOFF_SEC * 2 ** (attempts - 1))
    if topic.get("repair_pending"):
        _show(row, receipt)
    else:
        row.update(ok=True, status="succeeded", repair_pending=False, repair_finished=True)
    return not (observing_start and receipt.get("blocked"))
