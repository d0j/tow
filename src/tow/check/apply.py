"""Bring the client in line with a topic's revision: add it with its file selection, change
the selection of TOW's torrent already there, or verify it untouched - each confirmed by
read-back before it counts. A new add whose files do not fit on the target drive is added
stopped and waits for room (``tow.check.space``)."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING, Any

from tow.check import space
from tow.check.client_ops import (
    PREVIOUS_REVISION_ACTIVE,
    assert_client_can_add,
    client_unreachable,
    info_confirms,
    info_is_pending_tow_add,
    info_owned_by_tow,
    live_previous_overlap,
)
from tow.check.space import FREE_SPACE_MARGIN, Shortfall
from tow.check_steps import client_identities, is_owned_add_recovery, store_revision
from tow.clients.spec import TorrentClientAdapter
from tow.errors import TowError
from tow.events import new_operation_id
from tow.i18n import t
from tow.log import owner_language
from tow.records import Topic, topics_of

if TYPE_CHECKING:
    from tow.check.topic import CheckRun, Fetched, TopicCheck

__all__ = ["FREE_SPACE_MARGIN", "apply_revision", "free_space_problem"]


def _where_it_may_lie(dest: str, file: Any, name: str, is_multi: bool) -> tuple[Path, ...]:
    """Where ``file`` lies when it is already in ``dest`` (the client keeps it and adds only the
    rest): a multi-file torrent lands in ``dest/<name>/`` (or straight in ``dest/`` when the
    client creates no subfolder); a single file is ``dest/<name>``."""
    relative = Path(*str(file.path).replace("\\", "/").split("/"))
    return (Path(dest) / name / relative, Path(dest) / relative) if is_multi and name else (Path(dest) / relative,)


def free_space_problem(
    dest: str, files: tuple[Any, ...], selected_indices: Any, *, name: str = "", is_multi: bool = False
) -> Shortfall | None:
    """G6: the selected files do not fit on the target drive (None: they do, or it cannot be
    known - the folder is on another machine or not reachable from here).

    Only NEW bytes count (M5): files of the selection already in the folder (a new revision
    of a season whose earlier episodes are there) are not downloaded again.
    """
    wanted = set(selected_indices or ())
    selected = [f for f in files if f.index in wanted and not getattr(f, "is_pad", False)]
    return space.measure(
        dest, ((_where_it_may_lie(dest, f, name, is_multi), int(getattr(f, "size", 0) or 0), 0) for f in selected)
    )


def _shortfall(run: CheckRun, client_id: str, dest: str, metadata: Any, plan: Any) -> Shortfall | None:
    """Whether the plan's files do not fit; a client on another computer is never judged here."""
    if client_id in run.remote_clients:
        return None
    return free_space_problem(
        dest,
        metadata.files,
        plan.selected_indices,
        name=str(getattr(metadata, "name", "") or ""),
        is_multi=bool(getattr(metadata, "is_multi", False)),
    )


def _accept_existing_torrent(
    topic: Topic,
    run: CheckRun,
    row: dict[str, Any],
    info: dict[str, Any] | None,
    *,
    h: str,
    dest: str,
    plan: Any,
    operation_id: str | None,
    client_id: str,
    migrated: bool,
) -> bool:
    """The revision is already in the client (or only its hash label changed): verify it
    without touching it and return whether its file selection is TOW-verified (``info``, the
    one read of the torrent, decides both its mark and its folder)."""
    if not info_owned_by_tow(info, h):
        # Never green: TOW can change nothing about a torrent without its mark (the file
        # selection, the next revision), and it may be TOW's own add whose marking failed. The
        # hash lets the owner adopt it into TOW (tow.adopt) - never done without being asked -
        # while the topic still has the link and the client this check saw.
        seen = {"hash": h, "url": str(topic.get("url") or ""), "client": client_id}
        if plan.mode != "all":
            raise TowError("check.not_owned_partial", **seen)
        raise TowError("check.not_owned_existing", cls="qbit", **seen)
    if not info_confirms(info, h, dest):
        raise TowError("check.migration_unconfirmed" if migrated else "check.existing_unconfirmed")
    selection_verified = True
    row["added"] = False
    if migrated:
        row["hash_identity_migrated"] = True
        row["status"] = "skipped"
        row["skipped"] = t("check.skip.hash_normalized", owner_language())
    else:
        row["skipped"] = t("check.skip.already_in_client", owner_language())
        row["selection_verified"] = selection_verified
        row["status"] = "skipped"
    run.record(
        "client_updated",
        operation_id=operation_id,
        component="client",
        integration_id=client_id,
        client_id=client_id,
        client_kind=row.get("client_kind"),
        topic_id=topic.get("id"),
        topic=topic.get("id"),
        title=topic.get("title"),
        hash=h,
        status="skipped",
        reason="hash_identity_migrated" if migrated else "already_present",
        how=run.how,
    )
    return selection_verified


def _add_new_revision(
    topic: Topic,
    run: CheckRun,
    row: dict[str, Any],
    topic_client: TorrentClientAdapter,
    *,
    blob: bytes,
    metadata: Any,
    h: str,
    old: str,
    dest: str,
    plan: Any,
    policy: dict[str, Any],
    operation_id: str,
    client_id: str,
) -> str:
    """Add the revision with its file selection, confirm it by read-back, log and notify. Files
    that do not fit yet: added stopped, and the topic waits for room (the owner hears that).

    Returns the hash the client actually registered (a hybrid may come back as its v1 hash).
    """
    shortfall = _shortfall(run, client_id, dest, metadata, plan)
    if shortfall is None:
        added_info = topic_client.add_torrent_selected(blob, dest, h, plan.selected_indices)
    else:
        added_info = topic_client.add_torrent_selected(blob, dest, h, plan.selected_indices, start=False)
    resolved_hash = str((added_info or {}).get("hash") or "").upper()
    if resolved_hash:
        if resolved_hash not in client_identities(metadata):
            raise TowError("check.unexpected_identity")
        if resolved_hash != h:
            h = resolved_hash
            row["hash"] = h
            row["client_hash_legacy_v1"] = bool(h == str(getattr(metadata, "hash_v1", None) or "").upper())
    added = topic_client.inspect_torrent(h)  # the read-back: one read for the mark, folder and files
    if not info_confirms(added, h, dest, require_tow_ownership=True):
        raise TowError("check.add_unconfirmed")
    row["added"] = True
    row["status"] = "succeeded"
    kind = "updated" if old and h != old else "added"
    run.record(
        "client_added",
        operation_id=operation_id,
        component="client",
        integration_id=client_id,
        client_id=client_id,
        client_kind=row.get("client_kind"),
        topic_id=topic.get("id"),
        topic=topic.get("id"),
        title=topic.get("title"),
        hash=h,
        path=dest,
        selection_mode=plan.mode,
        selected_file_count=len(plan.selected_indices),
        total_file_count=plan.total_files,
        tracking_mode=policy["tracking_mode"],
        selected_files_preview=list(plan.selected_files[:20]),
        selection_truncated=len(plan.selected_files) > 20,
        started=shortfall is None,
        status="succeeded",
        how=run.how,
    )
    if shortfall is not None:
        # Not started: "added" is told when TOW starts it; now the owner hears the wait.
        space.wait_for_space(topic, run, row, h=h, client_id=client_id, info=added, shortfall=shortfall, kind=kind)
    elif run.notify:
        run.queue_notification(topic, kind=kind, operation_id=operation_id, tracker=str(row.get("tracker") or ""))
    return h


def _unrecorded_own_add(state: dict[str, Any], topic: Topic, h: str) -> bool:
    """A TOW-owned torrent no topic records: an earlier run added and confirmed it, but its
    commit (and with it the staged "added" message) was lost - this run finishes that add."""
    wanted = h.upper()
    if wanted in {str(value).upper() for value in topic.get("previous_hashes") or []}:
        return False  # an older revision of this topic, not a new add
    return not any(
        str(other.get("hash") or "").upper() == wanted
        for other in topics_of(state)
        if str(other.get("id")) != str(topic.get("id"))
    )


def _update_client_selection(
    topic: Topic,
    run: CheckRun,
    row: dict[str, Any],
    topic_client: TorrentClientAdapter,
    *,
    blob: bytes,
    metadata: Any,
    h: str,
    old: str,
    dest: str,
    plan: Any,
    policy: dict[str, Any],
    operation_id: str,
    client_id: str,
    resume: bool,
    pending_recovery: bool,
) -> None:
    """Apply a new file selection to a TOW-owned torrent already in the client, or finish
    an add an earlier run left pending (``resume``); confirm by read-back, log, notify. A
    torrent waiting for disk space starts when the new selection fits, else it keeps waiting."""
    info = topic_client.inspect_torrent(h)  # one read for the mark and the folder
    if not info_owned_by_tow(info, h):
        raise TowError("check.not_owned_priorities")
    if not info_confirms(info, h, dest, require_tow_ownership=True):
        raise TowError("check.save_path_unconfirmed")
    owned_add_recovery = h != old and (is_owned_add_recovery(topic) or _unrecorded_own_add(run.state, topic, h))
    completed_pending_add = resume or owned_add_recovery
    waiting = space.waiting_for(topic, h)
    shortfall = _shortfall(run, client_id, dest, metadata, plan) if waiting is not None else None
    if waiting is not None and shortfall is None:
        revisions = [str(value) for value in topic.get("previous_hashes") or []]
        if overlap := live_previous_overlap(topic_client, revisions, metadata.files):
            raise TowError(PREVIOUS_REVISION_ACTIVE, file=overlap)
    starts_waiting = waiting is not None and shortfall is None
    topic_client.configure_torrent_selection(
        blob, h, plan.selected_indices, ensure_started=completed_pending_add or starts_waiting
    )
    after = topic_client.inspect_torrent(h)
    if not info_confirms(after, h, dest, require_tow_ownership=True):
        raise TowError("check.selection_unconfirmed")
    row["added"] = completed_pending_add
    row["selection_updated"] = not completed_pending_add
    row["pending_add_recovered"] = pending_recovery
    row["status"] = "succeeded"
    run.record(
        "client_added" if completed_pending_add else "client_selection_updated",
        operation_id=operation_id,
        component="client",
        integration_id=client_id,
        client_id=client_id,
        client_kind=row.get("client_kind"),
        topic_id=topic.get("id"),
        topic=topic.get("id"),
        title=topic.get("title"),
        hash=h,
        selection_mode=plan.mode,
        selected_file_count=len(plan.selected_indices),
        total_file_count=plan.total_files,
        tracking_mode=policy["tracking_mode"],
        selected_files_preview=list(plan.selected_files[:20]),
        selection_truncated=len(plan.selected_files) > 20,
        recovered=pending_recovery or owned_add_recovery,
        status="succeeded",
        how=run.how,
    )
    if completed_pending_add and run.notify:
        run.queue_notification(
            topic,
            kind="updated" if old and h != old else "added",
            operation_id=operation_id,
            tracker=str(row.get("tracker") or ""),
        )
    if waiting is not None:
        kind = str(waiting.get("kind") or "added")
        if shortfall is not None:
            space.wait_for_space(topic, run, row, h=h, client_id=client_id, info=after, shortfall=shortfall, kind=kind)
        else:
            space.started(topic, run, row, h=h, client_id=client_id, kind=kind)


def apply_revision(
    work: TopicCheck,
    fetched: Fetched,
    metadata: Any,
    *,
    h: str,
    migrates: bool,
    plan: Any,
    policy: dict[str, Any],
    needs_selection_update: bool,
) -> None:
    """Hand the revision (or its new file selection) to the client and record what it confirmed."""
    topic, run, row, old = work.topic, work.run, work.row, work.old
    dest = (topic.get("save_path") or "").strip()
    if not dest:
        raise TowError("check.no_save_path")
    operation_id = work.operation_id = new_operation_id("client-add")
    row["operation_id"] = operation_id
    run.record(
        "client_add_started",
        operation_id=operation_id,
        component="client",
        integration_id=work.client_id,
        client_id=work.client_id,
        client_kind=row.get("client_kind"),
        topic_id=topic.get("id"),
        topic=topic.get("id"),
        title=topic.get("title"),
        hash=h,
        path=dest,
        selection_mode=plan.mode,
        selected_file_count=len(plan.selected_indices),
        total_file_count=plan.total_files,
        tracking_mode=policy["tracking_mode"],
        status="started",
        how=run.how,
    )
    client = work.client
    if client is None:
        raise client_unreachable(run, work.client_id)
    assert_client_can_add(
        run.state,
        topic,
        client,
        h=h,
        old=old,
        dest=dest,
        plan=plan,
        files=metadata.files,
        replaces_revision=bool(h != old and old and not migrates),
    )
    h, selection_verified = _hand_to_client(
        work,
        client,
        fetched,
        metadata,
        h=h,
        dest=dest,
        migrates=migrates,
        plan=plan,
        policy=policy,
        needs_selection_update=needs_selection_update,
    )
    store_revision(
        topic,
        h=h,
        old=old,
        keep_previous=bool(old and old != h and not migrates),
        selection_verified=selection_verified,
        plan=plan,
        once=policy["tracking_mode"] == "once",
        files=metadata.files,
    )
    if not row.get("waiting_space"):
        space.forget(topic)  # a revision handed over and running: nothing waits any more
    if migrates:
        row["hash_identity_from"] = old  # reconcile relabels the history (B7)


def _hand_to_client(
    work: TopicCheck,
    client: TorrentClientAdapter,
    fetched: Fetched,
    metadata: Any,
    *,
    h: str,
    dest: str,
    migrates: bool,
    plan: Any,
    policy: dict[str, Any],
    needs_selection_update: bool,
) -> tuple[str, bool]:
    """Add, reconfigure or just verify the torrent in the client: (the hash the client knows it
    by, whether its file selection is TOW-verified)."""
    topic, run, row, old = work.topic, work.run, work.row, work.old
    operation_id = str(work.operation_id)
    # One read of the torrent decides whether it is there, an unfinished add of TOW's and TOW's
    # own (a torrent the client does not have reports None).
    info = client.inspect_torrent(h)
    exists = info is not None
    pending_recovery = info_is_pending_tow_add(info, h)
    if migrates and exists and not needs_selection_update:
        verified = _accept_existing_torrent(
            topic,
            run,
            row,
            info,
            h=h,
            dest=dest,
            plan=plan,
            operation_id=operation_id,
            client_id=work.client_id,
            migrated=True,
        )
        return h, verified
    if not exists:
        added = _add_new_revision(
            topic,
            run,
            row,
            client,
            blob=fetched.blob,
            metadata=metadata,
            h=h,
            old=old,
            dest=dest,
            plan=plan,
            policy=policy,
            operation_id=operation_id,
            client_id=work.client_id,
        )
        return added, True
    if needs_selection_update or (h != old and info_owned_by_tow(info, h)):
        _update_client_selection(
            topic,
            run,
            row,
            client,
            blob=fetched.blob,
            metadata=metadata,
            h=h,
            old=old,
            dest=dest,
            plan=plan,
            policy=policy,
            operation_id=operation_id,
            client_id=work.client_id,
            resume=fetched.magnet_hash is not None or pending_recovery,
            pending_recovery=pending_recovery,
        )
        return h, True
    verified = _accept_existing_torrent(
        topic,
        run,
        row,
        info,
        h=h,
        dest=dest,
        plan=plan,
        operation_id=operation_id,
        client_id=work.client_id,
        migrated=False,
    )
    return h, verified
