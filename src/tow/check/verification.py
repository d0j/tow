"""Deferred client file verification: validated handover, then client-only confirmation.

The receipt binds the selected file identities to one revision, client and owner settings.
No tracker request, re-add, stop, move or priority reset is needed to finish it.
"""

from __future__ import annotations

import contextlib
import copy
from typing import TYPE_CHECKING, Any

from tow.check.client_ops import client_unreachable, info_confirms, info_owned_by_tow
from tow.check.rows import client_marks, set_error, stamp_result, withdrawn_meanwhile
from tow.clients import files
from tow.clients.managed import ClientVerificationPending
from tow.clients.spec import TorrentClientAdapter
from tow.clock import iso_now
from tow.errors import TowError
from tow.events import new_operation_id
from tow.i18n import t
from tow.jsonish import as_dict
from tow.log import error_fields, owner_language
from tow.records import Topic, topics_of

if TYPE_CHECKING:
    from tow.check.topic import CheckRun, TopicCheck

OWNER_REPAIR_CODES = frozenset(
    {
        "client.managed.not_owned",
        "client.managed.wrong_folder",
        "client.managed.hash_changed_selection",
        "client.managed.wrong_selection",
    }
)
TERMINAL_CODES = OWNER_REPAIR_CODES | {
    "client.managed.missing",
    "client.managed.error_state",
    "client.managed.start_unconfirmed",
}


def waiting_topics(state: dict[str, Any]) -> list[Topic]:
    return [
        topic
        for topic in topics_of(state)
        if as_dict(topic.get("verification_pending"))
        and not as_dict(topic.get("verification_pending")).get("blocked")
        and not topic.get("paused")
        and not topic.get("selection_dirty")
    ]


def pending_row(row: dict[str, Any], h: str) -> None:
    # Neither an actionable failure nor confirmed success. Do not stage an "added" message.
    row.update(ok=False, added=False, hash=h, status="verification_pending", verification_pending=True)
    row["skipped"] = t("check.verifying_files", owner_language())
    for key in ("error", "error_record", "error_class"):
        row.pop(key, None)


def blocked_row(topic: Topic, row: dict[str, Any], h: str) -> None:
    waiting = as_dict(topic.get("verification_pending"))
    error = TowError(
        str(waiting.get("blocked") or "check.selection_unconfirmed"), **as_dict(waiting.get("blocked_params"))
    )
    row.update(ok=False, changed=False, hash=h, status="failed")
    set_error(row, error)


def _stop_rejected(client: TorrentClientAdapter, h: str) -> None:
    """A rejected handover is not allowed to download; never stop an unrelated identity."""
    with contextlib.suppress(Exception):
        if info_owned_by_tow(client.inspect_torrent(h), h):
            client.stop_owned_torrent(h)


def defer_start(
    topic: Topic,
    run: CheckRun,
    row: dict[str, Any],
    client: TorrentClientAdapter,
    *,
    h: str,
    client_id: str,
    destination: str,
    before: dict[str, Any],
    kind: str,
) -> None:
    try:
        _defer_start(
            topic, run, row, client, h=h, client_id=client_id, destination=destination, before=before, kind=kind
        )
    except Exception:
        _stop_rejected(client, h)
        topic.pop("waiting_space", None)  # a refused selection must not trigger a start/stop loop
        raise


def _defer_start(
    topic: Topic,
    run: CheckRun,
    row: dict[str, Any],
    client: TorrentClientAdapter,
    *,
    h: str,
    client_id: str,
    destination: str,
    before: dict[str, Any],
    kind: str,
) -> None:
    """A verified, disk-space-waiting task started checking: retain its exact selection."""
    after = client.inspect_torrent(h)
    if not info_confirms(after, h, destination, require_tow_ownership=True):
        raise TowError("check.start_unconfirmed")
    rows = list(before.get("files") or [])
    wanted = {
        index
        for index, priority in files.priorities(rows, fail=lambda: TowError("check.selection_unconfirmed")).items()
        if priority > 0
    }
    if not rows or not wanted:
        raise TowError("check.selection_unconfirmed")
    files.verify_selection(
        rows, list((after or {}).get("files") or []), wanted, fail=lambda: TowError("check.selection_unconfirmed")
    )
    topic["verification_pending"] = {
        "hash": h,
        "client": client_id,
        "path": destination,
        "owner": list(client_marks(topic)),
        "since": iso_now(),
        "kind": kind,
        "selection_update": False,
        "completion_event": "client_started",
        "resume_after_check": True,
        "receipt": {
            "hash": h,
            "wanted": sorted(wanted),
            "ignored": [],
            "files": [{key: item.get(key) for key in ("index", "name", "size", "priority")} for item in rows],
        },
    }
    if topic.get("tracking_mode") == "once":
        topic["once_done"] = False
    topic.pop("waiting_space", None)
    pending_row(row, h)
    run.record(
        "client_verification_pending", topic=topic.get("id"), client_id=client_id, hash=h, status="pending", how=run.how
    )


def defer(work: TopicCheck, error: ClientVerificationPending, metadata: Any, plan: Any) -> str:
    from tow.check_steps import client_identities

    try:
        return _defer(work, error, metadata, plan)
    except Exception:
        if work.client is not None and error.infohash in client_identities(metadata):
            _stop_rejected(work.client, error.infohash)
        raise


def _defer(work: TopicCheck, error: ClientVerificationPending, metadata: Any, plan: Any) -> str:
    """Independently validate the adapter's receipt before making this revision current."""
    from tow.check_steps import client_identities

    client, receipt = work.client, error.receipt
    h = error.infohash
    dest = str(work.topic.get("save_path") or "")
    if client is None or not receipt or h not in client_identities(metadata):
        raise TowError("check.add_unconfirmed") from error
    info = client.inspect_torrent(h)
    if not info_confirms(info, h, dest, require_tow_ownership=True):
        raise TowError("check.add_unconfirmed") from error

    def fail() -> TowError:
        return TowError("check.selection_unconfirmed")

    rows = list(receipt.get("files") or [])
    ignored = set(receipt.get("ignored") or [])
    if receipt.get("all_files"):
        source = {item.index for item in metadata.files if not item.is_pad}
        allowed_ignored = {row["index"] for row in rows if files.padding_like(row.get("name"))}
        wanted = {row["index"] for row in rows if row["index"] not in allowed_ignored}
        if (
            client.client_kind != "qbittorrent"
            or set(plan.selected_indices) != source
            or len(wanted) != len(source)
            or any(item.is_pad or files.padding_like(item.path) for item in metadata.files)
        ):
            raise fail()
    else:
        mapping = files.map_files(metadata.files, rows, metadata.name, fail=lambda _path: fail())
        wanted = {mapping[index] for index in plan.selected_indices}
        allowed_ignored = {
            row["index"] for row in rows if row["index"] not in mapping.values() and files.padding_like(row.get("name"))
        }
    if (
        str(receipt.get("hash") or "").upper() != h
        or set(receipt.get("wanted") or []) != wanted
        or not ignored.issubset(allowed_ignored)
    ):
        raise fail()
    files.verify_selection(rows, list((info or {}).get("files") or []), wanted, ignored=ignored, fail=fail)
    waiting_space = as_dict(work.topic.get("waiting_space"))
    waited_for_space = str(waiting_space.get("hash") or "").upper() == h
    previous = as_dict(work.topic.get("verification_pending"))
    retries_same = (
        bool(work.row.get("verification_repair"))
        and str(previous.get("hash") or "").upper() == h
        and not work.row.get("readd_removed")
    )
    work.topic["verification_pending"] = {
        "hash": h,
        "client": work.client_id,
        "path": dest,
        "owner": list(client_marks(work.topic)),
        "receipt": copy.deepcopy(receipt),
        "since": iso_now(),
        "kind": str(waiting_space.get("kind") or "added")
        if waited_for_space
        else "updated"
        if work.old and h != work.old
        else "added",
        "selection_update": h == work.old
        and info is not None
        and not waited_for_space
        and not work.row.get("readd_removed")
        and "tow-pending" not in {str(tag).strip().casefold() for tag in info.get("tags") or []},
    }
    if waited_for_space:
        work.topic["verification_pending"].update(completion_event="client_started", resume_after_check=True)
    elif retries_same:
        work.topic["verification_pending"].update(
            kind=str(previous.get("kind") or "added"),
            selection_update=bool(previous.get("selection_update")),
        )
        for key in ("completion_event", "resume_after_check"):
            if key in previous:
                work.topic["verification_pending"][key] = previous[key]
    if work.topic.get("tracking_mode") == "once":
        work.topic["once_done"] = False
    pending_row(work.row, h)
    work.run.record(
        "client_verification_pending",
        operation_id=work.operation_id,
        component="client",
        integration_id=work.client_id,
        client_id=work.client_id,
        topic_id=work.topic.get("id"),
        topic=work.topic.get("id"),
        hash=h,
        status="pending",
        how=work.run.how,
    )
    return h


def _confirm(
    topic: Topic,
    run: CheckRun,
    waiting: dict[str, Any],
    client: TorrentClientAdapter | None,
    client_id: str,
    h: str,
) -> bool:
    if client is None:
        raise client_unreachable(run, client_id)
    info = client.inspect_torrent(h)
    if run.readd_removed and (info is None or str(waiting.get("blocked") or "") in OWNER_REPAIR_CODES):
        return False  # only the owner's explicit row check may re-add or repair altered evidence
    # Retry a dropped start once, only for an unfinished TOW add; a later explicit check
    # may retry it again. Never resume an ordinary torrent paused in the client by its owner.
    stopped = str((info or {}).get("state") or "").casefold().startswith(("stopped", "paused"))
    pending_mark = "tow-pending" in {str(tag).casefold().strip() for tag in (info or {}).get("tags") or []}
    retry = stopped and (
        ((pending_mark or waiting.get("resume_after_check")) and not waiting.get("resume_attempted"))
        or run.readd_removed
    )
    finish = getattr(client, "finish_verification", None)
    if not callable(finish):
        raise TowError("check.selection_unconfirmed")
    finish(
        h,
        str(waiting.get("path") or ""),
        waiting.get("receipt") or {},
        start_stopped=retry,
        on_start=lambda: waiting.__setitem__("resume_attempted", True),
    )
    return True


def _restore_check_times(topic: Topic, previous_check: str | None, previous_ok_at: str | None) -> None:
    if previous_check is None:
        topic.pop("last_check", None)
    else:
        topic["last_check"] = previous_check
    if previous_ok_at is None:
        topic.pop("last_ok_at", None)
    else:
        topic["last_ok_at"] = previous_ok_at


def recheck(topic: Topic, run: CheckRun, row: dict[str, Any], *, pass_only: bool = False) -> bool:
    """Return True when this pending transaction alone supplied the row (no site traffic).

    An unexpected failure is this topic's result, never the end of the run: the receipt is
    kept for the next pass and nothing else is asked of the site or the client for it now."""
    previous_check = topic.get("last_check")
    previous_ok_at = topic.get("last_ok_at")
    try:
        return _recheck(topic, run, row, pass_only=pass_only)
    except Exception as error:  # noqa: BLE001 - one topic's defect must not abort every other topic
        before = (topic.get("last_error_code"), topic.get("last_error_params"), topic.get("last_error"))
        row.update(ok=False, status="failed")
        set_error(row, error)
        stamp_result(topic, row)
        if pass_only:
            _restore_check_times(topic, previous_check, previous_ok_at)
        if before != (topic.get("last_error_code"), topic.get("last_error_params"), topic.get("last_error")):
            run.record("check_fail", topic=topic.get("id"), **error_fields(error), how=run.how)
        if run.notify:
            run.queue_notification(topic, kind="error", operation_id=new_operation_id("check-error"), error=error)
        return True


def _recheck(topic: Topic, run: CheckRun, row: dict[str, Any], *, pass_only: bool) -> bool:
    waiting = as_dict(topic.get("verification_pending"))
    if not isinstance(waiting, dict) or not waiting or topic.get("selection_dirty"):
        return False
    h = str(waiting.get("hash") or "").upper()
    client_id, client = run.get_client_for(topic)
    if (
        h != str(topic.get("hash") or "").upper()
        or waiting.get("client") != client_id
        or waiting.get("owner") != list(client_marks(topic))
    ):
        if run.apply:
            topic.pop("verification_pending", None)
        return False  # owner's new settings need an ordinary check, never this old receipt
    if waiting.get("blocked") and not run.readd_removed:
        if pass_only:
            blocked_row(topic, row, h)
        return pass_only  # no fast retry; ordinary site checks can still find a new revision
    row.update(hash=h, changed=False, client_id=client_id, client_kind=client.client_kind if client else None)
    if topic.get("paused") or (run.apply and withdrawn_meanwhile(topic, row, h, client_marks(topic))):
        return True
    if not run.apply:
        pending_row(row, h)
        return True
    previous_check = topic.get("last_check")
    previous_ok_at = topic.get("last_ok_at")
    before_error = (topic.get("last_error_code"), topic.get("last_error_params"), topic.get("last_error"))
    try:
        if not _confirm(topic, run, waiting, client, client_id, h):
            row["verification_repair"] = True  # request-local intent, not a persisted selection edit
            return False
    except ClientVerificationPending as error:
        if not error.receipt:
            row.update(ok=False, status="failed")
            set_error(row, error)
        else:
            waiting.pop("blocked", None)
            waiting.pop("blocked_params", None)
            pending_row(row, h)
    except Exception as error:  # noqa: BLE001 - one failed confirmation never ends the batch
        code = getattr(error, "code", None)
        if code in TERMINAL_CODES:
            waiting["blocked"] = str(code)  # retain reason independently of reconcile errors
            waiting["blocked_params"] = error_fields(error).get("error_params") or {}
        row.update(ok=False, status="failed")
        set_error(row, error)
        stamp_result(topic, row)
        if before_error != (topic.get("last_error_code"), topic.get("last_error_params"), topic.get("last_error")):
            run.record("check_fail", topic=topic.get("id"), **error_fields(error), how=run.how)
        if run.notify:
            run.queue_notification(topic, kind="error", operation_id=new_operation_id("check-error"), error=error)
    else:
        topic.pop("verification_pending", None)
        if str(topic.get("tracking_mode") or "watch") == "once":
            topic["once_done"] = True
        row.update(ok=True, status="succeeded", added=not waiting.get("selection_update"), verification_pending=False)
        row.pop("skipped", None)
        for key in ("error", "error_record", "error_class"):
            row.pop(key, None)
        run.record(
            str(
                waiting.get("completion_event")
                or ("client_selection_updated" if waiting.get("selection_update") else "client_added")
            ),
            operation_id=new_operation_id("client-confirm"),
            component="client",
            integration_id=client_id,
            client_id=client_id,
            topic_id=topic.get("id"),
            topic=topic.get("id"),
            hash=h,
            status="succeeded",
            reason="verification_finished",
            how=run.how,
        )
        if run.notify and not waiting.get("selection_update"):
            run.queue_notification(
                topic, kind=str(waiting.get("kind") or "added"), operation_id=new_operation_id("bot")
            )
    stamp_result(topic, row)
    if pass_only:
        _restore_check_times(topic, previous_check, previous_ok_at)
    return True
