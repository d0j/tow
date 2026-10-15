"""Deferred client file verification: validated handover, then client-only confirmation.

The receipt binds the selected file identities to one revision, client and owner settings.
No tracker request, re-add, stop, move or priority reset is needed to finish it.
"""

from __future__ import annotations

import contextlib
import copy
from typing import TYPE_CHECKING, Any

from tow.check.client_ops import client_unreachable, info_confirms, info_owned_by_tow
from tow.check.rows import client_marks, removal_seen_by, set_error, stamp_result, withdrawn_meanwhile
from tow.clients import files
from tow.clients.managed import AWAITING_INITIAL_CHECK, ClientVerificationPending
from tow.clients.spec import TorrentClientAdapter
from tow.clock import machine_now, parse_timestamp
from tow.errors import Msg, TowError
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
# A torrent its client does not list counts as removed only after two answers of the client at
# least this far apart: a client still loading its torrents after a restart lists some late.
MISSING_CONFIRM_SEC = 600
# A verification still waiting after this long is the owner's to look at (a red client error),
# not a reason to drop, replace or finish the receipt: it is still confirmed when the client is done.
OVERDUE_SEC = 12 * 3600
# Where a receipt whose selection is not applied yet (``AWAITING_INITIAL_CHECK``) is saved: never
# as ``receipt``, which TOW 1.30.10 and older read as an applied, read-back selection - an add of
# all files would pass that, and they would start it without measuring disk space. Without a
# ``receipt`` they refuse it (a selection they cannot confirm) before asking anything of the client.
UNAPPLIED_RECEIPT = "unapplied_receipt"


class _NotListedYet(Exception):
    """The client answered without the torrent; its removal is not confirmed yet."""


def waiting_topics(state: dict[str, Any]) -> list[Topic]:
    return [
        topic
        for topic in topics_of(state)
        if as_dict(topic.get("verification_pending"))
        and not as_dict(topic.get("verification_pending")).get("blocked")
        and not topic.get("paused")
        and not topic.get("selection_dirty")
    ]


def _stamp() -> str:
    return machine_now().isoformat(timespec="seconds")


def receipt_of(waiting: dict[str, Any]) -> Any:
    """The receipt a pass confirms: the applied one, else the one handed over before its
    selection was applied (``UNAPPLIED_RECEIPT``)."""
    return waiting.get("receipt") if "receipt" in waiting else waiting.get(UNAPPLIED_RECEIPT)


def _keep_receipt(waiting: dict[str, Any], receipt: dict[str, Any]) -> None:
    """Save ``receipt`` under the one key its phase belongs to (``UNAPPLIED_RECEIPT``)."""
    waiting.pop("receipt", None)
    waiting.pop(UNAPPLIED_RECEIPT, None)
    unapplied = receipt.get("phase") == AWAITING_INITIAL_CHECK
    waiting[UNAPPLIED_RECEIPT if unapplied else "receipt"] = copy.deepcopy(receipt)


def _anchor(previous: dict[str, Any]) -> str:
    """When this transaction's verification began: kept across its re-deferrals, so a check
    that never ends cannot postpone the overdue alert. A missing, unreadable or future value
    (a clock set back) starts from now."""
    try:
        since = parse_timestamp(str(previous.get("since") or ""))
    except TypeError, ValueError, OverflowError:
        return _stamp()
    return str(previous["since"]) if since <= machine_now() else _stamp()


def _overdue(waiting: dict[str, Any]) -> bool:
    """The client has been checking (or not starting) this transaction for OVERDUE_SEC."""
    since = _anchor(waiting)
    waiting["since"] = since
    return (machine_now() - parse_timestamp(since)).total_seconds() >= OVERDUE_SEC


def pending_row(row: dict[str, Any], h: str) -> None:
    # Neither an actionable failure nor confirmed success. Do not stage an "added" message.
    row.update(ok=False, added=False, hash=h, status="verification_pending", verification_pending=True)
    row["skipped"] = t("check.verifying_files", owner_language())
    for key in ("error", "error_record", "error_class"):
        row.pop(key, None)


def awaits_client(topic: Topic, row: dict[str, Any]) -> bool:
    """The receipt still waits for its client after this look: not finished, blocked or withdrawn."""
    waiting = as_dict(topic.get("verification_pending"))
    return bool(waiting) and not waiting.get("blocked") and not row.get("ok")


def blocked_row(topic: Topic, row: dict[str, Any], h: str, *, by_hand_only: bool = False) -> None:
    """The row of a handover its client failed. ``by_hand_only``: the site was not asked for a
    newer revision (a daily download limit, no way to ask without a download) - the row says
    that a check started by hand looks for one."""
    waiting = as_dict(topic.get("verification_pending"))
    error = TowError(
        str(waiting.get("blocked") or "check.selection_unconfirmed"), **as_dict(waiting.get("blocked_params"))
    )
    if by_hand_only:
        error = TowError("check.two_errors", first=error, second=Msg("check.blocked_by_hand"), cls=error.error_class)
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
    except Exception as error:
        _stop_rejected(client, h)
        _block_start_handover(topic, error, h=h, client_id=client_id, destination=destination, before=before, kind=kind)
        topic.pop("waiting_space", None)  # never an automatically runnable wait again: no start/stop loop
        raise


def _start_receipt(h: str, before: dict[str, Any]) -> dict[str, Any] | None:
    """The receipt of the selection validated before the start (None: it cannot be built)."""
    rows = list(before.get("files") or [])
    try:
        priorities = files.priorities(rows, fail=lambda: TowError("check.selection_unconfirmed"))
    except TowError:
        return None
    wanted = {index for index, priority in priorities.items() if priority > 0}
    if not rows or not wanted:
        return None
    return {
        "hash": h,
        "wanted": sorted(wanted),
        "ignored": [],
        "files": [{key: item.get(key) for key in ("index", "name", "size", "priority")} for item in rows],
    }


def _block_start_handover(
    topic: Topic,
    error: Exception,
    *,
    h: str,
    client_id: str,
    destination: str,
    before: dict[str, Any],
    kind: str,
) -> None:
    """The start of a torrent that waited for disk space failed its validation (D5g): the
    handover stays on record, bound to its hash, client, owner settings, folder and the selection
    validated before the start, blocked with the original failure. No pass finishes or restarts
    it (``repair_only``): a check started by hand reconstructs the evidence."""
    code = getattr(error, "code", None)
    record: dict[str, Any] = {
        "hash": h,
        "client": client_id,
        "path": destination,
        "owner": list(client_marks(topic)),
        "since": _stamp(),
        "kind": kind,
        "selection_update": False,
        "completion_event": "client_started",
        "resume_after_check": True,
        "repair_only": True,
        "blocked": str(code) if isinstance(code, str) and code else "check.start_unconfirmed",
        "blocked_params": (error_fields(error).get("error_params") or {}) if code else {},
    }
    if (receipt := _start_receipt(h, before)) is not None:
        record["receipt"] = receipt
    topic["verification_pending"] = record
    if topic.get("tracking_mode") == "once":
        topic["once_done"] = False  # not done: a one-time topic is checked again until it is


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
    receipt = _start_receipt(h, before)
    if receipt is None:
        raise TowError("check.selection_unconfirmed")
    files.verify_selection(
        receipt["files"],
        list((after or {}).get("files") or []),
        set(receipt["wanted"]),
        fail=lambda: TowError("check.selection_unconfirmed"),
    )
    topic["verification_pending"] = {
        "hash": h,
        "client": client_id,
        "path": destination,
        "owner": list(client_marks(topic)),
        "since": _stamp(),
        "kind": kind,
        "selection_update": False,
        "completion_event": "client_started",
        "resume_after_check": True,
        "receipt": receipt,
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
    awaiting = receipt.get("phase") == AWAITING_INITIAL_CHECK
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
        or (awaiting and (receipt.get("all_files") or type(receipt.get("start")) is not bool))
    ):
        raise fail()
    if awaiting:  # not applied yet: the client checks the data it found (Transmission)
        files.verify_identities(rows, list((info or {}).get("files") or []), fail=fail)
    else:
        files.verify_selection(rows, list((info or {}).get("files") or []), wanted, ignored=ignored, fail=fail)
    waiting_space = as_dict(work.topic.get("waiting_space"))
    waited_for_space = str(waiting_space.get("hash") or "").upper() == h
    previous = as_dict(work.topic.get("verification_pending"))
    retries_same = (
        bool(work.row.get("verification_repair"))
        and str(previous.get("hash") or "").upper() == h
        and not work.row.get("readd_removed")
    )
    same_transaction = (
        str(previous.get("hash") or "").upper() == h
        and previous.get("client") == work.client_id
        and previous.get("owner") == list(client_marks(work.topic))
        and not work.row.get("readd_removed")
    )
    work.topic["verification_pending"] = {
        "hash": h,
        "client": work.client_id,
        "path": dest,
        "owner": list(client_marks(work.topic)),
        "since": _anchor(previous) if same_transaction else _stamp(),
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
    _keep_receipt(work.topic["verification_pending"], receipt)
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
    if error.start_attempted:
        work.topic["verification_pending"]["resume_attempted"] = True  # the receipt's one start was given
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


def _observe_presence(waiting: dict[str, Any], info: dict[str, Any] | None) -> None:
    """Count the client's answers without the torrent (``missing_since``, ``missing_seen`` on
    the receipt); any answer with it clears them. Raises ``_NotListedYet`` until two answers at
    least MISSING_CONFIRM_SEC apart did not list it - a client that did not answer is no answer."""
    if info is not None:
        waiting.pop("missing_since", None)
        waiting.pop("missing_seen", None)
        return
    now = machine_now()
    seen = waiting.get("missing_seen")
    seen = seen if type(seen) is int and seen > 0 else 0
    try:
        elapsed = (now - parse_timestamp(str(waiting.get("missing_since") or ""))).total_seconds()
    except TypeError, ValueError, OverflowError:
        elapsed = -1.0
    if not seen or elapsed < 0:  # its first answer without it, or the clock was set back since
        waiting["missing_since"] = now.isoformat(timespec="seconds")
        seen, elapsed = 0, 0.0
    waiting["missing_seen"] = seen + 1
    if waiting["missing_seen"] < 2 or elapsed < MISSING_CONFIRM_SEC:
        raise _NotListedYet


def _confirm(
    topic: Topic,
    run: CheckRun,
    waiting: dict[str, Any],
    client: TorrentClientAdapter | None,
    client_id: str,
    h: str,
) -> dict[str, Any] | None:
    """The client's confirmation of the receipt; None: the owner's check repairs it instead."""
    if client is None:
        raise client_unreachable(run, client_id)
    info = client.inspect_torrent(h)
    if run.readd_removed and (
        (info is None and removal_seen_by(topic, run.readd_since))
        or str(waiting.get("blocked") or "") in OWNER_REPAIR_CODES
        or waiting.get("repair_only")
    ):
        return None  # only the owner's explicit row check may re-add or repair altered evidence
    _observe_presence(waiting, info)  # never re-added here: a confirmed absence blocks the receipt
    if waiting.get("resume_attempted"):
        _start_given(waiting)  # an earlier pass gave the start and failed right after it
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
    try:
        confirmed = finish(
            h,
            str(waiting.get("path") or ""),
            receipt_of(waiting) or {},
            start_stopped=retry,
            on_start=lambda: _start_given(waiting),
        )
    except TowError as error:
        if error.code == "client.managed.missing":
            # The client's own read while finishing it did not list it: one answer without it,
            # as the look above - blocked only once that absence is confirmed.
            _observe_presence(waiting, None)
        raise
    return confirmed if isinstance(confirmed, dict) else {}


def _start_given(waiting: dict[str, Any]) -> None:
    """The receipt's one start is given (or was, by a pass that failed right after it), saved
    before the client answers. An add handed over during its client's own check gets its start
    only once its selection is applied and read back: from then on its receipt is the applied
    one, so a lost answer never makes a later pass stop, select or start it again."""
    waiting["resume_attempted"] = True
    receipt = as_dict(receipt_of(waiting))
    if receipt.get("phase") == AWAITING_INITIAL_CHECK and receipt.get("start") is True:
        _adopt_receipt(waiting, {key: value for key, value in receipt.items() if key not in {"phase", "start"}})


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
        confirmed = _confirm(topic, run, waiting, client, client_id, h)
        if confirmed is None:
            row["verification_repair"] = True  # request-local intent, not a persisted selection edit
            return False
    except _NotListedYet:
        _still_waiting(topic, run, row, waiting, h, before_error, note="check.verification_not_listed")
    except ClientVerificationPending as error:
        if not error.receipt:
            row.update(ok=False, status="failed")
            set_error(row, error)
        else:
            waiting.pop("blocked", None)
            waiting.pop("blocked_params", None)
            _adopt_receipt(waiting, error.receipt)
            _still_waiting(topic, run, row, waiting, h, before_error)
    except Exception as error:  # noqa: BLE001 - one failed confirmation never ends the batch
        _failed(topic, run, row, waiting, error, before_error)
    else:
        _finished(topic, run, row, waiting, client_id, h, client=client, info=confirmed)
    stamp_result(topic, row)
    if pass_only:
        _restore_check_times(topic, previous_check, previous_ok_at)
    return True


def _still_waiting(
    topic: Topic,
    run: CheckRun,
    row: dict[str, Any],
    waiting: dict[str, Any],
    h: str,
    before_error: tuple[Any, ...],
    *,
    note: str = "",
) -> None:
    """The client is not done yet: unconfirmed, no failure - until OVERDUE_SEC after the
    transaction's first deferral, then a red client error the owner hears once. The receipt is
    neither blocked nor changed: a later pass still confirms it."""
    pending_row(row, h)
    if note:
        row["skipped"] = t(note, owner_language())
    if _overdue(waiting):
        row.pop("skipped", None)
        _failed(topic, run, row, waiting, TowError("check.verification_overdue", cls="qbit"), before_error)


def _failed(
    topic: Topic,
    run: CheckRun,
    row: dict[str, Any],
    waiting: dict[str, Any],
    error: Exception,
    before_error: tuple[Any, ...],
) -> None:
    """The confirmation failed: a terminal reason blocks the receipt; logged and told once."""
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


def _adopt_receipt(waiting: dict[str, Any], receipt: dict[str, Any]) -> None:
    """The adapter applied a selection handed over before it (the client's own check ended) and
    the start is not confirmed yet: the same hash and selection, now read back, is the receipt
    the next pass confirms."""
    current = as_dict(receipt_of(waiting))
    if (
        current.get("phase") == AWAITING_INITIAL_CHECK
        and receipt.get("phase") is None
        and str(receipt.get("hash") or "").upper() == str(current.get("hash") or "").upper()
        and receipt.get("wanted") == current.get("wanted")
        and receipt.get("ignored", []) == current.get("ignored", [])
    ):
        _keep_receipt(waiting, receipt)


def _finished(
    topic: Topic,
    run: CheckRun,
    row: dict[str, Any],
    waiting: dict[str, Any],
    client_id: str,
    h: str,
    *,
    client: TorrentClientAdapter | None = None,
    info: dict[str, Any] | None = None,
) -> None:
    """The client confirmed the transaction: the receipt is done, History and the owner hear it.
    An add handed over before its selection that did not fit stays stopped and waits for room."""
    receipt = as_dict(receipt_of(waiting))
    if receipt.get("phase") == AWAITING_INITIAL_CHECK and receipt.get("start") is False:
        _wait_for_room(topic, run, row, waiting, client_id, h, client=client, info=info or {})
        return
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
        run.queue_notification(topic, kind=str(waiting.get("kind") or "added"), operation_id=new_operation_id("bot"))


def _wait_for_room(
    topic: Topic,
    run: CheckRun,
    row: dict[str, Any],
    waiting: dict[str, Any],
    client_id: str,
    h: str,
    *,
    client: TorrentClientAdapter | None,
    info: dict[str, Any],
) -> None:
    """Its selection applied and read back, stopped and released, the add whose files did not
    fit waits for room as such an add always does (``tow.check.space``): measured now, started
    by TOW once it fits - "added" is told then."""
    from tow.check import space

    topic.pop("verification_pending", None)
    run.record(
        "client_added",
        operation_id=new_operation_id("client-confirm"),
        component="client",
        integration_id=client_id,
        client_id=client_id,
        topic_id=topic.get("id"),
        topic=topic.get("id"),
        hash=h,
        started=False,
        status="succeeded",
        reason="verification_finished",
        how=run.how,
    )
    path = str(waiting.get("path") or "")
    topic["waiting_space"] = {
        "hash": h,
        "client": client_id,
        "needed": 0,
        "free": 0,
        "path": path,
        "since": _stamp(),
        "selection": space.selection_key(info),
        "kind": str(waiting.get("kind") or "added"),
    }
    for key in ("error", "error_record", "error_class", "skipped"):
        row.pop(key, None)
    row["verification_pending"] = False
    space.recheck(topic, run, row, client, client_id)
